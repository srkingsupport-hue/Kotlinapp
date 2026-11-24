# SR King v5.0 — Engineering Audit & Upgrade Summary

## 1. Project Understanding
The original codebase (`bot.py`, `config.py`, `downloader.py`, `storage.py`, `requirements.txt`) was a Telegram-only YouTube audio downloader featuring `yt-dlp` client rotation, Node.js `nsig` support, disk + Telegram `file_id` caching, Auto-DJ jukebox, and a 7-Zone 3D HRTF Beat-Synced 16D Spatial DSP engine.

Because Telegram caps bot uploads at 50 MB, downloading full HD/4K videos or movies was impossible inside Telegram. This upgrade transforms the core engine into a dedicated **Railway-ready Web Application** (FastAPI backend + vanilla ES6+ studio UI) focused strictly on **YouTube link → direct download** (Video + Audio up to 4K MP4, Studio Audio, and 16D Spatial Audio), with DJ Modes intentionally excluded for now and kept modular for future addition.

## 2. Current State Audit (Original Bot vs. Web Requirements)
- **CRITICAL — 50 MB Upload Ceiling**: Telegram's file cap forced bitrate downscaling (`_fit_telegram_size`) and prevented video/movie downloads. **Fixed**: Replaced with streaming HTTP `FileResponse` supporting HTTP `Range` requests and configurable limits up to 4 GB / 4 hours.
- **CRITICAL — Missing Railway Container Runtime**: Original repo lacked a `Dockerfile` and `railway.json`, risking missing `ffmpeg` or `node` binaries on Nixpacks builds. **Fixed**: Added deterministic `python:3.11-slim-bookworm` Dockerfile with `ffmpeg`, `nodejs`, and unprivileged runtime user.
- **HIGH — Uncancellable FFmpeg Subprocesses**: Original `subprocess.run()` calls could not be interrupted mid-render if a user cancelled. **Fixed**: Replaced with `_run_cancellable_subprocess()` using `Popen` so clicking Cancel terminates FFmpeg immediately.
- **HIGH — Cookie Deployment Friction on Railway**: Original bot only supported a filesystem path (`YT_COOKIES_FILE`). **Fixed**: Added `YT_COOKIES_BASE64` support that decodes securely to a `0600` file at startup.
- **MEDIUM — Production Auto-Pip-Install Risk**: Original `bot.py` ran `pip install -U yt-dlp` automatically on startup/background, which can break production stability. **Fixed**: Removed blind auto-updates; moved `yt-dlp` upgrade to an explicit, admin-key-protected endpoint (`POST /api/admin/update-ytdlp`).
- **MEDIUM — Reverse Proxy Timeouts on Long Downloads**: Synchronous HTTP download endpoints time out on Railway for large 4K files or 16D renders. **Fixed**: Async `JobManager` + Server-Sent Events (`/api/jobs/{id}/events`) with automatic polling fallback.

## 3. Architecture & Key Improvements
- **Backend (`app/`)**: Clean separation across `config.py`, `storage.py`, `downloader.py`, `jobs.py`, and `main.py`.
- **Security**: Strict YouTube-only regex validation and canonical URL reconstruction (`https://www.youtube.com/watch?v={video_id}`) eliminates SSRF; strict CSP & security headers; per-IP sliding-window rate limiter; timing-safe `secrets.compare_digest` for optional `ADMIN_KEY`.
- **Performance**: 10-minute in-memory inspect cache, single-flight deduplication (`_in_flight` futures), bounded disk cache (skips >500 MB files so movies don't evict songs), and background 5-minute reaper for expired job directories.
- **UI/UX (`static/`)**: High-craft dark studio interface with custom inline SVG iconography, zero emojis, zero online streaming player, 3-tab format selector, estimated file sizes per quality, live progress/speed/ETA/BPM display, auto-triggered browser download, and local `localStorage` recent links.
- **Future Maintenance**: Adding DJ Modes later only requires registering new preset definitions in `app/config.py`, adding a `dj` branch in `app/downloader.py`, and adding one tab in `static/index.html` + `static/app.js`.
