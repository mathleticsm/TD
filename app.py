import os, re, time, uuid, threading, subprocess, shlex
from queue import Queue, Empty
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse

app = FastAPI()

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
DOWNLOAD_DIR = "/tmp/downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

q: "Queue[dict]" = Queue(maxsize=3)  # small queue for Free plan
jobs = {}  # job_id -> job dict

def auth(req: Request):
    if ADMIN_TOKEN and req.headers.get("X-Admin-Token") != ADMIN_TOKEN:
        raise HTTPException(401, "Missing/invalid X-Admin-Token")

def append_log(job: dict, line: str):
    lines = (job.get("log") or "").splitlines()
    lines.append(line)
    if len(lines) > 350:
        lines = lines[-350:]
    job["log"] = "\n".join(lines)

def run_and_log(job: dict, cmd: list[str], stage: str):
    job["stage"] = stage
    append_log(job, f"\n=== {stage} ===")
    append_log(job, "CMD: " + " ".join(shlex.quote(x) for x in cmd))

    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in p.stdout:
        append_log(job, line.rstrip())
    rc = p.wait()
    if rc != 0:
        raise RuntimeError(f"{stage} failed (exit {rc})")

def build_td_videodownload(vod_id: str, out_path: str, quality: str, threads: int,
                           bandwidth: int | None, beginning: str | None, ending: str | None):
    # videodownload args: --quality, --threads, --bandwidth, --beginning, --ending, --temp-path :contentReference[oaicite:4]{index=4}
    cmd = ["TwitchDownloaderCLI", "videodownload", "--id", vod_id, "-o", out_path, "--quality", quality]
    cmd += ["--threads", str(threads)]
    if bandwidth is not None:
        cmd += ["--bandwidth", str(bandwidth)]
    if beginning:
        cmd += ["--beginning", beginning]
    if ending:
        cmd += ["--ending", ending]
    cmd += ["--temp-path", "/tmp"]
    return cmd

def build_td_chatdownload(vod_id: str, out_path: str, threads: int,
                          beginning: str | None, ending: str | None):
    # chatdownload args: -o supports .json/.html/.txt, --compression, -E embed images, --threads :contentReference[oaicite:5]{index=5}
    cmd = ["TwitchDownloaderCLI", "chatdownload", "--id", vod_id, "-o", out_path,
           "--compression", "Gzip", "-E", "--threads", str(threads), "--temp-path", "/tmp"]
    if beginning:
        cmd += ["--beginning", beginning]
    if ending:
        cmd += ["--ending", ending]
    return cmd

def build_td_chatrender(chat_json_path: str, out_path: str, chat_width: int, chat_height: int,
                        font_size: int, framerate: int, update_rate: float,
                        background_color: str, outline: bool):
    # chatrender args: -i/-o, -w/-h, --font-size, --framerate, --update-rate, --background-color, --outline :contentReference[oaicite:6]{index=6}
    cmd = ["TwitchDownloaderCLI", "chatrender",
           "-i", chat_json_path,
           "-o", out_path,
           "-w", str(chat_width),
           "-h", str(chat_height),
           "--font-size", str(font_size),
           "--framerate", str(framerate),
           "--update-rate", str(update_rate),
           "--background-color", background_color,
           "--temp-path", "/tmp",
           "--readable-colors", "true"]
    if outline:
        cmd += ["--outline"]
    return cmd

def ffmpeg_side_by_side(video_path: str, chat_path: str, out_path: str, chat_width: int, height: int):
    # Make a wider final video: 1920 + chat_width, height 1080
    # (Most reliable vs transparency overlay on Free)
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
        "-map", "0:a?",               # include audio if present
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "18",
        "-c:a", "aac",
        "-b:a", "160k",
        "-movflags", "+faststart",
        out_path
    ]

