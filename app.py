import os
import re
import time
import uuid
import json
import shlex
import threading
import subprocess
from queue import Queue, Empty
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse


app = FastAPI()

# ========= Config =========
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
DOWNLOAD_DIR = "/tmp/downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

MAX_QUEUE = 3                  # Render Free: keep this small
MAX_JOBS_IN_MEMORY = 30        # how many jobs to keep in the list
KEEP_LOG_LINES = 450           # log line cap

q: "Queue[dict]" = Queue(maxsize=MAX_QUEUE)

# job_id -> job dict
jobs: Dict[str, Dict[str, Any]] = {}
jobs_order: List[str] = []     # newest-first list


# ========= Helpers =========
def auth(req: Request):
    if ADMIN_TOKEN and req.headers.get("X-Admin-Token") != ADMIN_TOKEN:
        raise HTTPException(401, "Missing/invalid X-Admin-Token")


def now_ts() -> int:
    return int(time.time())


def clamp_int(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def normalize_optional_time(value) -> Optional[str]:
    """
    Accepts:
      - None, "", "none", "null", "undefined" -> None
      - "00:00:00" -> "00:00:00"
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # don't accept numbers as timestamps; force string input
        return None
    s = str(value).strip()
    if s == "":
        return None
    if s.lower() in ("none", "null", "undefined"):
        return None
    # simple validation: allow H:MM:SS or HH:MM:SS
    if not re.fullmatch(r"\d{1,2}:\d{2}:\d{2}", s):
        raise HTTPException(400, "beginning/ending must look like HH:MM:SS (example 02:00:00)")
    return s


def normalize_color(value: str) -> str:
    s = (value or "").strip()
    if not s:
        return "#111111"
    # accept #RRGGBB or #AARRGGBB
    if not re.fullmatch(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{8})", s):
        raise HTTPException(400, "background_color must be like #RRGGBB or #AARRGGBB")
    return s


def add_job(job: Dict[str, Any]) -> None:
    job_id = job["job_id"]
    jobs[job_id] = job
    jobs_order.insert(0, job_id)
    # trim
    while len(jobs_order) > MAX_JOBS_IN_MEMORY:
        old = jobs_order.pop()
        try:
            # best-effort cleanup of any file
            path = jobs.get(old, {}).get("path") or ""
            if path and os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
        jobs.pop(old, None)


def get_job_or_404(job_id: str) -> Dict[str, Any]:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


def append_log(job: Dict[str, Any], line: str) -> None:
    lines = (job.get("log") or "").splitlines()
    lines.append(line)
    if len(lines) > KEEP_LOG_LINES:
        lines = lines[-KEEP_LOG_LINES:]
    job["log"] = "\n".join(lines)
    job["last_log_line"] = line


def hint_from_log(log: str) -> str:
    if not log:
        return ""
    if "Couldn't find a valid ICU package installed" in log or "dotnet-missing-libicu" in log:
        return (
            "Your container is missing ICU. Fix Dockerfile: add `libicu-dev` (or `libicu72`) "
            "to apt-get install, then redeploy."
        )
    if "Quality not found" in log or "Unable to find requested quality" in log:
        return (
            "Requested quality isn't available for this VOD. Try `1080p` instead of `1080p60`, "
            "or leave quality on Auto/Best."
        )
    if "429" in log or "Too Many Requests" in log:
        return (
            "Twitch is rate limiting you. Lower threads (2), and consider setting bandwidth."
        )
    return ""


def run_and_log(job: Dict[str, Any], cmd: List[str], stage: str) -> None:
    job["stage"] = stage
    append_log(job, f"\n=== {stage} ===")
    append_log(job, "CMD: " + " ".join(shlex.quote(x) for x in cmd))

    # keep handle so we can cancel later (best-effort)
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    job["_proc_pid"] = p.pid

    try:
        for line in p.stdout:
            append_log(job, line.rstrip())
            # cancellation check (best effort)
            if job.get("cancel_requested"):
                try:
                    p.terminate()
                except Exception:
                    pass
    finally:
        rc = p.wait()

    if job.get("cancel_requested"):
        raise RuntimeError("Cancelled by user")

    if rc != 0:
        raise RuntimeError(f"{stage} failed (exit {rc})")


# ========= TwitchDownloaderCLI command builders =========
def td_videodownload(vod_id: str, out_path: str, quality: str, threads: int,
                     bandwidth: Optional[int], beginning: Optional[str], ending: Optional[str]) -> List[str]:
    cmd = ["TwitchDownloaderCLI", "videodownload", "--id", vod_id, "-o", out_path]
    if quality:
        cmd += ["--quality", quality]  # 1080p60/1080p/etc
    cmd += ["--threads", str(threads)]
    if bandwidth is not None:
        cmd += ["--bandwidth", str(bandwidth)]
    if beginning:
        cmd += ["--beginning", beginning]
    if ending:
        cmd += ["--ending", ending]
    cmd += ["--temp-path", "/tmp"]
    return cmd


def td_chatdownload(vod_id: str, out_path: str, threads: int,
                    beginning: Optional[str], ending: Optional[str]) -> List[str]:
    # gzip json + embed images for best render reliability
    cmd = [
        "TwitchDownloaderCLI", "chatdownload",
        "--id", vod_id,
        "-o", out_path,
        "--compression", "Gzip",
        "-E",
        "--threads", str(threads),
        "--temp-path", "/tmp",
    ]
    if beginning:
        cmd += ["--beginning", beginning]
    if ending:
        cmd += ["--ending", ending]
    return cmd


def td_chatrender(chat_json_path: str, out_path: str, chat_width: int, chat_height: int,
                  font_size: int, framerate: int, update_rate: float,
                  background_color: str, outline: bool) -> List[str]:
    cmd = [
        "TwitchDownloaderCLI", "chatrender",
        "-i", chat_json_path,
        "-o", out_path,
        "-w", str(chat_width),
        "-h", str(chat_height),
        "--font-size", str(font_size),
        "--framerate", str(framerate),
        "--update-rate", str(update_rate),
        "--background-color", background_color,
        "--temp-path", "/tmp",
        "--readable-colors", "true",
    ]
    if outline:
        cmd += ["--outline"]
    return cmd


def ffmpeg_side_by_side(video_path: str, chat_path: str, out_path: str, chat_width: int, height: int) -> List[str]:
    # make final width = 1920 + chat_width
    filter_complex = (
        f"[0:v]scale=1920:{height}:force_original_aspect_ratio=decrease,"
        f"pad=1920:{height}:(ow-iw)/2:(oh-ih)/2[vid];"
        f"[1:v]scale={chat_width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={chat_width}:{height}:(ow-iw)/2:(oh-ih)/2[chat];"
        f"[vid][chat]hstack=inputs=2[v]"
    )
    return [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", chat_path,
        "-filter_complex", filter_complex,
        "-map", "[v]",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "18",
        "-c:a", "aac",
        "-b:a", "160k",
        "-movflags", "+faststart",
        out_path
    ]


# ========= Worker loop =========
def worker_loop():
    while True:
        try:
            payload = q.get(timeout=1)
        except Empty:
            continue

        job_id = payload["job_id"]
        job = jobs.get(job_id)
        if not job:
            q.task_done()
            continue

        job["status"] = "running"
        job["started_at"] = now_ts()
        job["finished_at"] = 0
        job["error"] = ""
        job["hint"] = ""

        try:
            vod_id = payload["vod_id"]
            quality = payload["quality"]
            threads = payload["threads"]
            bandwidth = payload["bandwidth"]
            beginning = payload["beginning"]
            ending = payload["ending"]

            include_chat = payload["include_chat"]

            video_path = payload["video_path"]
            final_path = payload["final_path"]

            run_and_log(job, td_videodownload(vod_id, video_path, quality, threads, bandwidth, beginning, ending),
                        "VideoDownload")

            if include_chat:
                chat_json = payload["chat_json"]
                chat_mp4 = payload["chat_mp4"]

                run_and_log(job, td_chatdownload(vod_id, chat_json, threads, beginning, ending),
                            "ChatDownload")

                run_and_log(job, td_chatrender(
                    chat_json, chat_mp4,
                    payload["chat_width"], payload["chat_height"],
                    payload["font_size"], payload["framerate"], payload["update_rate"],
                    payload["background_color"], payload["outline"]
                ), "ChatRender")

                run_and_log(job, ffmpeg_side_by_side(video_path, chat_mp4, final_path, payload["chat_width"], payload["chat_height"]),
                            "Combine (Video + Chat)")

                # cleanup intermediates (save /tmp)
                for p in (chat_json, chat_mp4, video_path):
                    try:
                        if os.path.exists(p):
                            os.remove(p)
                    except Exception:
                        pass
            else:
                os.replace(video_path, final_path)

            job["status"] = "done"
            job["stage"] = "done"
            job["path"] = final_path

        except Exception as e:
            job["status"] = "error"
            job["stage"] = "failed"
            job["error"] = str(e)
            job["hint"] = hint_from_log(job.get("log", ""))

        finally:
            job["finished_at"] = now_ts()
            job.pop("_proc_pid", None)
            q.task_done()


threading.Thread(target=worker_loop, daemon=True).start()


# ========= API =========
@app.get("/healthz")
def healthz():
    return {"ok": True, "ts": now_ts()}


@app.get("/", response_class=HTMLResponse)
def home():
    return """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>TwitchDownloader (Render Free)</title>
  <style>
    :root{
      --bg:#0b0f19; --card:#101827; --card2:#0f172a; --text:#e5e7eb; --muted:#9ca3af;
      --line:#243045; --accent:#60a5fa; --good:#34d399; --warn:#fbbf24; --bad:#fb7185;
      --shadow:0 12px 35px rgba(0,0,0,.35); --r:16px;
      --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
      --sans: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, "Apple Color Emoji","Segoe UI Emoji";
    }
    *{box-sizing:border-box}
    body{margin:0;font-family:var(--sans);background:
      radial-gradient(1100px 800px at 15% 10%, rgba(96,165,250,.18), transparent 55%),
      radial-gradient(900px 700px at 85% 20%, rgba(52,211,153,.14), transparent 55%),
      var(--bg); color:var(--text);}
    .wrap{max-width:1150px;margin:26px auto 70px;padding:0 18px}
    header{display:flex;justify-content:space-between;gap:14px;align-items:flex-start;margin-bottom:16px}
    h1{margin:0;font-size:22px;letter-spacing:.2px}
    .sub{margin-top:6px;color:var(--muted);font-size:14px;line-height:1.4}
    .pill{display:inline-flex;align-items:center;gap:8px;padding:8px 10px;border:1px solid var(--line);
      border-radius:999px;background:rgba(255,255,255,.04);color:var(--muted);font-size:13px}
    .grid{display:grid;gap:14px;grid-template-columns:1fr}
    @media(min-width:980px){.grid{grid-template-columns:1.1fr .9fr;align-items:start}}
    .card{background:linear-gradient(180deg, rgba(255,255,255,.05), rgba(255,255,255,0)), var(--card);
      border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--shadow);overflow:hidden}
    .hd{padding:14px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;gap:10px;
      background:rgba(0,0,0,.10)}
    .hd h2{margin:0;font-size:14px;letter-spacing:.2px}
    .bd{padding:14px}
    .row{display:flex;gap:10px;flex-wrap:wrap}
    .f{flex:1;min-width:220px}
    label{display:block;font-size:12px;color:var(--muted);margin:0 0 6px}
    input,select,button,textarea{
      width:100%; font:inherit; color:var(--text); background:rgba(0,0,0,.14);
      border:1px solid var(--line); border-radius:12px; padding:10px 12px; outline:none;
    }
    input:focus,select:focus,textarea:focus{border-color:rgba(96,165,250,.7)}
    button{cursor:pointer;font-weight:650;background:rgba(96,165,250,.18);border-color:rgba(96,165,250,.45)}
    button:hover{transform:translateY(-1px)} button:active{transform:translateY(0px)}
    .btn2{background:rgba(0,0,0,.14);border-color:var(--line)}
    .btnBad{background:rgba(251,113,133,.18);border-color:rgba(251,113,133,.45)}
    .btnGood{background:rgba(52,211,153,.18);border-color:rgba(52,211,153,.45)}
    .toggle{display:flex;align-items:center;gap:10px;padding:10px 12px;border:1px solid var(--line);
      border-radius:12px;background:rgba(0,0,0,.10);user-select:none}
    .toggle input{width:auto}
    details{border:1px solid var(--line);border-radius:12px;padding:10px 12px;background:rgba(0,0,0,.08)}
    summary{cursor:pointer;font-weight:650}
    .muted{color:var(--muted);font-size:13px;line-height:1.45}
    .statusbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:10px 12px;border:1px solid var(--line);
      border-radius:12px;background:rgba(0,0,0,.10)}
    .dot{width:10px;height:10px;border-radius:999px;background:var(--muted)}
    .good{background:var(--good)} .warn{background:var(--warn)} .bad{background:var(--bad)}
    .mono{font-family:var(--mono)}
    pre{margin:0;background:rgba(0,0,0,.22);border:1px solid var(--line);border-radius:12px;padding:12px;overflow:auto;max-height:420px}
    .jobs{display:flex;flex-direction:column;gap:10px}
    .job{padding:10px 12px;border:1px solid var(--line);border-radius:12px;background:rgba(0,0,0,.10);cursor:pointer}
    .job:hover{border-color:rgba(96,165,250,.55)}
    .jobTop{display:flex;justify-content:space-between;gap:10px;align-items:center}
    .badge{font-size:12px;padding:3px 8px;border-radius:999px;border:1px solid var(--line);color:var(--muted)}
    .badge.good{border-color:rgba(52,211,153,.6);color:rgba(52,211,153,.95)}
    .badge.bad{border-color:rgba(251,113,133,.7);color:rgba(251,113,133,.95)}
    .badge.warn{border-color:rgba(251,191,36,.75);color:rgba(251,191,36,.95)}
    .hint{margin-top:10px;padding:10px 12px;border-radius:12px;border:1px solid rgba(251,191,36,.35);
      background:rgba(251,191,36,.08);color:rgba(251,191,36,.95);font-size:13px;line-height:1.4}
    .err{margin-top:10px;padding:10px 12px;border-radius:12px;border:1px solid rgba(251,113,133,.35);
      background:rgba(251,113,133,.08);color:rgba(251,113,133,.95);font-size:13px;line-height:1.4}
    .ok{margin-top:10px;padding:10px 12px;border-radius:12px;border:1px solid rgba(52,211,153,.35);
      background:rgba(52,211,153,.08);color:rgba(52,211,153,.95);font-size:13px;line-height:1.4}
    .actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:10px}
    .actions > button{flex:1;min-width:220px}
  </style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>TwitchDownloader (Render Free)</h1>
      <div class="sub">
        1080p video + optional chat render. For long VODs, use 2-hour chunks (Begin/End).<br/>
        Free services can sleep; this page sends keep-alive pings automatically.
      </div>
    </div>
    <div class="pill">
      <span id="wakeDot" class="dot warn"></span>
      <span id="wakeText">Warming up…</span>
    </div>
  </header>

  <div class="grid">
    <!-- Left: New Job -->
    <section class="card">
      <div class="hd">
        <h2>New Job</h2>
        <button class="btn2" id="btnToken">Set Token</button>
      </div>
      <div class="bd">

        <div class="statusbar" style="margin-bottom:12px">
          <span id="statusDot" class="dot"></span>
          <div style="flex:1">
            <div id="statusText" style="font-weight:650">Idle</div>
            <div class="muted" id="statusSub">Ready.</div>
          </div>
          <div class="badge" id="tokenBadge">token: not set</div>
        </div>

        <div class="row">
          <div class="f">
            <label>VOD ID</label>
            <input id="vod" placeholder="Numbers only (example: 2656545471)" />
          </div>
          <div class="f">
            <label>Quality</label>
            <select id="quality">
              <option value="1080p60" selected>1080p60 (try)</option>
              <option value="1080p">1080p (try)</option>
              <option value="">Auto / Best available</option>
              <option value="720p60">720p60</option>
              <option value="720p">720p</option>
            </select>
          </div>
        </div>

        <div class="row">
          <div class="f">
            <label>Threads (Render Free recommended: 2)</label>
            <input id="threads" value="2" />
          </div>
          <div class="f">
            <label>Bandwidth KiB/s per thread (optional)</label>
            <input id="bandwidth" placeholder="e.g. 4000" />
          </div>
        </div>

        <details style="margin-top:10px">
          <summary>Chunking (Begin / End)</summary>
          <div class="muted" style="margin:10px 0 12px">
            Use chunks for 3–6 hour VODs (ex: 00:00:00 → 02:00:00). Leave blank for full VOD.
          </div>
          <div class="row">
            <div class="f">
              <label>Begin (HH:MM:SS)</label>
              <input id="begin" placeholder="00:00:00" />
            </div>
            <div class="f">
              <label>End (HH:MM:SS)</label>
              <input id="end" placeholder="02:00:00" />
            </div>
          </div>
          <div class="actions">
            <button class="btn2" id="btnSet2h">Set 0→2h</button>
            <button class="btn2" id="btnNext2h">Next 2h Chunk</button>
          </div>
        </details>

        <div class="toggle" style="margin-top:10px">
          <input id="include_chat" type="checkbox" checked />
          <div>
            <div style="font-weight:650">Render chat + combine</div>
            <div class="muted">Side-by-side chat panel (most reliable on Free).</div>
          </div>
        </div>

        <details style="margin-top:10px" id="chatSettings">
          <summary>Chat render settings</summary>
          <div class="row" style="margin-top:10px">
            <div class="f">
              <label>Chat width (px)</label>
              <input id="chat_width" value="422" />
            </div>
            <div class="f">
              <label>Font size</label>
              <input id="font_size" value="18" />
            </div>
          </div>
          <div class="row">
            <div class="f">
              <label>Framerate</label>
              <input id="framerate" value="30" />
            </div>
            <div class="f">
              <label>Update rate (seconds)</label>
              <input id="update_rate" value="0.2" />
            </div>
          </div>
          <div class="row">
            <div class="f">
              <label>Background color</label>
              <input id="bg" value="#111111" />
            </div>
            <div class="f">
              <label>Outline</label>
              <select id="outline">
                <option value="false" selected>Off</option>
                <option value="true">On</option>
              </select>
            </div>
          </div>
        </details>

        <div class="actions">
          <button class="btnGood" id="btnStart">Start Job</button>
          <button class="btnBad" id="btnCancel" disabled>Cancel Running Job</button>
        </div>

        <div id="msgArea"></div>
      </div>
    </section>

    <!-- Right: Jobs + Log -->
    <section class="card">
      <div class="hd">
        <h2>Jobs</h2>
        <div class="inline" style="display:flex;gap:10px">
          <button class="btn2" id="btnRefresh">Refresh</button>
          <button class="btn2" id="btnClearLocal">Clear Local History</button>
        </div>
      </div>
      <div class="bd">
        <div class="muted" style="margin-bottom:10px">
          Click a job to view details. Logs show the most recent lines.
        </div>

        <div class="jobs" id="jobsList"></div>

        <div style="margin-top:12px">
          <div class="row">
            <div class="f">
              <label>Log filter (optional)</label>
              <input id="logFilter" placeholder="type to filter lines…" />
            </div>
            <div class="f">
              <label>Actions</label>
              <div class="row">
                <button class="btn2" id="btnCopyLog">Copy Log</button>
                <button class="btn2" id="btnDownloadLog">Download Log</button>
              </div>
            </div>
          </div>
          <pre id="logBox" class="mono">Select a job to see logs.</pre>
          <div id="jobHint"></div>
          <div id="jobError"></div>
          <div id="jobOk"></div>
          <div class="actions">
            <button class="btn2" id="btnDownload" disabled>Download MP4</button>
            <button class="btn2" id="btnDelete" disabled>Delete Job</button>
          </div>
        </div>

      </div>
    </section>
  </div>
