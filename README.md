# vidtomp4

A small Flask web app built on [yt-dlp](https://github.com/yt-dlp/yt-dlp). Paste a video URL and it downloads the video, with audio, as a 360p MP4.

## Requirements
- Python 3.10+
- ffmpeg (`sudo apt install ffmpeg`), which merges the audio and video streams

## Run locally
```sh
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt
python app.py        # http://127.0.0.1:8000
```

## Deploy (systemd + gunicorn)
```sh
sudo useradd -r -s /usr/sbin/nologin vidtomp4
sudo mkdir -p /opt/vidtomp4 /var/lib/vidtomp4
sudo cp -r app.py templates requirements.txt /opt/vidtomp4/
sudo python3 -m venv /opt/vidtomp4/venv
sudo /opt/vidtomp4/venv/bin/pip install -r /opt/vidtomp4/requirements.txt
sudo chown -R vidtomp4: /opt/vidtomp4 /var/lib/vidtomp4
sudo cp deploy/vidtomp4.service /etc/systemd/system/
sudo systemctl enable --now vidtomp4
```
Put nginx or Caddy in front of 127.0.0.1:8000 for HTTPS. Keep `--workers 1`, because job state is held in memory.

Sites change often, so update yt-dlp regularly: `/opt/vidtomp4/venv/bin/pip install -U yt-dlp`.

## Settings (environment variables)
| Variable | Default | Meaning |
|---|---|---|
| `VIDTOMP4_DIR` | `/tmp/vidtomp4` | Where files are stored |
| `VIDTOMP4_MAX_AGE` | `3600` | Seconds before files are deleted |
| `VIDTOMP4_MAX_BYTES` | `1073741824` | Largest download allowed |

## Notes
- The app has no authentication. Do not expose it publicly without adding a login (for example, nginx basic auth). Anyone who can reach it can use your server's bandwidth.
- Only download content you have the right to download. Follow each site's terms of service.
