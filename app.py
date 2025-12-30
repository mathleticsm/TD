import os
import re
import time
import uuid
import shlex
import signal
import shutil
import threading
import subprocess
from queue import Queue, Empty
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse

app = FastAPI()

# =========================
# Config (Render-friendly)
# =========================
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

# IMPORTANT: avoid Render's /tmp 2GB temp cap
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/app/storage")
TD_TEMP_DIR = os.environ.get("TD_TEMP_DIR", "/app/tdtmp")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(TD_TEMP_DIR, exist_ok=True)
os.environ.setdefault("TMPDIR", TD_TEMP_DIR)

MAX_QUEUE = int(os.environ.get("MAX_QUEUE", "3"))
MAX_JOBS_IN_MEMORY = int(os.environ.get("MAX_JOBS_IN_MEMORY", "30"))
KEEP_LOG_LINES = int(os.environ.get("KEEP_LOG_LINES", "1200"))

q: "Queue[dict]" = Queue(maxsize=MAX_QUEUE)

jobs: Dict[str, Dict[str, Any]] = {}
jobs_order: List[str] = []  # newest-first


# =========================
# Helpers
# =========================
def auth(req: Request):
    if ADMIN_TOKEN and req.headers.get("X-Admin-Token") != ADMIN_TOKEN:
        raise HTTPException(401, "Missing/invalid X-Admin-Token")


def now_ts() -> int:
    return int(time.time())


def clamp_int(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def disk_info(path: str) -> Dict[str, int]:
    usage = shutil.disk_usage(path)
    return {
        "total_mb": usage.total // (1024 * 1024),
        "used_mb": usage.used // (1024 * 1024),
        "free_mb": usage.free // (1024 * 1024),
    }


def ensure_free_space(path: str, need_free_mb: int) -> None:
    free_mb = disk_info(path)["free_mb"]
    if free_mb < need_free_mb:
        raise RuntimeError(
            f"Not enough free disk space in {path} ({free_mb}MB free). "
            f"Use smaller chunks (e.g., 1 hour) or lower quality."
        )


def normalize_optional_time(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return None
    s = str(value).strip()
    if s == "" or s.lower() in ("none", "null", "undefined"):
        return None
    if not re.fullmatch(r"\d{1,2}:\d{2}:\d{2}", s):
        raise HTTPException(400, "beginning/ending must look like HH:MM:SS (example 02:00:00)")
    return s


def normalize_color(value: str) -> str:
    s = (value or "").strip()
    if not s:
        return "#111111"
    if not re.fullmatch(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{8})", s):
        raise HTTPException(400, "background_color must be like #RRGGBB or #AARRGGBB")
    return s


def cleanup_paths(paths: List[str]) -> None:
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


def cleanup_job_files(job: Dict[str, Any]) -> None:
    paths = list(job.get("files") or [])
    final_path = job.get("path") or ""
    if final_path:
        paths.append(final_path)
    cleanup_paths(paths)
    job["files"] = []
    job["path"] = ""


def add_job(job: Dict[str, Any]) -> None:
    job_id = job["job_id"]
    jobs[job_id] = job
    jobs_order.insert(0, job_id)

    while len(jobs_order) > MAX_JOBS_IN_MEMORY:
        old = jobs_order.pop()
        old_job = jobs.get(old)
        if old_job:
            cleanup_job_files(old_job)
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
        return "Missing ICU. Install `libicu-dev` in Dockerfile and redeploy."
    if "exceeded the limit of 2GB" in log or "temporary storage volume /tmp exceeded" in log.lower():
        return (
            "You hit Render's /tmp 2GB cap. Make sure DOWNLOAD_DIR and TD_TEMP_DIR are NOT under /tmp, "
            "and use chunks (1 hour recommended on Free)."
        )
    if "Quality not found" in log or "Unable to find requested quality" in log:
        return "That quality isn't available for this VOD. Try 1080p (not 1080p60) or Auto."
    if "429" in log or "Too Many Requests" in log:
        return "Twitch rate-limited you. Keep threads=2 and consider bandwidth (e.g., 4000 KiB/s)."
    return ""


def terminate_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=3)
        return
    except Exception:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run_and_log(job: Dict[str, Any], cmd: List[str], stage: str) -> None:
    job["stage"] = stage
    append_log(job, f"\n=== {stage} ===")
    append_log(job, "CMD: " + " ".join(shlex.quote(x) for x in cmd))

    env = os.environ.copy()
    env["TMPDIR"] = TD_TEMP_DIR

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
        start_new_session=True,  # create process group; allows cancel to kill children too
    )
    job["_proc_pid"] = proc.pid

    try:
        if proc.stdout:
            for line in proc.stdout:
                append_log(job, line.rstrip())
                if job.get("cancel_requested"):
                    append_log(job, "Cancel requested: terminating process group…")
                    terminate_process_group(proc)
                    break

        rc = proc.wait()
    finally:
        try:
            if proc.stdout:
                proc.stdout.close()
        except Exception:
            pass

    if job.get("cancel_requested"):
        raise RuntimeError("Cancelled by user")
    if rc != 0:
        raise RuntimeError(f"{stage} failed (exit {rc})")


