#!/usr/bin/env python3
"""SR King Web — FastAPI application serving static UI, YouTube inspection, SSE jobs, and streaming downloads."""

import asyncio
import json
import logging
import os
import secrets
import shutil
import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Deque, Dict, Literal, Optional, Tuple
from urllib.parse import quote

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import config, downloader
from app.downloader import DownloadError, detect_youtube_url, human_size
from app.jobs import JobManager
from app.storage import WebStorage

# ======================================================================================
#  LOGGING & RUNTIME SINGLETONS
# ======================================================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("SRKing.Web")

START_TIME = time.time()
STORAGE = WebStorage(config.DB_PATH)
JOBS = JobManager(storage=STORAGE, max_workers=config.MAX_WORKERS)

_saved_client = STORAGE.get_setting("last_working_client")
if _saved_client:
    downloader.set_last_working_client(_saved_client)


# ======================================================================================
#  SLIDING-WINDOW RATE LIMITER
# ======================================================================================

class RateLimiter:
    def __init__(self) -> None:
        self._windows: Dict[str, Deque[float]] = {}

    def check(self, key: str, limit: int, window_sec: int = 60) -> Tuple[bool, int]:
        now = time.monotonic()
        dq = self._windows.setdefault(key, deque())
        cutoff = now - window_sec
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= limit:
            retry = max(1, int(dq[0] + window_sec - now))
            return False, retry
        dq.append(now)
        if len(self._windows) > 5000:
            self._windows.clear()
        return True, 0


RATE_LIMITER = RateLimiter()


def get_client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def verify_admin(x_admin_key: Optional[str]) -> None:
    if not config.ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Admin endpoints are disabled (ADMIN_KEY not set).")
    if not x_admin_key or not secrets.compare_digest(x_admin_key, config.ADMIN_KEY):
        raise HTTPException(status_code=401, detail="Invalid admin key.")


# ======================================================================================
#  LIFESPAN & FASTAPI APP
# ======================================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()
    removed = JOBS.cleanup_all_orphan_job_dirs()
    downloader.cleanup_expired_disk_cache()
    JOBS.attach_loop(loop)
    log.info(
        "%s v%s ready | env=%s | port=%d | workers=%d | yt-dlp=%s | ffmpeg=%s | node=%s | cleaned_orphans=%d",
        config.APP_NAME,
        config.APP_VERSION,
        config.runtime_env_name(),
        config.PORT,
        config.MAX_WORKERS,
        downloader.get_ytdlp_version(),
        bool(downloader.FFMPEG_PATH),
        bool(downloader.NODE_PATH),
        removed,
    )
    yield
    log.info("Shutting down %s gracefully...", config.APP_NAME)
    JOBS.shutdown()
    STORAGE.close()


app = FastAPI(
    title=f"{config.APP_NAME} Web",
    version=config.APP_VERSION,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "img-src 'self' https://i.ytimg.com https://i9.ytimg.com https://img.youtube.com data:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "script-src 'self'; "
        "connect-src 'self';"
    )
    return response


# ======================================================================================
#  REQUEST SCHEMAS
# ======================================================================================

class InspectRequest(BaseModel):
    url: str = Field(..., min_length=10, max_length=500)


class CreateJobRequest(BaseModel):
    url: str = Field(..., min_length=10, max_length=500)
    media_type: Literal["video", "audio", "spatial16d"]
    quality: str = Field(..., min_length=2, max_length=32)


# ======================================================================================
#  PUBLIC ENDPOINTS
# ======================================================================================

@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "app": config.APP_NAME,
        "version": config.APP_VERSION,
        "uptime_seconds": int(time.time() - START_TIME),
        "ffmpeg": bool(downloader.FFMPEG_PATH),
        "node": bool(downloader.NODE_PATH),
    }


@app.post("/api/inspect")
async def inspect_link(payload: InspectRequest, request: Request):
    ip = get_client_ip(request)
    ok, retry = RATE_LIMITER.check(f"inspect:{ip}", config.RATE_LIMIT_INSPECT_PER_MIN)
    if not ok:
        raise HTTPException(
            status_code=429,
            detail=f"Too many requests. Please wait {retry}s and try again.",
        )

    canonical_url, video_id = detect_youtube_url(payload.url)
    if not canonical_url or not video_id:
        raise HTTPException(
            status_code=400,
            detail="Please enter a valid YouTube link (youtube.com/watch, youtu.be, shorts, or music.youtube.com).",
        )

    loop = asyncio.get_running_loop()

    def _save_client(client: str) -> None:
        STORAGE.set_setting("last_working_client", client)

    try:
        info = await loop.run_in_executor(
            JOBS.executor, downloader.inspect_youtube_sync, video_id, _save_client
        )
        return info
    except DownloadError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception:
        log.exception("Unexpected error during inspect for %s", video_id)
        raise HTTPException(status_code=500, detail="Could not inspect this YouTube link. Please retry.")


