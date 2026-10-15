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
from urllib.parse import quote
from pathlib import Path

from flask import Flask, Response, abort, jsonify, render_template, request, send_file
from yt_dlp import YoutubeDL
from yt_dlp.extractor import get_info_extractor

from sniff import sniff

DOWNLOAD_DIR = Path(os.environ.get("VIDTOMP4_DIR", "/tmp/vidtomp4"))
MAX_AGE_SECONDS = int(os.environ.get("VIDTOMP4_MAX_AGE", "600"))
# 0 means no limit for MAX_FILESIZE and MAX_PLAYLIST.
MAX_FILESIZE = int(os.environ.get("VIDTOMP4_MAX_BYTES", str(1024**3)))  # 1 GiB, per video
# Optional login. Set both to require a username and password (HTTP basic auth).
AUTH_USER = os.environ.get("VIDTOMP4_USER")
AUTH_PASSWORD = os.environ.get("VIDTOMP4_PASSWORD")
MAX_PLAYLIST = int(os.environ.get("VIDTOMP4_MAX_PLAYLIST", "50"))
# Settings that help with sites' own rate limits (HTTP 429 / "Too Many Requests").
COOKIES_FILE = os.environ.get("VIDTOMP4_COOKIES")  # Netscape-format cookies.txt
SLEEP_REQUESTS = float(os.environ.get("VIDTOMP4_SLEEP_REQUESTS", "0"))
SLEEP_VIDEOS = float(os.environ.get("VIDTOMP4_SLEEP_VIDEOS", "0"))
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


CHANNEL_URL = re.compile(
    r"^(https?://(?:www\.|m\.)?youtube\.com/(?:@[^/?#]+|channel/[^/?#]+|c/[^/?#]+|user/[^/?#]+))")
# yt-dlp extractors whose URLs are always lists of videos: channel pages and
# tabs (including channel search), search results pages and playlists.
LIST_EXTRACTORS = [get_info_extractor(k) for k in ("YoutubeTab", "YoutubeSearchURL", "YoutubePlaylist")]


def is_list_url(url: str) -> bool:
    if re.search(r"youtube\.com/(watch|shorts/)|youtu\.be/", url):
        return False  # a single video, even if it has &list=
    return any(ie.suitable(url) for ie in LIST_EXTRACTORS)


def channel_search_url(url: str, query: str) -> str | None:
    """Build https://www.youtube.com/@name/search?query=... from any channel URL."""
    m = CHANNEL_URL.match(url)
    return f"{m.group(1)}/search?query={quote(query)}" if m else None


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_.")[:80] or "video"


class JobCancelled(BaseException):
    """Raised inside a job to stop it. A BaseException, so yt-dlp's
    ignoreerrors (used for playlists) does not swallow it."""


def check_cancelled(job_id: str) -> None:
    with jobs_lock:
        if jobs[job_id].get("cancel"):
            raise JobCancelled()


class JobLogger:
    """Collects yt-dlp's messages so a failed job can say why it failed."""

    def __init__(self, job_id: str):
        self.job_id = job_id

    def _add(self, msg: str) -> None:
        if msg.startswith("[download]") and "%" in msg:
            return  # progress lines
        with jobs_lock:
            log = jobs[self.job_id].setdefault("log", [])
            log.append(msg)
            del log[:-20]

    debug = info = _add

    def warning(self, msg: str) -> None:
        self._add(msg)

    def error(self, msg: str) -> None:
        self._add(msg)


def ydl_options(job_id: str, out_dir: Path, height: int | None, playlist: bool = False,
                headers: dict | None = None, title: str | None = None) -> dict:
    def hook(d):
        check_cancelled(job_id)
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
        "playlist_items": f"1:{MAX_PLAYLIST}" if MAX_PLAYLIST else None,
        # In a playlist, skip private/removed videos instead of failing the whole job.
        "ignoreerrors": playlist,
        "max_filesize": MAX_FILESIZE or None,
        "cookiefile": COOKIES_FILE,
        "sleep_interval_requests": SLEEP_REQUESTS or None,
        # Random pause of SLEEP_VIDEOS to 2x SLEEP_VIDEOS seconds before each video.
        "sleep_interval": SLEEP_VIDEOS or None,
        "max_sleep_interval": SLEEP_VIDEOS * 2 or None,
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 5,
        "retry_sleep_functions": {"http": lambda n: min(2 ** n, 60),
                                  "fragment": lambda n: min(2 ** n, 60),
                                  "extractor": lambda n: min(5 * 2 ** n, 120)},
        "progress_hooks": [hook],
        "logger": JobLogger(job_id),
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
    }


