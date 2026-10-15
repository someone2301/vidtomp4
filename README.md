# vidtomp4

A small Flask web app built on [yt-dlp](https://github.com/yt-dlp/yt-dlp). Paste a video URL and it downloads the video, with audio, as an MP4. Choose a quality from 144p to 1080p (default 360p) or "Best available". Tick "Whole playlist" to download every video in a playlist (for example a YouTube playlist) as a zip of MP4 files.

## Requirements
- Python 3.10+
- ffmpeg (`sudo apt install ffmpeg`), which merges the audio and video streams

## Run locally
```sh
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium
python app.py        # http://127.0.0.1:8000
```

## Deploy (systemd + gunicorn)
```sh
sudo useradd -r -s /usr/sbin/nologin vidtomp4
sudo mkdir -p /opt/vidtomp4 /var/lib/vidtomp4
sudo cp -r app.py sniff.py templates requirements.txt /opt/vidtomp4/
sudo python3 -m venv /opt/vidtomp4/venv
sudo /opt/vidtomp4/venv/bin/pip install -r /opt/vidtomp4/requirements.txt
sudo PLAYWRIGHT_BROWSERS_PATH=/opt/vidtomp4/browsers /opt/vidtomp4/venv/bin/playwright install --with-deps chromium
sudo chown -R vidtomp4: /opt/vidtomp4 /var/lib/vidtomp4
sudo cp deploy/vidtomp4.service /etc/systemd/system/
sudo systemctl enable --now vidtomp4
```
Put nginx or Caddy in front of 127.0.0.1:8000 for HTTPS. Keep `--workers 1`, because job state is held in memory.

Sites change often, so update yt-dlp regularly: `/opt/vidtomp4/venv/bin/pip install -U yt-dlp`.

## Settings (environment variables)
| Variable | Default | Meaning |
|---|---|---|
| `VIDTOMP4_HOST` | `127.0.0.1` | Address for `python app.py` to listen on; `0.0.0.0` allows LAN/VPN access (the systemd unit binds 0.0.0.0) |
| `VIDTOMP4_PORT` | `8000` | Port for `python app.py` (the systemd unit binds 8090) |
| `VIDTOMP4_USER` / `VIDTOMP4_PASSWORD` | (unset) | Set both to require a login |
| `VIDTOMP4_DIR` | `/tmp/vidtomp4` | Where files are stored |
| `VIDTOMP4_MAX_AGE` | `600` | Seconds after a job finishes before its files are deleted (checked every minute; leftovers from before a restart are also removed) |
| `VIDTOMP4_MAX_BYTES` | `1073741824` | Largest download allowed, per video (`0` = no limit) |
| `VIDTOMP4_MAX_PLAYLIST` | `50` | Most videos downloaded from one playlist (`0` = no limit) |
| `VIDTOMP4_COOKIES` | (unset) | Path to a Netscape-format `cookies.txt`, sent to sites (helps with YouTube rate limits and bot checks) |
| `VIDTOMP4_SLEEP_REQUESTS` | `0` | Seconds to wait between requests while reading a page or playlist |
| `VIDTOMP4_SLEEP_VIDEOS` | `0` | Wait a random 1x to 2x this many seconds before each video |
| `VIDTOMP4_BROWSERS` | `2` | Max headless browsers running at once |
| `VIDTOMP4_SNIFF_SECONDS` | `20` | How long to watch a page for a video stream |
| `VIDTOMP4_CHROMIUM` | (unset) | Path to a Chromium binary, if not using Playwright's |

## Browser fallback
If yt-dlp cannot handle a site, the app opens the page in headless Chromium, tries to start the player, and records network requests for HLS (`.m3u8`), DASH (`.mpd`) or MP4/WebM files. The first stream it finds is downloaded with the page as Referer, then converted to MP4 at the chosen quality as usual. The fallback is not used in playlist mode. It cannot handle DRM, streams with keys ffmpeg cannot get, or pages behind bot checks such as Cloudflare challenges. Each browser uses a few hundred MB of RAM.

## Channels and searches
- A YouTube channel URL (`https://www.youtube.com/@name`, or a tab like `/videos`), a channel search URL (`/@name/search?query=...`), a search results URL (`/results?search_query=...`) or a playlist URL is downloaded as a playlist automatically, even if "Whole playlist" is not ticked.
- To download only matching videos from a channel, paste the channel URL and type words in "Search this channel". The app builds the channel's search URL for you.
- `VIDTOMP4_MAX_PLAYLIST` (default 50) also limits how many videos a channel or search downloads.

## Notes
- The Cancel button stops a job at any stage (downloading, browser search or re-encoding) and deletes its files right away.
- If a site has no stream at or below the chosen quality, the video is re-encoded down to it. "Best available" is never re-encoded.
- In playlist mode, private or removed videos are skipped. A large playlist at high quality needs a lot of disk space, since files are kept until they expire.
- Set `VIDTOMP4_USER` and `VIDTOMP4_PASSWORD` before exposing the app publicly. Anyone who can reach it can use your server's bandwidth.
- Only download content you have the right to download. Follow each site's terms of service.