</div>

<script>
  // ===== Token handling =====
  const TOKEN_KEY = "td_admin_token";
  function getToken(){ return localStorage.getItem(TOKEN_KEY) || ""; }
  function setToken(t){ localStorage.setItem(TOKEN_KEY, t); updateTokenBadge(); }
  function clearToken(){ localStorage.removeItem(TOKEN_KEY); updateTokenBadge(); }

  function updateTokenBadge(){
    const badge = document.getElementById("tokenBadge");
    const t = getToken();
    badge.textContent = t ? ("token: set (" + t.slice(0,3) + "…" + t.slice(-3) + ")") : "token: not set";
    badge.className = "badge " + (t ? "good" : "warn");
  }

  document.getElementById("btnToken").onclick = () => {
    const existing = getToken();
    const t = prompt("Enter ADMIN token (stored in this browser).", existing || "");
    if(t === null) return;
    const trimmed = t.trim();
    if(!trimmed){ clearToken(); return; }
    setToken(trimmed);
  };

  // ===== UI helpers =====
  function setStatus(kind, title, sub){
    const dot = document.getElementById("statusDot");
    dot.className = "dot " + (kind || "");
    document.getElementById("statusText").textContent = title || "";
    document.getElementById("statusSub").textContent = sub || "";
  }

  function fmtElapsed(job){
    const s = job.started_at || 0;
    const f = job.finished_at || 0;
    if(!s) return "";
    const end = f ? f : Math.floor(Date.now()/1000);
    const sec = Math.max(0, end - s);
    const h = Math.floor(sec/3600);
    const m = Math.floor((sec%3600)/60);
    const r = sec%60;
    const mm = String(m).padStart(2,"0");
    const rr = String(r).padStart(2,"0");
    return (h>0 ? (h+":"+mm+":"+rr) : (m+":"+rr));
  }

  function safeJsonParse(t){
    try { return JSON.parse(t); } catch(e){ return null; }
  }

  function showMsg(html, cls){
    const m = document.getElementById("msgArea");
    m.innerHTML = '<div class="'+cls+'">'+html+'</div>';
  }
  function clearMsg(){ document.getElementById("msgArea").innerHTML = ""; }

  // ===== Chunk helper =====
  function toSeconds(hms){
    const parts = hms.split(":").map(x=>parseInt(x,10));
    if(parts.length!==3) return 0;
    return parts[0]*3600 + parts[1]*60 + parts[2];
  }
  function fromSeconds(sec){
    const h = Math.floor(sec/3600);
    const m = Math.floor((sec%3600)/60);
    const s = sec%60;
    return String(h).padStart(2,"0")+":"+String(m).padStart(2,"0")+":"+String(s).padStart(2,"0");
  }
  document.getElementById("btnSet2h").onclick = () => {
    document.getElementById("begin").value = "00:00:00";
    document.getElementById("end").value = "02:00:00";
  };
  document.getElementById("btnNext2h").onclick = () => {
    const b = document.getElementById("begin").value.trim() || "00:00:00";
    const e = document.getElementById("end").value.trim() || "02:00:00";
    const b2 = toSeconds(b) + 2*3600;
    const e2 = toSeconds(e) + 2*3600;
    document.getElementById("begin").value = fromSeconds(b2);
    document.getElementById("end").value = fromSeconds(e2);
  };

  // Enable/disable chat settings UI
  const includeChatEl = document.getElementById("include_chat");
  function syncChatSettings(){
    document.getElementById("chatSettings").style.display = includeChatEl.checked ? "block" : "none";
  }
  includeChatEl.onchange = syncChatSettings;
  syncChatSettings();

  // ===== Jobs list =====
  let selectedJobId = null;
  let pollTimer = null;

  async function api(path, opts={}){
    const t = getToken();
    if(!t){
      throw new Error("Token not set. Click “Set Token”.");
    }
    const headers = Object.assign({}, opts.headers || {}, {"X-Admin-Token": t});
    return fetch(path, Object.assign({}, opts, {headers}));
  }

  function badgeForStatus(st){
    if(st==="done") return "badge good";
    if(st==="error") return "badge bad";
    if(st==="running") return "badge warn";
    return "badge";
  }

  function renderJobs(jobs){
    const list = document.getElementById("jobsList");
    if(!jobs.length){
      list.innerHTML = '<div class="muted">No jobs yet.</div>';
      return;
    }
    list.innerHTML = jobs.map(j => {
      const active = (j.job_id === selectedJobId) ? 'style="border-color: rgba(96,165,250,.75)"' : "";
      const el = fmtElapsed(j);
      return `
        <div class="job" ${active} onclick="selectJob('${j.job_id}')">
          <div class="jobTop">
            <div>
              <div style="font-weight:650">VOD ${j.vod_id}</div>
              <div class="muted">Stage: ${j.stage || ""} • Elapsed: ${el || "—"}</div>
            </div>
            <div class="${badgeForStatus(j.status)}">${j.status}</div>
          </div>
          <div class="muted mono" style="margin-top:8px;font-size:12px;opacity:.9">
            ${j.job_id}
          </div>
        </div>
      `;
    }).join("");
  }

  async function refreshJobs(){
    try{
      const r = await api("/api/jobs");
      const data = await r.json();
      renderJobs(data.jobs || []);
      // keep selected job highlighted
      if(selectedJobId){
        // re-select to refresh details
        await selectJob(selectedJobId, true);
      }
    }catch(e){
      renderJobs([]);
      setStatus("bad","Auth / API error", e.message);
      showMsg(e.message, "err");
    }
  }

  window.selectJob = async function(jobId, silent=false){
    selectedJobId = jobId;
    if(!silent) clearMsg();

    document.getElementById("btnDownload").disabled = true;
    document.getElementById("btnDelete").disabled = true;

    try{
      const r = await api("/api/jobs/" + jobId);
      const job = await r.json();
      updateDetail(job);
      renderJobs((await (await api("/api/jobs")).json()).jobs || []);
      startPollingIfNeeded(job);
    }catch(e){
      if(!silent) showMsg(e.message, "err");
    }
  }

  function updateDetail(job){
    // Status header left side
    const st = job.status || "unknown";
    if(st==="running") setStatus("warn","Running", "Stage: " + (job.stage||""));
    else if(st==="done") setStatus("good","Done", "Ready to download.");
    else if(st==="error") setStatus("bad","Error", job.error || "Failed.");
    else setStatus("", "Idle", "Ready.");

    // log display with filter
    const filter = document.getElementById("logFilter").value.trim().toLowerCase();
    const lines = (job.log || "").split("\\n");
    const filtered = filter ? lines.filter(x => x.toLowerCase().includes(filter)) : lines;
    const tail = filtered.slice(-220).join("\\n");
    document.getElementById("logBox").textContent = tail || "(no log yet)";

    // hint + error boxes
    const hint = job.hint || "";
    document.getElementById("jobHint").innerHTML = hint ? '<div class="hint"><b>Hint:</b> ' + hint + '</div>' : "";
    document.getElementById("jobError").innerHTML = (st==="error" && job.error) ? '<div class="err"><b>Error:</b> ' + escapeHtml(job.error) + '</div>' : "";
    document.getElementById("jobOk").innerHTML = (st==="done") ? '<div class="ok"><b>Done:</b> Click Download MP4.</div>' : "";

    // buttons
    document.getElementById("btnDownload").disabled = (st!=="done");
    document.getElementById("btnDelete").disabled = false;

    // cancel button only if running
    document.getElementById("btnCancel").disabled = (st!=="running");
  }

  function escapeHtml(s){
    return (s||"").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");
  }

  function startPollingIfNeeded(job){
    if(pollTimer) clearInterval(pollTimer);
    if(job.status === "running" || job.status === "queued"){
      pollTimer = setInterval(async () => {
        try{
          const r = await api("/api/jobs/" + selectedJobId);
          const j = await r.json();
          updateDetail(j);
          // refresh list to show new status/elapsed
          const listData = await (await api("/api/jobs")).json();
          renderJobs(listData.jobs || []);
          if(j.status !== "running" && j.status !== "queued"){
            clearInterval(pollTimer);
            pollTimer = null;
          }
        }catch(e){
          // ignore transient
        }
      }, 2500);
    }
  }

  // ===== Start job =====
  document.getElementById("btnStart").onclick = async () => {
    clearMsg();
    const vod = document.getElementById("vod").value.trim();
    if(!/^[0-9]+$/.test(vod)) return showMsg("VOD ID must be numbers only.", "err");

    const payload = {
      vod_id: vod,
      quality: document.getElementById("quality").value,
      threads: document.getElementById("threads").value.trim(),
      bandwidth: document.getElementById("bandwidth").value.trim(),
      beginning: document.getElementById("begin").value.trim(), // send "" not null
      ending: document.getElementById("end").value.trim(),      // send "" not null
      include_chat: document.getElementById("include_chat").checked,

      chat_width: document.getElementById("chat_width").value.trim(),
      font_size: document.getElementById("font_size").value.trim(),
      framerate: document.getElementById("framerate").value.trim(),
      update_rate: document.getElementById("update_rate").value.trim(),
      background_color: document.getElementById("bg").value.trim(),
      outline: document.getElementById("outline").value
    };

    try{
      setStatus("warn","Queueing…","Creating job…");
      const r = await api("/api/jobs", {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify(payload)
      });
      const txt = await r.text();
      if(!r.ok){
        const maybe = safeJsonParse(txt);
        const msg = maybe?.detail || txt;
        setStatus("bad","Error", msg);
        return showMsg(msg, "err");
      }
      const data = JSON.parse(txt);
      showMsg("Job created: <span class='mono'>" + data.job_id + "</span>", "ok");
      await refreshJobs();
      await selectJob(data.job_id);
    }catch(e){
      setStatus("bad","Error", e.message);
      showMsg(e.message, "err");
    }
  };

  // ===== Cancel/Delete/Download/Log actions =====
  document.getElementById("btnCancel").onclick = async () => {
    if(!selectedJobId) return;
    try{
      const r = await api("/api/jobs/" + selectedJobId + "/cancel", {method:"POST"});
      const data = await r.json();
      showMsg("Cancel requested.", "hint");
      await selectJob(selectedJobId);
    }catch(e){
      showMsg("Cancel failed: " + e.message, "err");
    }
  };

  document.getElementById("btnDelete").onclick = async () => {
    if(!selectedJobId) return;
    try{
      await api("/api/jobs/" + selectedJobId + "/delete", {method:"POST"});
      selectedJobId = null;
      document.getElementById("logBox").textContent = "Select a job to see logs.";
      document.getElementById("jobHint").innerHTML = "";
      document.getElementById("jobError").innerHTML = "";
      document.getElementById("jobOk").innerHTML = "";
      document.getElementById("btnDownload").disabled = true;
      document.getElementById("btnCancel").disabled = true;
      showMsg("Job deleted.", "ok");
      await refreshJobs();
    }catch(e){
      showMsg("Delete failed: " + e.message, "err");
    }
  };

  document.getElementById("btnDownload").onclick = () => {
    if(!selectedJobId) return;
    window.location.href = "/api/jobs/" + selectedJobId + "/file";
  };

  document.getElementById("btnCopyLog").onclick = async () => {
    const t = document.getElementById("logBox").textContent;
    await navigator.clipboard.writeText(t);
    showMsg("Copied log to clipboard.", "ok");
  };

  document.getElementById("btnDownloadLog").onclick = () => {
    const t = document.getElementById("logBox").textContent;
    const blob = new Blob([t], {type:"text/plain"});
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = (selectedJobId ? selectedJobId : "log") + ".txt";
    a.click();
    URL.revokeObjectURL(a.href);
  };

  document.getElementById("btnRefresh").onclick = refreshJobs;

  document.getElementById("btnClearLocal").onclick = () => {
    // only clears token in this UI (server job list stays)
    if(confirm("Clear saved token from this browser?")){
      clearToken();
      showMsg("Token cleared. Click Set Token to add it again.", "hint");
    }
  };

  document.getElementById("logFilter").oninput = async () => {
    if(selectedJobId){
      const r = await api("/api/jobs/" + selectedJobId);
      updateDetail(await r.json());
    }
  };

  // ===== Keep alive + wake status =====
  async function wake(){
    try{
      const r = await fetch("/healthz");
      if(!r.ok) throw new Error("healthz not ok");
      document.getElementById("wakeDot").className = "dot good";
      document.getElementById("wakeText").textContent = "Online";
    }catch{
      document.getElementById("wakeDot").className = "dot warn";
      document.getElementById("wakeText").textContent = "Warming up…";
    }
  }
  setInterval(wake, 60000);
  wake();

  // Initial
  updateTokenBadge();
  refreshJobs();
  setStatus("", "Idle", "Ready.");