@app.post("/api/jobs")
async def create_download_job(payload: CreateJobRequest, request: Request):
    ip = get_client_ip(request)
    ok, retry = RATE_LIMITER.check(f"dl:{ip}", config.RATE_LIMIT_DOWNLOAD_PER_MIN)
    if not ok:
        raise HTTPException(
            status_code=429,
            detail=f"Download rate limit reached. Please wait {retry}s.",
        )

    if JOBS.active_jobs_for_ip(ip) >= config.MAX_ACTIVE_JOBS_PER_IP:
        raise HTTPException(
            status_code=429,
            detail=f"You already have {config.MAX_ACTIVE_JOBS_PER_IP} active downloads. Wait or cancel one first.",
        )

    canonical_url, video_id = detect_youtube_url(payload.url)
    if not canonical_url or not video_id:
        raise HTTPException(status_code=400, detail="Only valid YouTube links are supported.")

    quality = payload.quality.strip().lower()
    if payload.media_type == "video":
        valid_video = {f"{h}p" for h in config.VIDEO_HEIGHTS}
        if quality not in valid_video:
            raise HTTPException(status_code=400, detail="Unsupported video resolution selected.")
    elif payload.media_type == "audio":
        if quality not in config.AUDIO_PRESETS:
            raise HTTPException(status_code=400, detail="Unsupported audio quality selected.")
    elif payload.media_type == "spatial16d":
        if quality not in config.SPATIAL_16D_PRESETS:
            raise HTTPException(status_code=400, detail="Unsupported 16D spatial quality selected.")

    job = JOBS.create_job(video_id=video_id, media_type=payload.media_type, quality=quality, client_ip=ip)
    return JSONResponse(status_code=202, content=job.to_public_dict())


@app.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str):
    job = JOBS.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Download session not found or expired.")
    return job.to_public_dict()


@app.get("/api/jobs/{job_id}/events")
async def stream_job_events(job_id: str, request: Request):
    job = JOBS.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Download session not found or expired.")

    queue = JOBS.subscribe(job)

    async def _event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(data)}\n\n"
                    if data.get("status") in ("ready", "error", "cancelled"):
                        break
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            JOBS.unsubscribe(job, queue)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_download_job(job_id: str):
    job = JOBS.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Download session not found.")
    cancelled = JOBS.cancel_job(job_id)
    return {"cancelled": cancelled, "status": job.status}


@app.get("/api/jobs/{job_id}/file")
async def download_job_file(job_id: str):
    job = JOBS.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Download link expired. Please fetch the video again.")
    if job.status != "ready" or not job.file_path or not os.path.isfile(job.file_path):
        raise HTTPException(status_code=409, detail="File is not ready for download.")

    mime_map = {
        "mp4": "video/mp4",
        "mkv": "video/x-matroska",
        "webm": "video/webm",
        "mp3": "audio/mpeg",
        "m4a": "audio/mp4",
    }
    media_type = mime_map.get(job.ext.lower(), "application/octet-stream")
    ascii_fallback = job.filename.encode("ascii", "ignore").decode("ascii").strip() or f"srking.{job.ext}"
    ascii_fallback = ascii_fallback.replace('"', "")
    utf8_name = quote(job.filename)

    return FileResponse(
        path=job.file_path,
        media_type=media_type,
        headers={
            "Content-Disposition": f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{utf8_name}",
            "Cache-Control": "private, max-age=1800",
        },
    )


# ======================================================================================
#  PROTECTED OWNER / ADMIN ENDPOINTS
# ======================================================================================

@app.get("/api/admin/diagnostics")
async def admin_diagnostics(x_admin_key: Optional[str] = Header(default=None)):
    verify_admin(x_admin_key)
    disk_usage = shutil.disk_usage(config.DOWNLOAD_ROOT)
    cache_files, cache_bytes = downloader.disk_cache_stats()
    return {
        "app": config.APP_NAME,
        "version": config.APP_VERSION,
        "env": config.runtime_env_name(),
        "python": sys.version.split()[0],
        "yt_dlp_version": downloader.get_ytdlp_version(),
        "ffmpeg": bool(downloader.FFMPEG_PATH),
        "node": bool(downloader.NODE_PATH),
        "cookies_configured": bool(config.YT_COOKIES_FILE),
        "last_working_client": downloader.get_last_working_client(),
        "active_jobs": JOBS.active_jobs_count(),
        "max_workers": config.MAX_WORKERS,
        "cache_files": cache_files,
        "cache_size": human_size(cache_bytes),
        "disk_free": human_size(disk_usage.free),
        "stats": STORAGE.get_stats(),
        "uptime_seconds": int(time.time() - START_TIME),
    }


@app.post("/api/admin/clear-cache")
async def admin_clear_cache(x_admin_key: Optional[str] = Header(default=None)):
    verify_admin(x_admin_key)
    removed, freed = downloader.disk_cache_clear()
    stale_jobs = JOBS.cleanup_expired_jobs()
    return {
        "cache_files_removed": removed,
        "bytes_freed": freed,
        "freed_human": human_size(freed),
        "expired_jobs_cleaned": stale_jobs,
    }


@app.post("/api/admin/update-ytdlp")
async def admin_update_ytdlp(x_admin_key: Optional[str] = Header(default=None)):
    verify_admin(x_admin_key)
    loop = asyncio.get_running_loop()
    ok, detail = await loop.run_in_executor(None, downloader.update_ytdlp_sync)
    return {"ok": ok, "detail": detail}


# ======================================================================================
#  STATIC FRONTEND MOUNT
# ======================================================================================

@app.get("/")
async def serve_index():
    return FileResponse(os.path.join(config.STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")