def ensure_height(path: Path, max_height: int | None, job_id: str) -> Path:
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
    proc = subprocess.Popen(
        ["ffmpeg", "-loglevel", "error", "-y", "-i", str(path), "-vf", f"scale=-2:{max_height}",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)])
    while proc.poll() is None:
        try:
            check_cancelled(job_id)
        except JobCancelled:
            proc.kill()
            proc.wait()
            raise
        time.sleep(0.5)
    if proc.returncode:
        raise RuntimeError(f"ffmpeg failed to re-encode (exit code {proc.returncode}).")
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
                found = sniff(url, lambda: check_cancelled(job_id))
            except Exception as sniff_error:
                raise RuntimeError(f"{first_error} | Browser fallback: {sniff_error}") from None
            headers = {"Referer": found.page_url, "User-Agent": found.user_agent}
            opts = ydl_options(job_id, out_dir, height, headers=headers, title=found.title or "video")
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(found.media_url, download=True)
            info["title"] = found.title or info.get("title")
        files = sorted(out_dir.glob("*.mp4"))
        if not files:
            with jobs_lock:
                log = jobs[job_id].get("log", [])
            reasons = [m for m in log if any(k in m.lower() for k in
                       ("error", "warning", "larger than", "max-filesize", "skipping", "not available"))]
            if any("larger than" in m or "max-filesize" in m for m in log):
                hint = (f"The video is larger than the {MAX_FILESIZE / 1024**2:g} MB limit. "
                        "Choose a lower quality or raise VIDTOMP4_MAX_BYTES.")
            else:
                hint = "yt-dlp finished without producing a file."
            detail = " | ".join(reasons[-3:] or log[-3:])
            raise RuntimeError(f"No MP4 was produced. {hint}" + (f" Details: {detail}" if detail else ""))
        with jobs_lock:
            jobs[job_id]["status"] = "processing"
        files = [ensure_height(f, height, job_id) for f in files]
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
                                title=info.get("title"), count=len(files),
                                finished=time.time())
    except JobCancelled:
        shutil.rmtree(out_dir, ignore_errors=True)
        with jobs_lock:
            jobs[job_id].update(status="cancelled", finished=time.time())
    except Exception as exc:  # report any yt-dlp/ffmpeg failure to the user
        with jobs_lock:
            jobs[job_id].update(status="error", error=str(exc), finished=time.time())


def cleanup_loop() -> None:
    while True:
        cutoff = time.time() - MAX_AGE_SECONDS
        with jobs_lock:
            # Expire finished jobs by when they finished, and never delete a running job.
            for job_id, job in list(jobs.items()):
                if job["status"] in ("done", "error", "cancelled") and job.get("finished", job["created"]) < cutoff:
                    shutil.rmtree(DOWNLOAD_DIR / job_id, ignore_errors=True)
                    del jobs[job_id]
            # Remove leftovers from before a restart, which are no longer in `jobs`.
            for path in DOWNLOAD_DIR.iterdir():
                if path.name not in jobs and path.stat().st_mtime < cutoff:
                    shutil.rmtree(path, ignore_errors=True) if path.is_dir() else path.unlink()
        time.sleep(60)


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
    search = str(data.get("search", "")).strip()
    if search:
        url = channel_search_url(url, search)
        if not url:
            return jsonify(error="Searching only works with a YouTube channel URL, "
                                 "such as https://www.youtube.com/@name."), 400
    # Channel pages, channel searches and search results are lists of videos.
    playlist = playlist or is_list_url(url)
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


@app.post("/api/jobs/<job_id>/cancel")
def cancel_job(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            abort(404)
        if job["status"] not in ("done", "error", "cancelled"):
            job["cancel"] = True
            job["status"] = "cancelling"
        return jsonify(status=job["status"])


@app.get("/api/jobs/<job_id>/file")
def job_file(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job.get("status") != "done":
        abort(404)
    path = Path(job["file"])
    return send_file(path, mimetype=job["mimetype"], as_attachment=True, download_name=path.name)


if __name__ == "__main__":
    app.run(host=os.environ.get("VIDTOMP4_HOST", "127.0.0.1"), port=int(os.environ.get("VIDTOMP4_PORT", "8000")))