# =========================
# TwitchDownloaderCLI builders
# =========================
def td_videodownload(
    vod_id: str,
    out_path: str,
    quality: str,
    threads: int,
    bandwidth: Optional[int],
    beginning: Optional[str],
    ending: Optional[str],
) -> List[str]:
    cmd = ["TwitchDownloaderCLI", "videodownload", "--id", vod_id, "-o", out_path]
    if quality:
        cmd += ["--quality", quality]
    cmd += ["--threads", str(threads)]
    if bandwidth is not None:
        cmd += ["--bandwidth", str(bandwidth)]
    if beginning:
        cmd += ["--beginning", beginning]
    if ending:
        cmd += ["--ending", ending]
    cmd += ["--temp-path", TD_TEMP_DIR]
    return cmd


def td_chatdownload(
    vod_id: str,
    out_path: str,
    threads: int,
    beginning: Optional[str],
    ending: Optional[str],
) -> List[str]:
    cmd = [
        "TwitchDownloaderCLI", "chatdownload",
        "--id", vod_id,
        "-o", out_path,
        "--compression", "Gzip",
        "-E",
        "--threads", str(threads),
        "--temp-path", TD_TEMP_DIR,
    ]
    if beginning:
        cmd += ["--beginning", beginning]
    if ending:
        cmd += ["--ending", ending]
    return cmd


def td_chatrender(
    chat_json_path: str,
    out_path: str,
    chat_width: int,
    chat_height: int,
    font_size: int,
    framerate: int,
    update_rate: float,
    background_color: str,
    outline: bool,
) -> List[str]:
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
        "--temp-path", TD_TEMP_DIR,
        "--readable-colors", "true",
    ]
    if outline:
        cmd += ["--outline"]
    return cmd


def ffmpeg_side_by_side(
    video_path: str,
    chat_path: str,
    out_path: str,
    chat_width: int,
    height: int,
    crf: int,
) -> List[str]:
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
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "160k",
        "-movflags", "+faststart",
        out_path
    ]


# =========================
# Worker
# =========================
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
        job["cancel_requested"] = False

        try:
            # Fail fast instead of getting evicted mid-run
            ensure_free_space(DOWNLOAD_DIR, need_free_mb=800)
            ensure_free_space(TD_TEMP_DIR, need_free_mb=200)

            vod_id = payload["vod_id"]
            quality = payload["quality"]
            threads = payload["threads"]
            bandwidth = payload["bandwidth"]
            beginning = payload["beginning"]
            ending = payload["ending"]
            include_chat = payload["include_chat"]

            video_path = payload["video_path"]
            final_path = payload["final_path"]

            run_and_log(job, td_videodownload(vod_id, video_path, quality, threads, bandwidth, beginning, ending), "VideoDownload")

            if include_chat:
                chat_json = payload["chat_json"]
                chat_mp4 = payload["chat_mp4"]

                run_and_log(job, td_chatdownload(vod_id, chat_json, threads, beginning, ending), "ChatDownload")

                run_and_log(
                    job,
                    td_chatrender(
                        chat_json, chat_mp4,
                        payload["chat_width"], payload["chat_height"],
                        payload["font_size"], payload["framerate"], payload["update_rate"],
                        payload["background_color"], payload["outline"]
                    ),
                    "ChatRender"
                )

                run_and_log(
                    job,
                    ffmpeg_side_by_side(video_path, chat_mp4, final_path, payload["chat_width"], payload["chat_height"], payload["crf"]),
                    "Combine (Video + Chat)"
                )

                # cleanup intermediates ASAP
                cleanup_paths([chat_json, chat_mp4, video_path])
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

            # cleanup intermediates on failure
            cleanup_paths(job.get("files") or [])

        finally:
            job["finished_at"] = now_ts()
            job.pop("_proc_pid", None)
            q.task_done()


threading.Thread(target=worker_loop, daemon=True).start()


# =========================
# API
# =========================
@app.get("/healthz")
def healthz():
    return {"ok": True, "ts": now_ts()}


@app.get("/api/system")
def system(req: Request):
    auth(req)
    return {
        "download_dir": DOWNLOAD_DIR,
        "temp_dir": TD_TEMP_DIR,
        "disk_download_dir": disk_info(DOWNLOAD_DIR),
        "disk_temp_dir": disk_info(TD_TEMP_DIR),
        "queue": {"size": q.qsize(), "max": MAX_QUEUE},
        "jobs_in_memory": len(jobs_order),
        "time": now_ts(),
    }


