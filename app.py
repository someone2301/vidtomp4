"""vidtomp4: web front end for yt-dlp that saves videos and playlists as MP4 files."""
import hmac
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from pathlib import Path

from flask import Flask, Response, abort, jsonify, render_template, request, send_file
from yt_dlp import YoutubeDL

from sniff import sniff

DOWNLOAD_DIR = Path(os.environ.get("VIDTOMP4_DIR", "/tmp/vidtomp4"))
MAX_AGE_SECONDS = int(os.environ.get("VIDTOMP4_MAX_AGE", "3600"))
MAX_FILESIZE = int(os.environ.get("VIDTOMP4_MAX_BYTES", str(1024**3)))  # 1 GiB, per video
# Optional login. Set both to require a username and password (HTTP basic auth).
AUTH_USER = os.environ.get("VIDTOMP4_USER")
AUTH_PASSWORD = os.environ.get("VIDTOMP4_PASSWORD")
MAX_PLAYLIST = int(os.environ.get("VIDTOMP4_MAX_PLAYLIST", "50"))
# Allowed quality choices: maximum video height, or None for the best available.
QUALITIES = {"144": 144, "240": 240, "360": 360, "480": 480, "720": 720,
             "1080": 1080, "best": None}

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
app = Flask(__name__)


@app.before_request
def require_login():
    if not (AUTH_USER and AUTH_PASSWORD):
        return None
    auth = request.authorization
    if (auth and hmac.compare_digest(auth.username or "", AUTH_USER)
            and hmac.compare_digest(auth.password or "", AUTH_PASSWORD)):
        return None
    return Response("Login required.", 401, {"WWW-Authenticate": 'Basic realm="vidtomp4"'})

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_.")[:80] or "video"


def ydl_options(job_id: str, out_dir: Path, height: int | None, playlist: bool = False,
                headers: dict | None = None, title: str | None = None) -> dict:
    def hook(d):
        with jobs_lock:
            job = jobs[job_id]
            info = d.get("info_dict") or {}
            if info.get("playlist_index"):
                job["item"] = info["playlist_index"]
                job["items"] = info.get("n_entries") or info.get("playlist_count")
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                if total:
                    job["progress"] = round(d.get("downloaded_bytes", 0) * 100 / total, 1)
                job["status"] = "downloading"
            elif d["status"] == "finished":
                job["status"] = "processing"

    if height:
        # Best video at or below the chosen height plus best audio, or a pre-merged file.
        fmt = f"bv*[height<={height}]+ba/b[height<={height}]/wv*+ba/w"
        # Prefer that height in H.264 + AAC so the merge needs no re-encode in most cases.
        sort = [f"res:{height}", "vcodec:h264", "acodec:m4a"]
    else:
        fmt, sort = "bv*+ba/b", ["vcodec:h264", "acodec:m4a"]
    if title:
        name = safe_name(title) + ".%(ext)s"
    elif playlist:
        name = "%(playlist_index)03d_%(title).70B.%(ext)s"
    else:
        name = "%(title).80B.%(ext)s"
    return {
        "format": fmt,
        "format_sort": sort,
        "merge_output_format": "mp4",
        "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
        "outtmpl": str(out_dir / name),
        "http_headers": headers or {},
        "restrictfilenames": True,
        "noplaylist": not playlist,
        "playlist_items": f"1:{MAX_PLAYLIST}",
        # In a playlist, skip private/removed videos instead of failing the whole job.
        "ignoreerrors": playlist,
        "max_filesize": MAX_FILESIZE,
        "progress_hooks": [hook],
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
    }


def ensure_height(path: Path, max_height: int | None) -> Path:
    """Re-encode to max_height H.264/AAC if the site offered no stream at or below it."""
    if not max_height:
        return path
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=height", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True)
    height = int(probe.stdout.strip() or 0)
    if height <= max_height:
        return path
    out = path.with_name(f"{path.stem}.{max_height}p.mp4")
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-i", str(path), "-vf", f"scale=-2:{max_height}",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)],
        check=True)
    path.unlink()
    return out


def run_job(job_id: str, url: str, height: int | None, playlist: bool) -> None:
    out_dir = DOWNLOAD_DIR / job_id
    out_dir.mkdir()
    try:
        try:
            with YoutubeDL(ydl_options(job_id, out_dir, height, playlist)) as ydl:
                info = ydl.extract_info(url, download=True)
            if not info:
                raise RuntimeError("Nothing could be downloaded from that URL.")
        except Exception as first_error:
            if playlist:
                raise
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
            opts = ydl_options(job_id, out_dir, height, headers=headers, title=found.title or "video")
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(found.media_url, download=True)
            info["title"] = found.title or info.get("title")
        files = sorted(out_dir.glob("*.mp4"))
        if not files:
            raise RuntimeError("No MP4 was produced.")
        with jobs_lock:
            jobs[job_id]["status"] = "processing"
        files = [ensure_height(f, height) for f in files]
        if len(files) == 1:
            final, mimetype = files[0], "video/mp4"
        else:
            # MP4 is already compressed, so store the files in the zip without recompressing.
            final, mimetype = out_dir / f"{safe_name(info.get('title') or 'playlist')}.zip", "application/zip"
            with zipfile.ZipFile(final, "w", zipfile.ZIP_STORED) as zf:
                for f in files:
                    zf.write(f, f.name)
                    f.unlink()
        with jobs_lock:
            jobs[job_id].update(status="done", progress=100, file=str(final), mimetype=mimetype,
                                title=info.get("title"), count=len(files))
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
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()
    if not url.startswith(("http://", "https://")):
        return jsonify(error="Enter a valid http(s) URL."), 400
    quality = str(data.get("quality", "360"))
    if quality not in QUALITIES:
        return jsonify(error="Unknown quality."), 400
    playlist = bool(data.get("playlist"))
    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {"status": "queued", "progress": 0, "created": time.time()}
    threading.Thread(target=run_job, args=(job_id, url, QUALITIES[quality], playlist), daemon=True).start()
    return jsonify(id=job_id), 202


@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            abort(404)
        return jsonify({k: job.get(k) for k in ("status", "progress", "error", "title",
                                         "item", "items", "count")})


@app.get("/api/jobs/<job_id>/file")
def job_file(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job.get("status") != "done":
        abort(404)
    path = Path(job["file"])
    return send_file(path, mimetype=job["mimetype"], as_attachment=True, download_name=path.name)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("VIDTOMP4_PORT", "8000")))
