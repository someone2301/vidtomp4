"""vidtomp4: web front end for yt-dlp that saves videos as 360p MP4 files."""
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file
from yt_dlp import YoutubeDL

from sniff import sniff

DOWNLOAD_DIR = Path(os.environ.get("VIDTOMP4_DIR", "/tmp/vidtomp4"))
MAX_AGE_SECONDS = int(os.environ.get("VIDTOMP4_MAX_AGE", "3600"))
MAX_FILESIZE = int(os.environ.get("VIDTOMP4_MAX_BYTES", str(1024**3)))  # 1 GiB

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
app = Flask(__name__)
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_.")[:80] or "video"


def ydl_options(job_id: str, out_dir: Path, headers: dict | None = None,
                title: str | None = None) -> dict:
    def hook(d):
        with jobs_lock:
            job = jobs[job_id]
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                if total:
                    job["progress"] = round(d.get("downloaded_bytes", 0) * 100 / total, 1)
                job["status"] = "downloading"
            elif d["status"] == "finished":
                job["status"] = "processing"

    return {
        # Best video at or below 360p plus best audio, or a pre-merged file <=360p.
        "format": "bv*[height<=360]+ba/b[height<=360]/wv*+ba/w",
        # Prefer 360p H.264 + AAC so the merge needs no re-encode in most cases.
        "format_sort": ["res:360", "vcodec:h264", "acodec:m4a"],
        "merge_output_format": "mp4",
        "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
        "outtmpl": str(out_dir / (safe_name(title) + ".%(ext)s" if title
                                  else "%(title).80B.%(ext)s")),
        "http_headers": headers or {},
        "restrictfilenames": True,
        "noplaylist": True,
        "max_filesize": MAX_FILESIZE,
        "progress_hooks": [hook],
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
    }


def ensure_360p(path: Path) -> Path:
    """Re-encode to 360p H.264/AAC if the site offered no 360p-or-lower stream."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=height", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True)
    height = int(probe.stdout.strip() or 0)
    if height <= 360:
        return path
    out = path.with_name(path.stem + ".360p.mp4")
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-i", str(path), "-vf", "scale=-2:360",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)],
        check=True)
    path.unlink()
    return out


def run_job(job_id: str, url: str) -> None:
    out_dir = DOWNLOAD_DIR / job_id
    out_dir.mkdir()
    try:
        try:
            with YoutubeDL(ydl_options(job_id, out_dir)) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as first_error:
            # yt-dlp could not handle the site: find the stream with a headless browser.
            with jobs_lock:
                jobs[job_id]["status"] = "searching page"
            shutil.rmtree(out_dir)
            out_dir.mkdir()
            try:
                found = sniff(url)
            except Exception as sniff_error:
                raise RuntimeError(f"{first_error} | Browser fallback: {sniff_error}") from None
            headers = {"Referer": found.page_url, "User-Agent": found.user_agent}
            with YoutubeDL(ydl_options(job_id, out_dir, headers, found.title or "video")) as ydl:
                info = ydl.extract_info(found.media_url, download=True)
            info["title"] = found.title or info.get("title")
        files = sorted(out_dir.glob("*.mp4"))
        if not files:
            raise RuntimeError("No MP4 was produced.")
        with jobs_lock:
            jobs[job_id]["status"] = "processing"
        final = ensure_360p(files[0])
        with jobs_lock:
            jobs[job_id].update(status="done", progress=100,
                                file=str(final), title=info.get("title"))
    except Exception as exc:  # report any yt-dlp/ffmpeg failure to the user
        with jobs_lock:
            jobs[job_id].update(status="error", error=str(exc))


def cleanup_loop() -> None:
    while True:
        cutoff = time.time() - MAX_AGE_SECONDS
        with jobs_lock:
            for job_id in [j for j, v in jobs.items() if v["created"] < cutoff]:
                shutil.rmtree(DOWNLOAD_DIR / job_id, ignore_errors=True)
                del jobs[job_id]
        time.sleep(300)


threading.Thread(target=cleanup_loop, daemon=True).start()


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/jobs")
def create_job():
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify(error="Enter a valid http(s) URL."), 400
    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {"status": "queued", "progress": 0, "created": time.time()}
    threading.Thread(target=run_job, args=(job_id, url), daemon=True).start()
    return jsonify(id=job_id), 202


@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            abort(404)
        return jsonify({k: job.get(k) for k in ("status", "progress", "error", "title")})


@app.get("/api/jobs/<job_id>/file")
def job_file(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job.get("status") != "done":
        abort(404)
    path = Path(job["file"])
    return send_file(path, mimetype="video/mp4", as_attachment=True, download_name=path.name)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000)