@app.get("/", response_class=HTMLResponse)
def home():
    return f"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>TwitchDownloader – Render Free</title>
  <style>
    :root {{
      --bg0:#070A12;
      --bg1:#0B1020;
      --card:rgba(255,255,255,.06);
      --line:rgba(255,255,255,.12);
      --txt:#EAF0FF;
      --muted:rgba(234,240,255,.65);
      --good:#34d399;
      --warn:#fbbf24;
      --bad:#fb7185;
      --accent:#60a5fa;
      --accent2:#a78bfa;
      --shadow: 0 18px 45px rgba(0,0,0,.45);
      --r:18px;
      --mono: ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,"Liberation Mono","Courier New",monospace;
      --sans: ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,"Apple Color Emoji","Segoe UI Emoji";
    }}

    *{{box-sizing:border-box}}
    body {{
      margin:0;
      font-family:var(--sans);
      color:var(--txt);
      background:
        radial-gradient(1200px 900px at 12% 8%, rgba(96,165,250,.22), transparent 55%),
        radial-gradient(900px 700px at 88% 18%, rgba(167,139,250,.18), transparent 55%),
        radial-gradient(1000px 700px at 50% 110%, rgba(52,211,153,.10), transparent 55%),
        linear-gradient(180deg, var(--bg0), var(--bg1));
      min-height:100vh;
    }}

    .wrap {{ max-width:1200px; margin:26px auto 70px; padding:0 18px; }}
    .top {{
      display:flex; gap:14px; align-items:flex-start; justify-content:space-between;
      margin-bottom:14px;
    }}
    .brand h1 {{
      margin:0; font-size:20px; letter-spacing:.2px; font-weight:800;
    }}
    .brand .sub {{
      margin-top:6px; color:var(--muted); font-size:13px; line-height:1.45;
    }}
    .chips {{
      display:flex; flex-wrap:wrap; gap:10px; justify-content:flex-end;
    }}
    .chip {{
      display:flex; align-items:center; gap:10px;
      padding:10px 12px;
      background:var(--card);
      border:1px solid var(--line);
      border-radius:999px;
      box-shadow:var(--shadow);
      color:var(--muted);
      font-size:12px;
      backdrop-filter: blur(10px);
    }}
    .dot {{
      width:10px; height:10px; border-radius:999px; background:rgba(255,255,255,.35);
      box-shadow: 0 0 0 3px rgba(255,255,255,.06) inset;
    }}
    .dot.good{{ background:var(--good); }}
    .dot.warn{{ background:var(--warn); }}
    .dot.bad{{ background:var(--bad); }}

    .grid {{
      display:grid;
      gap:14px;
      grid-template-columns: 1fr;
    }}
    @media(min-width:1020px) {{
      .grid {{ grid-template-columns: 1.08fr .92fr; align-items:start; }}
    }}

    .card {{
      background: linear-gradient(180deg, rgba(255,255,255,.08), rgba(255,255,255,.02));
      border:1px solid var(--line);
      border-radius: var(--r);
      box-shadow: var(--shadow);
      overflow:hidden;
      backdrop-filter: blur(10px);
    }}
    .hd {{
      padding:14px 14px;
      border-bottom:1px solid rgba(255,255,255,.10);
      display:flex;
      gap:10px;
      align-items:center;
      justify-content:space-between;
      background: rgba(0,0,0,.14);
    }}
    .hd h2 {{
      margin:0; font-size:13px; letter-spacing:.28px; text-transform:uppercase;
      color: rgba(234,240,255,.82);
    }}
    .bd {{ padding:14px; }}

    .row {{ display:flex; gap:10px; flex-wrap:wrap; }}
    .f {{ flex:1; min-width:220px; }}
    label {{ display:block; margin:0 0 6px; font-size:12px; color:var(--muted); }}

    input, select, button, textarea {{
      width:100%;
      font:inherit;
      color:var(--txt);
      background: rgba(0,0,0,.18);
      border:1px solid rgba(255,255,255,.14);
      border-radius: 14px;
      padding:10px 12px;
      outline:none;
      transition: border-color .15s ease, transform .08s ease;
    }}
    input:focus, select:focus, textarea:focus {{
      border-color: rgba(96,165,250,.6);
      box-shadow: 0 0 0 4px rgba(96,165,250,.10);
    }}

    button {{
      cursor:pointer;
      font-weight:800;
      letter-spacing:.2px;
      background: linear-gradient(180deg, rgba(96,165,250,.30), rgba(96,165,250,.16));
      border-color: rgba(96,165,250,.45);
    }}
    button:hover {{ transform: translateY(-1px); }}
    button:active {{ transform: translateY(0px); }}

    .btn2 {{
      background: rgba(0,0,0,.18);
      border-color: rgba(255,255,255,.14);
      font-weight:750;
    }}
    .btnBad {{
      background: linear-gradient(180deg, rgba(251,113,133,.28), rgba(251,113,133,.12));
      border-color: rgba(251,113,133,.45);
    }}
    .btnGood {{
      background: linear-gradient(180deg, rgba(52,211,153,.25), rgba(52,211,153,.12));
      border-color: rgba(52,211,153,.45);
    }}

    .status {{
      display:flex; align-items:center; gap:10px; flex-wrap:wrap;
      padding:12px;
      border-radius: 16px;
      border:1px solid rgba(255,255,255,.12);
      background: rgba(0,0,0,.16);
      margin-bottom: 12px;
    }}
    .status .title {{ font-weight:900; }}
    .status .sub {{ color:var(--muted); font-size:12px; line-height:1.35; }}
    .badge {{
      margin-left:auto;
      font-size:12px;
      padding:5px 10px;
      border-radius:999px;
      border:1px solid rgba(255,255,255,.14);
      color: rgba(234,240,255,.75);
      background: rgba(0,0,0,.12);
    }}
    .badge.good{{ border-color: rgba(52,211,153,.55); color: rgba(52,211,153,.92); }}
    .badge.warn{{ border-color: rgba(251,191,36,.60); color: rgba(251,191,36,.95); }}
    .badge.bad{{ border-color: rgba(251,113,133,.60); color: rgba(251,113,133,.95); }}

    details {{
      border:1px solid rgba(255,255,255,.12);
      border-radius: 16px;
      padding: 10px 12px;
      background: rgba(0,0,0,.14);
    }}
    summary {{
      cursor:pointer;
      font-weight:900;
      letter-spacing:.15px;
    }}
    .muted {{ color:var(--muted); font-size:12.5px; line-height:1.5; }}

    .toggle {{
      display:flex; align-items:flex-start; gap:12px;
      padding: 12px;
      border-radius: 16px;
      border:1px solid rgba(255,255,255,.12);
      background: rgba(0,0,0,.14);
      user-select:none;
      margin-top:10px;
    }}
    .toggle input {{
      width:auto;
      transform: translateY(2px) scale(1.15);
      accent-color: var(--accent);
    }}

    .jobs {{ display:flex; flex-direction:column; gap:10px; }}
    .job {{
      padding: 12px;
      border-radius: 16px;
      border:1px solid rgba(255,255,255,.12);
      background: rgba(0,0,0,.14);
      cursor:pointer;
      transition: border-color .15s ease, transform .08s ease;
    }}
    .job:hover {{ border-color: rgba(96,165,250,.55); transform: translateY(-1px); }}
    .job.active {{ border-color: rgba(96,165,250,.85); box-shadow: 0 0 0 4px rgba(96,165,250,.10); }}
    .jobTop {{ display:flex; justify-content:space-between; gap:10px; align-items:center; }}
    .jobTitle {{ font-weight:950; }}
    .jobMeta {{ color:var(--muted); font-size:12px; margin-top:6px; }}
    .mono {{ font-family: var(--mono); }}
    pre {{
      margin:0;
      border-radius: 16px;
      padding: 12px;
      background: rgba(0,0,0,.22);
      border:1px solid rgba(255,255,255,.12);
      overflow:auto;
      max-height: 440px;
      font-size: 12px;
      line-height: 1.45;
    }}

    .msg {{
      margin-top: 10px;
      padding: 10px 12px;
      border-radius: 16px;
      border:1px solid rgba(255,255,255,.12);
      background: rgba(0,0,0,.14);
      font-size: 12.5px;
      line-height: 1.45;
    }}
    .msg.ok {{
      border-color: rgba(52,211,153,.38);
      background: rgba(52,211,153,.08);
      color: rgba(52,211,153,.95);
    }}
    .msg.err {{
      border-color: rgba(251,113,133,.38);
      background: rgba(251,113,133,.08);
      color: rgba(251,113,133,.95);
    }}
    .msg.hint {{
      border-color: rgba(251,191,36,.38);
      background: rgba(251,191,36,.07);
      color: rgba(251,191,36,.95);
    }}

    .actions {{
      display:flex;
      gap:10px;
      flex-wrap:wrap;
      margin-top: 10px;
    }}
    .actions > button {{ flex:1; min-width:220px; }}

    .tiny {{
      font-size: 11.5px;
      color: rgba(234,240,255,.55);
    }}
    .hr {{
      height:1px;
      background: rgba(255,255,255,.10);
      margin: 12px 0;
    }}
  </style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div class="brand">
      <h1>TwitchDownloader – Render Free</h1>
      <div class="sub">
        1080p video + optional chat render. For long VODs, use chunks (1h recommended on Free).<br/>
        Storage dirs: <span class="mono">{DOWNLOAD_DIR}</span> • temp: <span class="mono">{TD_TEMP_DIR}</span>
      </div>
    </div>

    <div class="chips">
      <div class="chip">
        <span id="wakeDot" class="dot warn"></span>
        <div>
          <div style="font-weight:900;color:rgba(234,240,255,.85)">Service</div>
          <div class="tiny" id="wakeText">Warming up…</div>
        </div>
      </div>
      <div class="chip">
        <span id="sysDot" class="dot warn"></span>
        <div>
          <div style="font-weight:900;color:rgba(234,240,255,.85)">Disk Free</div>
          <div class="tiny" id="diskText">—</div>
        </div>
      </div>
      <div class="chip">
        <span class="dot" style="background:rgba(255,255,255,.35)"></span>
        <div>
          <div style="font-weight:900;color:rgba(234,240,255,.85)">Queue</div>
          <div class="tiny" id="queueText">—</div>
        </div>
      </div>
    </div>
  </div>

  <div class="grid">
    <!-- Left -->
    <section class="card">
      <div class="hd">
        <h2>Create Job</h2>
        <div style="display:flex;gap:10px">
          <button class="btn2" id="btnToken">Set Token</button>
        </div>
      </div>
      <div class="bd">

        <div class="status">
          <span id="statusDot" class="dot"></span>
          <div style="flex:1">
            <div class="title" id="statusText">Idle</div>
            <div class="sub" id="statusSub">Ready.</div>
          </div>
          <div class="badge warn" id="tokenBadge">token: not set</div>
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
            <label>Threads (recommended: 2)</label>
            <input id="threads" value="2" />
          </div>
          <div class="f">
            <label>Bandwidth KiB/s per thread (optional)</label>
            <input id="bandwidth" placeholder="e.g. 4000" />
          </div>
        </div>

        <details style="margin-top:10px" open>
          <summary>Chunking (Begin / End)</summary>
          <div class="muted" style="margin:10px 0 12px">
            Tip: On Render Free, use <b>1-hour chunks</b> for 1080p to avoid storage limits.
          </div>
          <div class="row">
            <div class="f">
              <label>Begin (HH:MM:SS)</label>
              <input id="begin" placeholder="00:00:00" />
            </div>
            <div class="f">
              <label>End (HH:MM:SS)</label>
              <input id="end" placeholder="01:00:00" />
            </div>
          </div>
          <div class="row" style="margin-top:10px">
            <div class="f">
              <label>Chunk size</label>
              <select id="chunkSize">
                <option value="60" selected>1 hour (recommended)</option>
                <option value="90">1h 30m</option>
                <option value="120">2 hours</option>
              </select>
            </div>
            <div class="f">
              <label>Quick actions</label>
              <div class="row">
                <button class="btn2" id="btnSetStart">Set 0 → chunk</button>
                <button class="btn2" id="btnNextChunk">Next chunk</button>
              </div>
            </div>
          </div>
        </details>

        <div class="toggle">
          <input id="include_chat" type="checkbox" checked />
          <div>
            <div style="font-weight:950">Render chat + combine</div>
            <div class="muted">Side-by-side chat panel. Chat render settings below.</div>
          </div>
        </div>

        <details style="margin-top:10px" id="chatSettings">
          <summary>Chat render settings (smaller defaults)</summary>
          <div class="row" style="margin-top:10px">
            <div class="f">
              <label>Chat width (px)</label>
              <input id="chat_width" value="420" />
            </div>
            <div class="f">
              <label>Font size</label>
              <input id="font_size" value="18" />
            </div>
          </div>
          <div class="row">
            <div class="f">
              <label>Framerate</label>
              <input id="framerate" value="24" />
            </div>
            <div class="f">
              <label>Update rate (seconds)</label>
              <input id="update_rate" value="0.25" />
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

          <div class="row" style="margin-top:10px">
            <div class="f">
              <label>Final encode size (CRF)</label>
              <select id="crf">
                <option value="23" selected>23 (smaller – recommended)</option>
                <option value="21">21 (bigger)</option>
                <option value="18">18 (very big)</option>
              </select>
              <div class="tiny" style="margin-top:6px">Lower CRF = higher quality + bigger files.</div>
            </div>
            <div class="f">
              <label>Preset</label>
              <div class="tiny" style="margin-top:10px">Using ffmpeg preset: <span class="mono">veryfast</span></div>
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

    <!-- Right -->
    <section class="card">
      <div class="hd">
        <h2>Jobs & Logs</h2>
        <div style="display:flex;gap:10px">
          <button class="btn2" id="btnRefresh">Refresh</button>
          <button class="btn2" id="btnClearToken">Clear Token</button>
        </div>
      </div>
      <div class="bd">
        <div class="muted">
          Click a job to view. Logs show the last ~300 lines. Use the filter to find errors.
        </div>

        <div class="hr"></div>

        <div class="jobs" id="jobsList"></div>

        <div class="hr"></div>

        <div class="row">
          <div class="f">
            <label>Log filter</label>
            <input id="logFilter" placeholder="type to filter log lines…" />
          </div>
          <div class="f">
            <label>Log actions</label>
            <div class="row">
              <button class="btn2" id="btnCopyLog">Copy</button>
              <button class="btn2" id="btnDownloadLog">Download</button>
            </div>
          </div>
        </div>

        <div style="margin-top:10px">
          <pre id="logBox" class="mono">Select a job to see logs.</pre>
        </div>

        <div id="jobHint"></div>
        <div id="jobError"></div>
        <div id="jobOk"></div>

        <div class="actions">
          <button class="btn2" id="btnDownload" disabled>Download MP4</button>
          <button class="btn2" id="btnDelete" disabled>Delete Job</button>
        </div>
      </div>
    </section>

  </div>
