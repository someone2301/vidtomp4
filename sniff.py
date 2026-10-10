"""Fallback for sites yt-dlp cannot handle: load the page in headless Chromium
and capture the first video stream (HLS, DASH or MP4) it requests."""
import os
import re
import threading

from playwright.sync_api import sync_playwright

# Each browser uses a few hundred MB of RAM, so cap concurrent sniffs.
_slots = threading.Semaphore(int(os.environ.get("VIDTOMP4_BROWSERS", "2")))
SNIFF_SECONDS = int(os.environ.get("VIDTOMP4_SNIFF_SECONDS", "20"))
CHROMIUM_PATH = os.environ.get("VIDTOMP4_CHROMIUM")  # optional override

MEDIA_URL = re.compile(r"\.(m3u8|mpd|mp4|webm)(\?|$)", re.I)
MEDIA_TYPE = re.compile(r"mpegurl|dash\+xml|^video/", re.I)
# Prefer manifests over single files, since a page may also load short MP4 ads/previews.
PRIORITY = {"m3u8": 0, "mpd": 0, "mp4": 1, "webm": 1}


class SniffResult:
    def __init__(self, media_url: str, page_url: str, title: str, user_agent: str):
        self.media_url = media_url
        self.page_url = page_url
        self.title = title
        self.user_agent = user_agent


def _kind(url: str, content_type: str) -> str | None:
    m = MEDIA_URL.search(url)
    if m:
        return m.group(1).lower()
    if "mpegurl" in content_type:
        return "m3u8"
    if "dash+xml" in content_type:
        return "mpd"
    if content_type.startswith("video/"):
        return "mp4"
    return None


def sniff(page_url: str) -> SniffResult:
    with _slots, sync_playwright() as p:
        kwargs = {"headless": True, "args": ["--no-sandbox", "--autoplay-policy=no-user-gesture-required"]}
        if CHROMIUM_PATH:
            kwargs["executable_path"] = CHROMIUM_PATH
        browser = p.chromium.launch(**kwargs)
        try:
            context = browser.new_context()
            page = context.new_page()
            found: list[tuple[int, int, str]] = []

            def on_response(resp):
                ctype = resp.headers.get("content-type", "").lower()
                kind = _kind(resp.url, ctype)
                if kind and not resp.url.startswith(("blob:", "data:")):
                    found.append((PRIORITY[kind], len(found), resp.url))

            page.on("response", on_response)
            page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
            # Try to start playback in the page and in any iframes.
            for frame in page.frames:
                try:
                    frame.evaluate("document.querySelectorAll('video').forEach(v => { v.muted = true; v.play().catch(() => {}); })")
                except Exception:
                    pass
            for selector in ("video", "[class*=play]", "button[aria-label*=Play i]"):
                try:
                    page.locator(selector).first.click(timeout=2000)
                    break
                except Exception:
                    pass
            waited = 0
            while waited < SNIFF_SECONDS * 1000 and not any(f[0] == 0 for f in found):
                page.wait_for_timeout(500)
                waited += 500
            if not found:
                raise RuntimeError("No video stream found on the page.")
            return SniffResult(min(found)[2], page.url, page.title(),
                               page.evaluate("navigator.userAgent"))
        finally:
            browser.close()
