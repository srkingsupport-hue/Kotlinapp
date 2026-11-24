# SR King Web v5.0

Direct YouTube **Video + Audio (up to 4K MP4)**, **Studio Audio (MP3 320k/192k/128k, Original M4A)**, and **7-Zone 3D HRTF Beat-Synced 16D Spatial Audio** web application built for one-click **Railway** deployment.

- **Link-paste → Download only** (no online video/audio streaming player).
- **YouTube-only strict URL validation** (`youtube.com/watch`, `youtu.be`, `shorts`, `music.youtube.com`).
- **Real-time SSE progress** with speed, ETA, detected BPM for 16D Spatial, instant cancellation (kills active FFmpeg process), and HTTP Range download support for large HD/4K files.
- **Modular architecture** ready for future DJ Modes without touching core download infrastructure.

---

## Deploy to Railway (GitHub → Railway)

1. **Extract this ZIP** and push the `srking-web` folder contents to a new GitHub repository (make sure `Dockerfile`, `railway.json`, `requirements.txt`, `app/`, and `static/` are at the repository root).
2. Open [Railway](https://railway.app), click **New Project → Deploy from GitHub repo**, and select your repository.
3. Railway automatically detects `railway.json` and builds the `Dockerfile` (which pre-installs **Python 3.11 + FFmpeg + Node.js** for YouTube `nsig` challenges).
4. Once deployed, click **Settings → Networking → Generate Domain** to get your live HTTPS URL.
5. Open the URL — the top-right pill will show **Online · FFmpeg Ready**.

---

## Optional Environment Variables (Railway → Variables)

You do **not** need any environment variables to start. All variables below are optional:

| Variable | Default | Description |
| --- | --- | --- |
| `PORT` | `8080` | Set automatically by Railway. |
| `ADMIN_KEY` | *(disabled)* | Secret header (`X-Admin-Key`) to enable `/api/admin/*` endpoints. |
| `YT_COOKIES_BASE64` | *(unset)* | Base64-encoded Netscape `cookies.txt` if YouTube requests sign-in on a datacenter IP. |
| `YT_COOKIES_FILE` | *(unset)* | Alternative local file path to a `cookies.txt` file. |
| `MAX_DURATION_SECONDS` | `14400` (4 hrs) | Maximum supported YouTube video length (supports full movies). |
| `MAX_FILE_SIZE_GB` | `4.0` | Maximum downloadable file size per job. |
| `MAX_WORKERS` | `3` | Concurrent yt-dlp / FFmpeg worker threads. |
| `MAX_ACTIVE_JOBS_PER_IP` | `2` | Maximum simultaneous active downloads per user IP. |
| `CACHE_ENABLED` | `1` | Enable bounded disk cache for instant repeat downloads. |
| `CACHE_MAX_GB` | `3.0` | Maximum disk space used by the cache. |
| `JOB_TTL_SECONDS` | `1800` (30 min) | Auto-cleanup timer for completed temporary download files. |

### How to encode `cookies.txt` for Railway (`YT_COOKIES_BASE64`)

If YouTube ever flags Railway's IP with a bot check, export YouTube cookies in Netscape format and encode them:

```bash
base64 -w 0 cookies.txt
```

Paste the single-line output into Railway's `YT_COOKIES_BASE64` variable and redeploy. The server decodes it to a `0600` private file automatically on startup.

---

## API Overview

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/` | Web UI |
| `GET` | `/health` | Railway health check (`200 OK`) |
| `POST` | `/api/inspect` | Inspect YouTube URL and return available Video, Audio, and 16D presets |
| `POST` | `/api/jobs` | Create an async download/processing job |
| `GET` | `/api/jobs/{job_id}` | Poll current job status |
| `GET` | `/api/jobs/{job_id}/events` | Server-Sent Events (SSE) live progress stream |
| `POST` | `/api/jobs/{job_id}/cancel` | Cancel active download and terminate FFmpeg immediately |
| `GET` | `/api/jobs/{job_id}/file` | Direct attachment download with HTTP Range resume support |
| `GET` | `/api/admin/diagnostics` | *(Requires `X-Admin-Key`)* System, cache, and download stats |
| `POST` | `/api/admin/clear-cache` | *(Requires `X-Admin-Key`)* Flush disk cache and expired jobs |
| `POST` | `/api/admin/update-ytdlp` | *(Requires `X-Admin-Key`)* Manually update `yt-dlp` safely |

---

## Local Development

```bash
# Requires Python 3.11+, ffmpeg, and node installed on PATH
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

Run unit & FFmpeg 16D DSP tests:

```bash
python3 tests/test_core.py
```