def worker_loop():
    while True:
        try:
            payload = q.get(timeout=1)
        except Empty:
            continue

        job_id = payload["job_id"]
        job = jobs[job_id]
        job["status"] = "running"
        job["started_at"] = int(time.time())
        job["error"] = ""

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

            # 1) Video download (1080p requested; if not found, TD downloads highest available) :contentReference[oaicite:7]{index=7}
            run_and_log(job, build_td_videodownload(vod_id, video_path, quality, threads, bandwidth, beginning, ending),
                        "VideoDownload")

            if include_chat:
                chat_json = payload["chat_json"]
                chat_mp4 = payload["chat_mp4"]
                chat_width = payload["chat_width"]
                chat_height = payload["chat_height"]
                font_size = payload["font_size"]
                framerate = payload["framerate"]
                update_rate = payload["update_rate"]
                background_color = payload["background_color"]
                outline = payload["outline"]

                # 2) Chat download
                run_and_log(job, build_td_chatdownload(vod_id, chat_json, threads, beginning, ending), "ChatDownload")

                # 3) Chat render
                run_and_log(job, build_td_chatrender(chat_json, chat_mp4, chat_width, chat_height,
                                                     font_size, framerate, update_rate,
                                                     background_color, outline),
                            "ChatRender")

                # 4) Combine side-by-side
                run_and_log(job, ffmpeg_side_by_side(video_path, chat_mp4, final_path, chat_width, chat_height),
                            "Combine (Video + Chat)")

                # Clean up intermediates to save /tmp space (Free plan)
                for p in (chat_json, chat_mp4, video_path):
                    try:
                        os.remove(p)
                    except:
                        pass
            else:
                # Video-only: final is the downloaded mp4
                os.replace(video_path, final_path)

            job["status"] = "done"
            job["stage"] = "done"
            job["path"] = final_path
            job["finished_at"] = int(time.time())

        except Exception as e:
            job["status"] = "error"
            job["stage"] = "failed"
            job["error"] = str(e)
            job["finished_at"] = int(time.time())

        q.task_done()

threading.Thread(target=worker_loop, daemon=True).start()

@app.get("/", response_class=HTMLResponse)
def home():
    return """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>TwitchDownloader (Render Free)</title>
  <style>
    body{font-family:system-ui,sans-serif;max-width:980px;margin:40px auto;padding:0 16px}
    input,select,button,label{font-size:16px}
    input,select,button{padding:10px}
    pre{background:#f6f6f6;padding:12px;border-radius:10px;white-space:pre-wrap}
    .row{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0}
    .row > *{flex:1;min-width:200px}
    .small{font-size:14px;color:#555}
  </style>
</head>
<body>
  <h1>TwitchDownloader (Render Free)</h1>
  <p class="small">
    Keep this page open while it runs (Render Free spins down after 15 min without inbound traffic).
  </p>

  <div class="row">
    <input id="vod" placeholder="VOD ID (numbers)">
    <select id="quality">
      <option value="1080p60" selected>1080p60 (try)</option>
      <option value="1080p">1080p (try)</option>
    </select>
    <input id="threads" value="2" placeholder="Threads (2 recommended)">
    <input id="bandwidth" placeholder="Bandwidth KiB/s per thread (blank = no limit)">
  </div>

  <div class="row">
    <input id="begin" placeholder="Begin (optional) e.g. 00:00:00">
    <input id="end" placeholder="End (optional) e.g. 02:00:00">
    <label style="display:flex;align-items:center;gap:8px;flex:1;min-width:240px;">
      <input id="include_chat" type="checkbox" checked>
      Render chat + combine (side-by-side)
    </label>
    <button onclick="start()">Start</button>
  </div>

  <div class="row">
    <input id="chat_width" value="422" placeholder="Chat width (px)">
    <input id="font_size" value="18" placeholder="Chat font size">
    <input id="framerate" value="30" placeholder="Chat framerate">
    <input id="update_rate" value="0.2" placeholder="Chat update rate (sec)">
  </div>

  <div class="row">
    <input id="bg" value="#111111" placeholder="Chat background color (#RRGGBB or #AARRGGBB)">
    <label style="display:flex;align-items:center;gap:8px;">
      <input id="outline" type="checkbox">
      Outline
    </label>
  </div>

  <h3>Status</h3>
  <pre id="out">Idle.</pre>

<script>
let jobId=null;
function token(){ return localStorage.getItem("td_token") || ""; }

async function start(){
  if(!token()){
    const x = prompt("Enter ADMIN TOKEN (stored locally in your browser):");
    if(!x) return;
    localStorage.setItem("td_token", x);
  }

  const vod = document.getElementById("vod").value.trim();
  if(!/^[0-9]+$/.test(vod)) return alert("VOD ID must be numeric.");

  const payload = {
    vod_id: vod,
    quality: document.getElementById("quality").value,
    threads: document.getElementById("threads").value.trim(),
    bandwidth: document.getElementById("bandwidth").value.trim(),
    beginning: document.getElementById("begin").value.trim() || null,
    ending: document.getElementById("end").value.trim() || null,
    include_chat: document.getElementById("include_chat").checked,
    chat_width: document.getElementById("chat_width").value.trim(),
    font_size: document.getElementById("font_size").value.trim(),
    framerate: document.getElementById("framerate").value.trim(),
    update_rate: document.getElementById("update_rate").value.trim(),
    background_color: document.getElementById("bg").value.trim(),
    outline: document.getElementById("outline").checked
  };

  const r = await fetch("/api/jobs", {
    method:"POST",
    headers: {"Content-Type":"application/json","X-Admin-Token":token()},
    body: JSON.stringify(payload)
  });

  const txt = await r.text();
  if(!r.ok) return document.getElementById("out").textContent = txt;

  jobId = JSON.parse(txt).job_id;
  poll();
}

async function poll(){
  if(!jobId) return;
  const r = await fetch("/api/jobs/"+jobId, {headers: {"X-Admin-Token":token()}});
  const data = await r.json();
  let s = JSON.stringify(data, null, 2);
  if(data.status==="done"){
    s += "\\n\\nDownload: " + location.origin + "/api/jobs/" + jobId + "/file";
  }
  document.getElementById("out").textContent = s;
  if(data.status==="queued" || data.status==="running") setTimeout(poll, 3000);
}
</script>
</body>
</html>
"""