</div>

<script>
  // ========= Token =========
  const TOKEN_KEY = "td_admin_token";
  function getToken() {{ return localStorage.getItem(TOKEN_KEY) || ""; }}
  function setToken(t) {{ localStorage.setItem(TOKEN_KEY, t); updateTokenBadge(); }}
  function clearToken() {{ localStorage.removeItem(TOKEN_KEY); updateTokenBadge(); }}

  function updateTokenBadge() {{
    const badge = document.getElementById("tokenBadge");
    const t = getToken();
    badge.textContent = t ? ("token: set (" + t.slice(0,3) + "…" + t.slice(-3) + ")") : "token: not set";
    badge.className = "badge " + (t ? "good" : "warn");
  }}

  document.getElementById("btnToken").onclick = () => {{
    const existing = getToken();
    const t = prompt("Enter ADMIN token (stored in this browser).", existing || "");
    if (t === null) return;
    const trimmed = t.trim();
    if (!trimmed) {{ clearToken(); return; }}
    setToken(trimmed);
  }};

  document.getElementById("btnClearToken").onclick = () => {{
    if (confirm("Clear saved token from this browser?")) {{
      clearToken();
      showMsg("Token cleared.", "hint");
    }}
  }};

  // ========= UI helpers =========
  function setStatus(kind, title, sub) {{
    const dot = document.getElementById("statusDot");
    dot.className = "dot " + (kind || "");
    document.getElementById("statusText").textContent = title || "";
    document.getElementById("statusSub").textContent = sub || "";
  }}

  function showMsg(text, cls) {{
    const m = document.getElementById("msgArea");
    m.innerHTML = '<div class="msg ' + (cls||"") + '">' + text + '</div>';
  }}

  function clearMsg() {{ document.getElementById("msgArea").innerHTML = ""; }}

  function escapeHtml(s) {{
    return (s||"").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");
  }}

  function fmtElapsed(job) {{
    const s = job.started_at || 0;
    const f = job.finished_at || 0;
    if (!s) return "";
    const end = f ? f : Math.floor(Date.now()/1000);
    const sec = Math.max(0, end - s);
    const h = Math.floor(sec/3600);
    const m = Math.floor((sec%3600)/60);
    const r = sec%60;
    const mm = String(m).padStart(2,"0");
    const rr = String(r).padStart(2,"0");
    return (h>0 ? (h+":"+mm+":"+rr) : (m+":"+rr));
  }}

  // ========= Chunk helpers =========
  function toSeconds(hms) {{
    const parts = hms.split(":").map(x => parseInt(x,10));
    if (parts.length !== 3) return 0;
    return parts[0]*3600 + parts[1]*60 + parts[2];
  }}
  function fromSeconds(sec) {{
    const h = Math.floor(sec/3600);
    const m = Math.floor((sec%3600)/60);
    const s = sec%60;
    return String(h).padStart(2,"0")+":"+String(m).padStart(2,"0")+":"+String(s).padStart(2,"0");
  }}
  function chunkMinutes() {{
    return parseInt(document.getElementById("chunkSize").value, 10) || 60;
  }}

  document.getElementById("btnSetStart").onclick = () => {{
    const mins = chunkMinutes();
    document.getElementById("begin").value = "00:00:00";
    document.getElementById("end").value = fromSeconds(mins*60);
  }};
  document.getElementById("btnNextChunk").onclick = () => {{
    const mins = chunkMinutes();
    const b = document.getElementById("begin").value.trim() || "00:00:00";
    const e = document.getElementById("end").value.trim() || fromSeconds(mins*60);
    const b2 = toSeconds(b) + mins*60;
    const e2 = toSeconds(e) + mins*60;
    document.getElementById("begin").value = fromSeconds(b2);
    document.getElementById("end").value = fromSeconds(e2);
  }};

  // ========= Chat toggle =========
  const includeChatEl = document.getElementById("include_chat");
  function syncChatSettings() {{
    document.getElementById("chatSettings").style.display = includeChatEl.checked ? "block" : "none";
  }}
  includeChatEl.onchange = syncChatSettings;
  syncChatSettings();

  // ========= API =========
  async function api(path, opts={{}}) {{
    const t = getToken();
    if (!t) throw new Error("Token not set. Click Set Token.");
    const headers = Object.assign({{}}, opts.headers || {{}}, {{"X-Admin-Token": t}});
    return fetch(path, Object.assign({{}}, opts, {{ headers }}));
  }}

  // ========= Jobs =========
  let selectedJobId = null;
  let pollTimer = null;

  function badgeForStatus(st) {{
    if (st==="done") return "badge good";
    if (st==="error") return "badge bad";
    if (st==="running") return "badge warn";
    return "badge";
  }}

  function renderJobs(arr) {{
    const list = document.getElementById("jobsList");
    if (!arr || !arr.length) {{
      list.innerHTML = '<div class="muted">No jobs yet.</div>';
      return;
    }}
    list.innerHTML = arr.map(j => {{
      const active = (j.job_id === selectedJobId) ? "active" : "";
      const el = fmtElapsed(j) || "—";
      const stage = j.stage || "";
      return (
        '<div class="job ' + active + '" onclick="selectJob(\\'' + j.job_id + '\\')">' +
          '<div class="jobTop">' +
            '<div>' +
              '<div class="jobTitle">VOD ' + j.vod_id + '</div>' +
              '<div class="jobMeta">Stage: ' + stage + ' • Elapsed: ' + el + '</div>' +
            '</div>' +
            '<div class="' + badgeForStatus(j.status) + '">' + j.status + '</div>' +
          '</div>' +
          '<div class="jobMeta mono" style="margin-top:8px">' + j.job_id + '</div>' +
        '</div>'
      );
    }}).join("");
  }}

  async function refreshJobs() {{
    const r = await api("/api/jobs");
    const data = await r.json();
    renderJobs(data.jobs || []);
    if (selectedJobId) await selectJob(selectedJobId, true);
  }}

  window.selectJob = async function(jobId, silent=false) {{
    selectedJobId = jobId;
    if (!silent) clearMsg();
    document.getElementById("btnDownload").disabled = true;
    document.getElementById("btnDelete").disabled = true;
    document.getElementById("btnCancel").disabled = true;

    const r = await api("/api/jobs/" + jobId);
    const job = await r.json();

    updateDetail(job);
    await refreshJobs();

    startPollingIfNeeded(job);
  }}

  function updateDetail(job) {{
    const st = job.status || "unknown";
    if (st === "running") setStatus("warn", "Running", "Stage: " + (job.stage||""));
    else if (st === "done") setStatus("good", "Done", "Ready to download.");
    else if (st === "error") setStatus("bad", "Error", job.error || "Failed.");
    else setStatus("", "Idle", "Ready.");

    const filter = document.getElementById("logFilter").value.trim().toLowerCase();
    const lines = (job.log || "").split("\\n");
    const filtered = filter ? lines.filter(x => x.toLowerCase().includes(filter)) : lines;
    const tail = filtered.slice(-300).join("\\n");
    document.getElementById("logBox").textContent = tail || "(no log yet)";

    const hint = job.hint || "";
    document.getElementById("jobHint").innerHTML = hint ? '<div class="msg hint"><b>Hint:</b> ' + escapeHtml(hint) + '</div>' : "";
    document.getElementById("jobError").innerHTML = (st==="error" && job.error) ? '<div class="msg err"><b>Error:</b> ' + escapeHtml(job.error) + '</div>' : "";
    document.getElementById("jobOk").innerHTML = (st==="done") ? '<div class="msg ok"><b>Done:</b> Click Download MP4.</div>' : "";

    document.getElementById("btnDownload").disabled = (st !== "done");
    document.getElementById("btnDelete").disabled = false;
    document.getElementById("btnCancel").disabled = (st !== "running");
  }}

  function startPollingIfNeeded(job) {{
    if (pollTimer) clearInterval(pollTimer);
    if (job.status === "running" || job.status === "queued") {{
      pollTimer = setInterval(async () => {{
        try {{
          const r = await api("/api/jobs/" + selectedJobId);
          const j = await r.json();
          updateDetail(j);
          const listData = await (await api("/api/jobs")).json();
          renderJobs(listData.jobs || []);
          if (j.status !== "running" && j.status !== "queued") {{
            clearInterval(pollTimer);
            pollTimer = null;
          }}
        }} catch(e) {{
          // ignore transient
        }}
      }}, 2500);
    }}
  }}

  // ========= Create job =========
  document.getElementById("btnStart").onclick = async () => {{
    clearMsg();
    const vod = document.getElementById("vod").value.trim();
    if (!/^[0-9]+$/.test(vod)) return showMsg("VOD ID must be numbers only.", "err");

    const payload = {{
      vod_id: vod,
      quality: document.getElementById("quality").value,
      threads: document.getElementById("threads").value.trim(),
      bandwidth: document.getElementById("bandwidth").value.trim(),
      beginning: document.getElementById("begin").value.trim(),
      ending: document.getElementById("end").value.trim(),
      include_chat: document.getElementById("include_chat").checked,

      chat_width: document.getElementById("chat_width").value.trim(),
      font_size: document.getElementById("font_size").value.trim(),
      framerate: document.getElementById("framerate").value.trim(),
      update_rate: document.getElementById("update_rate").value.trim(),
      background_color: document.getElementById("bg").value.trim(),
      outline: document.getElementById("outline").value,
      crf: document.getElementById("crf").value
    }};

    try {{
      setStatus("warn", "Queueing…", "Creating job…");
      const r = await api("/api/jobs", {{
        method: "POST",
        headers: {{"Content-Type":"application/json"}},
        body: JSON.stringify(payload)
      }});
      const txt = await r.text();
      if (!r.ok) {{
        let msg = txt;
        try {{
          const j = JSON.parse(txt);
          msg = j.detail || txt;
        }} catch(_) {{}}
        setStatus("bad", "Error", msg);
        return showMsg(escapeHtml(msg), "err");
      }}
      const data = JSON.parse(txt);
      showMsg("Job created: <span class='mono'>" + data.job_id + "</span>", "ok");
      await refreshJobs();
      await selectJob(data.job_id);
    }} catch (e) {{
      setStatus("bad", "Error", e.message);
      showMsg(escapeHtml(e.message), "err");
    }}
  }};

  // ========= Actions =========
  document.getElementById("btnCancel").onclick = async () => {{
    if (!selectedJobId) return;
    try {{
      await api("/api/jobs/" + selectedJobId + "/cancel", {{ method: "POST" }});
      showMsg("Cancel requested.", "hint");
      await selectJob(selectedJobId, true);
    }} catch(e) {{
      showMsg("Cancel failed: " + escapeHtml(e.message), "err");
    }}
  }};

  document.getElementById("btnDelete").onclick = async () => {{
    if (!selectedJobId) return;
    try {{
      await api("/api/jobs/" + selectedJobId + "/delete", {{ method: "POST" }});
      selectedJobId = null;
      document.getElementById("logBox").textContent = "Select a job to see logs.";
      document.getElementById("jobHint").innerHTML = "";
      document.getElementById("jobError").innerHTML = "";
      document.getElementById("jobOk").innerHTML = "";
      document.getElementById("btnDownload").disabled = true;
      document.getElementById("btnCancel").disabled = true;
      document.getElementById("btnDelete").disabled = true;
      showMsg("Job deleted.", "ok");
      await refreshJobs();
      setStatus("", "Idle", "Ready.");
    }} catch(e) {{
      showMsg("Delete failed: " + escapeHtml(e.message), "err");
    }}
  }};

  document.getElementById("btnDownload").onclick = () => {{
    if (!selectedJobId) return;
    window.location.href = "/api/jobs/" + selectedJobId + "/file";
  }};

  document.getElementById("btnCopyLog").onclick = async () => {{
    const t = document.getElementById("logBox").textContent;
    await navigator.clipboard.writeText(t);
    showMsg("Copied log to clipboard.", "ok");
  }};

  document.getElementById("btnDownloadLog").onclick = () => {{
    const t = document.getElementById("logBox").textContent;
    const blob = new Blob([t], {{type:"text/plain"}});
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = (selectedJobId ? selectedJobId : "log") + ".txt";
    a.click();
    URL.revokeObjectURL(a.href);
  }};

  document.getElementById("btnRefresh").onclick = async () => {{
    try {{
      await refreshJobs();
      showMsg("Refreshed.", "ok");
    }} catch(e) {{
      showMsg("Refresh failed: " + escapeHtml(e.message), "err");
    }}
  }};

  document.getElementById("logFilter").oninput = async () => {{
    if (!selectedJobId) return;
    try {{
      const r = await api("/api/jobs/" + selectedJobId);
      updateDetail(await r.json());
    }} catch(e) {{}}
  }};

  // ========= Health + System metrics =========
  async function wakePing() {{
    try {{
      const r = await fetch("/healthz");
      if (!r.ok) throw new Error("healthz not ok");
      document.getElementById("wakeDot").className = "dot good";
      document.getElementById("wakeText").textContent = "Online";
    }} catch(_) {{
      document.getElementById("wakeDot").className = "dot warn";
      document.getElementById("wakeText").textContent = "Warming up…";
    }}
  }}

  async function sysPing() {{
    const dot = document.getElementById("sysDot");
    const diskText = document.getElementById("diskText");
    const queueText = document.getElementById("queueText");

    try {{
      const t = getToken();
      if (!t) {{
        dot.className = "dot warn";
        diskText.textContent = "— (set token)";
        queueText.textContent = "—";
        return;
      }}
      const r = await api("/api/system");
      const s = await r.json();

      const free = s.disk_download_dir?.free_mb ?? 0;
      const total = s.disk_download_dir?.total_mb ?? 0;
      const qsz = s.queue?.size ?? 0;
      const qmax = s.queue?.max ?? 0;

      diskText.textContent = free + "MB / " + total + "MB";
      queueText.textContent = qsz + " / " + qmax;

      if (free < 500) dot.className = "dot bad";
      else if (free < 900) dot.className = "dot warn";
      else dot.className = "dot good";
    }} catch(e) {{
      dot.className = "dot warn";
      diskText.textContent = "—";
      queueText.textContent = "—";
    }}
  }}

  // ========= Init =========
  updateTokenBadge();
  setStatus("", "Idle", "Ready.");
  wakePing();
  setInterval(wakePing, 60000);

  sysPing();
  setInterval(sysPing, 20000);

  (async () => {{
    try {{
      if (getToken()) {{
        await refreshJobs();
      }}
    }} catch(e) {{}}
  }})();
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

    beginning = normalize_optional_time(body.get("beginning"))
    ending = normalize_optional_time(body.get("ending"))
    include_chat = bool(body.get("include_chat", True))

    def get_int(name, default, lo, hi):
        try:
            v = int(str(body.get(name, default)).strip())
        except Exception:
            raise HTTPException(400, f"{name} must be a number")
        return clamp_int(v, lo, hi)

    chat_width = get_int("chat_width", 420, 250, 900)
    font_size = get_int("font_size", 18, 10, 52)
    framerate = get_int("framerate", 24, 10, 60)

    try:
        update_rate = float(str(body.get("update_rate", 0.25)).strip())
    except Exception:
        raise HTTPException(400, "update_rate must be a number")
    update_rate = max(0.05, min(2.0, update_rate))

    background_color = normalize_color(str(body.get("background_color", "#111111")))
    outline = str(body.get("outline", "false")).strip().lower() == "true"

    try:
        crf = int(str(body.get("crf", "23")).strip())
    except Exception:
        raise HTTPException(400, "crf must be a number")
    crf = clamp_int(crf, 18, 28)

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
        "files": [video_path, chat_json, chat_mp4, final_path],
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
        "outline": outline,
        "crf": crf,
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
        raise HTTPException(410, "file expired or missing (instance restarted / ephemeral storage)")
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
    cleanup_job_files(job)
    jobs.pop(job_id, None)
    if job_id in jobs_order:
        jobs_order.remove(job_id)
    return {"ok": True}