</script>
</body>
</html>
"""


@app.get("/api/jobs")
def list_jobs(req: Request):
    auth(req)
    out = []
    for jid in jobs_order:
        j = jobs.get(jid)
        if not j:
            continue
        out.append({
            "job_id": j.get("job_id", ""),
            "vod_id": j.get("vod_id", ""),
            "status": j.get("status", ""),
            "stage": j.get("stage", ""),
            "started_at": j.get("started_at", 0),
            "finished_at": j.get("finished_at", 0),
        })
    return {"jobs": out}


@app.post("/api/jobs")
async def create_job(req: Request):
    auth(req)
    body = await req.json()

    vod_id = str(body.get("vod_id", "")).strip()
    if not re.fullmatch(r"\d+", vod_id):
        raise HTTPException(400, "vod_id must be numeric")

    quality = str(body.get("quality", "1080p60")).strip()

    # threads
    try:
        threads = int(str(body.get("threads", "2")).strip())
    except Exception:
        raise HTTPException(400, "threads must be a number")
    threads = clamp_int(threads, 1, 4)

    # bandwidth
    bandwidth = None
    bw_raw = str(body.get("bandwidth", "")).strip()
    if bw_raw:
        try:
            bandwidth = int(bw_raw)
        except Exception:
            raise HTTPException(400, "bandwidth must be a number (KiB/s)")
        bandwidth = clamp_int(bandwidth, 64, 20000)

    # beginning/ending (FIX: don't stringify None)
    beginning = normalize_optional_time(body.get("beginning"))
    ending = normalize_optional_time(body.get("ending"))

    include_chat = bool(body.get("include_chat", True))

    # chat settings
    def get_int(name, default, lo, hi):
        try:
            v = int(str(body.get(name, default)).strip())
        except Exception:
            raise HTTPException(400, f"{name} must be a number")
        return clamp_int(v, lo, hi)

    chat_width = get_int("chat_width", 422, 250, 900)
    font_size = get_int("font_size", 18, 10, 52)
    framerate = get_int("framerate", 30, 10, 60)
    try:
        update_rate = float(str(body.get("update_rate", 0.2)).strip())
    except Exception:
        raise HTTPException(400, "update_rate must be a number")
    update_rate = max(0.0, min(2.0, update_rate))

    background_color = normalize_color(str(body.get("background_color", "#111111")))
    outline = str(body.get("outline", "false")).strip().lower() == "true"

    if q.full():
        raise HTTPException(429, "Queue full. Try again later.")

    job_id = uuid.uuid4().hex

    video_path = os.path.join(DOWNLOAD_DIR, f"{vod_id}-{job_id}.video.mp4")
    chat_json = os.path.join(DOWNLOAD_DIR, f"{vod_id}-{job_id}.chat.json.gz")
    chat_mp4 = os.path.join(DOWNLOAD_DIR, f"{vod_id}-{job_id}.chat.mp4")
    final_path = os.path.join(DOWNLOAD_DIR, f"{vod_id}-{job_id}.final.mp4")

    job = {
        "job_id": job_id,
        "vod_id": vod_id,
        "status": "queued",
        "stage": "queued",
        "quality": quality,
        "include_chat": include_chat,
        "path": "",
        "log": "",
        "last_log_line": "",
        "hint": "",
        "error": "",
        "started_at": 0,
        "finished_at": 0,
        "cancel_requested": False,
    }
    add_job(job)

    q.put({
        "job_id": job_id,
        "vod_id": vod_id,
        "quality": quality,
        "threads": threads,
        "bandwidth": bandwidth,
        "beginning": beginning,
        "ending": ending,
        "include_chat": include_chat,
        "video_path": video_path,
        "chat_json": chat_json,
        "chat_mp4": chat_mp4,
        "final_path": final_path,
        "chat_width": chat_width,
        "chat_height": 1080,
        "font_size": font_size,
        "framerate": framerate,
        "update_rate": update_rate,
        "background_color": background_color,
        "outline": outline
    })

    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, req: Request):
    auth(req)
    return get_job_or_404(job_id)


@app.get("/api/jobs/{job_id}/file")
def job_file(job_id: str, req: Request):
    auth(req)
    job = get_job_or_404(job_id)
    if job.get("status") != "done":
        raise HTTPException(409, "not ready")
    path = job.get("path") or ""
    if not path or not os.path.exists(path):
        raise HTTPException(410, "file expired or missing (Render Free /tmp is temporary)")
    return FileResponse(path, filename=os.path.basename(path))


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, req: Request):
    auth(req)
    job = get_job_or_404(job_id)
    job["cancel_requested"] = True
    return {"ok": True}


@app.post("/api/jobs/{job_id}/delete")
def delete_job(job_id: str, req: Request):
    auth(req)
    job = get_job_or_404(job_id)
    # remove any file
    path = job.get("path") or ""
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass
    jobs.pop(job_id, None)
    if job_id in jobs_order:
        jobs_order.remove(job_id)
    return {"ok": True}