@app.post("/api/jobs")
async def create_job(req: Request):
    auth(req)
    body = await req.json()

    vod_id = str(body.get("vod_id","")).strip()
    if not re.fullmatch(r"\d+", vod_id):
        raise HTTPException(400, "vod_id must be numeric")

    quality = str(body.get("quality","1080p60")).strip() or "1080p60"

    try:
        threads = int(str(body.get("threads","2")).strip())
    except:
        raise HTTPException(400, "threads must be a number")
    threads = max(1, min(4, threads))

    bw_raw = str(body.get("bandwidth","")).strip()
    bandwidth = None
    if bw_raw:
        try:
            bandwidth = int(bw_raw)
        except:
            raise HTTPException(400, "bandwidth must be a number (KiB/s)")
        bandwidth = max(64, min(20000, bandwidth))

    beginning = (str(body.get("beginning","")).strip() or None)
    ending = (str(body.get("ending","")).strip() or None)

    include_chat = bool(body.get("include_chat", True))

    # Chat settings (only used if include_chat)
    def as_int(name, default, lo, hi):
        try:
            v = int(str(body.get(name, default)).strip())
        except:
            raise HTTPException(400, f"{name} must be a number")
        return max(lo, min(hi, v))

    chat_width = as_int("chat_width", 422, 250, 900)
    font_size = as_int("font_size", 18, 10, 48)
    framerate = as_int("framerate", 30, 10, 60)

    try:
        update_rate = float(str(body.get("update_rate", 0.2)).strip())
    except:
        raise HTTPException(400, "update_rate must be a number")
    update_rate = max(0.0, min(2.0, update_rate))

    background_color = str(body.get("background_color", "#111111")).strip() or "#111111"
    outline = bool(body.get("outline", False))

    if q.full():
        raise HTTPException(429, "Queue full. Try again later.")

    job_id = uuid.uuid4().hex

    video_path = os.path.join(DOWNLOAD_DIR, f"{vod_id}-{job_id}.video.mp4")
    chat_json = os.path.join(DOWNLOAD_DIR, f"{vod_id}-{job_id}.chat.json.gz")
    chat_mp4 = os.path.join(DOWNLOAD_DIR, f"{vod_id}-{job_id}.chat.mp4")
    final_path = os.path.join(DOWNLOAD_DIR, f"{vod_id}-{job_id}.final.mp4")

    jobs[job_id] = {
        "job_id": job_id,
        "vod_id": vod_id,
        "status": "queued",
        "stage": "queued",
        "quality": quality,
        "include_chat": include_chat,
        "path": "",
        "log": "",
        "started_at": 0,
        "finished_at": 0,
        "error": ""
    }

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
def get_job(job_id: str, req: Request):
    auth(req)
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job

@app.get("/api/jobs/{job_id}/file")
def get_file(job_id: str, req: Request):
    auth(req)
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    if job["status"] != "done" or not job["path"] or not os.path.exists(job["path"]):
        raise HTTPException(409, "not ready (or file expired)")
    return FileResponse(job["path"], filename=os.path.basename(job["path"]))
