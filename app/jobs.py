#!/usr/bin/env python3
"""Async download job manager with SSE live progress, single-flight caching, cancellation, and cleanup."""

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from app import config, downloader
from app.downloader import DownloadCancelled, DownloadError, MediaResult
from app.storage import WebStorage

log = logging.getLogger("SRKing.Jobs")


@dataclass
class JobState:
    job_id: str
    video_id: str
    media_type: str
    quality: str
    client_ip: str
    status: str = "queued"  # queued | running | ready | error | cancelled
    stage: str = "Queued"
    progress: int = 0
    speed: str = ""
    eta: str = ""
    title: str = ""
    channel: str = ""
    duration: int = 0
    filename: str = ""
    file_path: str = ""
    work_dir: str = ""
    file_size: int = 0
    file_size_text: str = ""
    ext: str = ""
    bpm: Optional[float] = None
    from_cache: bool = False
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # Internal runtime controls (excluded from API JSON)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    active_proc: Optional[subprocess.Popen] = field(default=None, repr=False)
    proc_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    subscribers: Set[asyncio.Queue] = field(default_factory=set, repr=False)

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "video_id": self.video_id,
            "media_type": self.media_type,
            "quality": self.quality,
            "status": self.status,
            "stage": self.stage,
            "progress": self.progress,
            "speed": self.speed,
            "eta": self.eta,
            "title": self.title,
            "channel": self.channel,
            "duration": self.duration,
            "filename": self.filename,
            "file_size": self.file_size,
            "file_size_text": self.file_size_text,
            "ext": self.ext,
            "bpm": self.bpm,
            "from_cache": self.from_cache,
            "error": self.error,
            "download_url": f"/api/jobs/{self.job_id}/file" if self.status == "ready" else None,
        }


class JobManager:
    def __init__(self, storage: WebStorage, max_workers: int = config.MAX_WORKERS):
        self.storage = storage
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="srk_web")
        self._jobs: Dict[str, JobState] = {}
        self._in_flight: Dict[Tuple[str, str, str], asyncio.Future] = {}
        self._lock = threading.RLock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._reaper_task: Optional[asyncio.Task] = None

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = loop.create_task(self._reaper_loop())

    def active_jobs_count(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if j.status in ("queued", "running"))

    def active_jobs_for_ip(self, client_ip: str) -> int:
        with self._lock:
            return sum(
                1 for j in self._jobs.values()
                if j.client_ip == client_ip and j.status in ("queued", "running")
            )

    def get_job(self, job_id: str) -> Optional[JobState]:
        with self._lock:
            return self._jobs.get(job_id)

    def _emit(self, job: JobState) -> None:
        job.updated_at = time.time()
        payload = job.to_public_dict()
        if not self._loop or self._loop.is_closed():
            return

        def _push() -> None:
            for q in list(job.subscribers):
                try:
                    if q.full():
                        try:
                            q.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                    q.put_nowait(payload)
                except Exception:
                    pass

        self._loop.call_soon_threadsafe(_push)

    def subscribe(self, job: JobState) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=32)
        job.subscribers.add(q)
        q.put_nowait(job.to_public_dict())
        return q

    def unsubscribe(self, job: JobState, q: asyncio.Queue) -> None:
        job.subscribers.discard(q)

    def create_job(self, video_id: str, media_type: str, quality: str, client_ip: str) -> JobState:
        job_id = uuid.uuid4().hex[:16]
        job = JobState(
            job_id=job_id,
            video_id=video_id,
            media_type=media_type,
            quality=quality,
            client_ip=client_ip,
            status="queued",
            stage="Starting download",
            progress=1,
        )
        with self._lock:
            self._jobs[job_id] = job
        if self._loop:
            self._loop.create_task(self._run_job(job))
        return job

    def cancel_job(self, job_id: str) -> bool:
        job = self.get_job(job_id)
        if not job or job.status not in ("queued", "running"):
            return False
        job.cancel_event.set()
        with job.proc_lock:
            proc = job.active_proc
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
        job.status = "cancelled"
        job.stage = "Cancelled"
        job.error = "Download cancelled."
        self._emit(job)
        return True

    def _save_client(self, client: str) -> None:
        try:
            self.storage.set_setting("last_working_client", client)
        except Exception:
            pass

    async def _run_job(self, job: JobState) -> None:
        assert self._loop is not None
        job.status = "running"
        job.stage = "Checking cache"
        job.progress = 2
        self._emit(job)

        # 1. Instant disk cache hit
        if config.CACHE_ENABLED:
            cached = await self._loop.run_in_executor(
                None, downloader.disk_cache_lookup, job.video_id, job.media_type, job.quality
            )
            if cached:
                cached_path, meta = cached
                work_dir = tempfile.mkdtemp(prefix=f"job_{job.job_id}_", dir=config.JOBS_DIR)
                ext = meta.get("ext") or os.path.splitext(cached_path)[1].lstrip(".") or "mp4"
                filename = meta.get("filename") or downloader.sanitize_filename(
                    meta.get("title") or job.video_id, ext
                )
                local_path = os.path.join(work_dir, f"media.{ext}")
                try:
                    await self._loop.run_in_executor(None, downloader._link_or_copy, cached_path, local_path)
                    sz = os.path.getsize(local_path)
                    job.work_dir = work_dir
                    job.file_path = local_path
                    job.filename = filename
                    job.title = meta.get("title") or "SR King Media"
                    job.channel = meta.get("channel") or "YouTube"
                    job.duration = int(meta.get("duration") or 0)
                    job.file_size = sz
                    job.file_size_text = downloader.human_size(sz)
                    job.ext = ext
                    job.bpm = meta.get("bpm")
                    job.from_cache = True
                    job.status = "ready"
                    job.stage = "Ready (instant cache hit)"
                    job.progress = 100
                    self.storage.record_download(
                        job.video_id, job.title, job.media_type, job.quality, sz, job.duration, True
                    )
                    self._emit(job)
                    return
                except Exception as e:
                    log.warning("Cache serve failed for %s: %s", job.job_id, e)
                    shutil.rmtree(work_dir, ignore_errors=True)

        # 2. Single-flight deduplication: if same track+quality is already downloading, await it
        flight_key = (job.video_id, job.media_type, job.quality)
        existing_future = self._in_flight.get(flight_key)
        if existing_future and not existing_future.done():
            job.stage = "Joining active download for this media"
            job.progress = 20
            self._emit(job)
            try:
                await asyncio.shield(existing_future)
                cached = await self._loop.run_in_executor(
                    None, downloader.disk_cache_lookup, job.video_id, job.media_type, job.quality
                )
                if cached:
                    cached_path, meta = cached
                    work_dir = tempfile.mkdtemp(prefix=f"job_{job.job_id}_", dir=config.JOBS_DIR)
                    ext = meta.get("ext") or "mp4"
                    local_path = os.path.join(work_dir, f"media.{ext}")
                    await self._loop.run_in_executor(None, downloader._link_or_copy, cached_path, local_path)
                    sz = os.path.getsize(local_path)
                    job.work_dir = work_dir
                    job.file_path = local_path
                    job.filename = meta.get("filename") or f"{job.video_id}.{ext}"
                    job.title = meta.get("title") or "SR King Media"
                    job.channel = meta.get("channel") or "YouTube"
                    job.duration = int(meta.get("duration") or 0)
                    job.file_size = sz
                    job.file_size_text = downloader.human_size(sz)
                    job.ext = ext
                    job.bpm = meta.get("bpm")
                    job.from_cache = True
                    job.status = "ready"
                    job.stage = "Ready to download"
                    job.progress = 100
                    self.storage.record_download(
                        job.video_id, job.title, job.media_type, job.quality, sz, job.duration, True
                    )
                    self._emit(job)
                    return
            except Exception:
                # Fall through to own download if leader failed or was cancelled
                pass

        # 3. Execute fresh download in worker pool
        future = self._loop.create_future()
        self._in_flight[flight_key] = future
        work_dir = tempfile.mkdtemp(prefix=f"job_{job.job_id}_", dir=config.JOBS_DIR)
        job.work_dir = work_dir

        def _on_progress(stage: str, pct: int, speed: str, eta: str) -> None:
            if job.cancel_event.is_set():
                return
            job.stage = stage
            job.progress = pct
            job.speed = speed
            job.eta = eta
            self._emit(job)

        def _register_proc(proc: Optional[subprocess.Popen]) -> None:
            with job.proc_lock:
                job.active_proc = proc

        try:
            res: MediaResult = await self._loop.run_in_executor(
                self.executor,
                downloader.download_media_sync,
                job.video_id,
                job.media_type,
                job.quality,
                work_dir,
                job.cancel_event,
                _on_progress,
                _register_proc,
                self._save_client,
            )

            meta_payload = {
                "title": res.title,
                "channel": res.channel,
                "duration": res.duration,
                "filename": res.filename,
                "ext": res.ext,
                "bpm": res.bpm,
            }
            if config.CACHE_ENABLED:
                await self._loop.run_in_executor(
                    None,
                    downloader.disk_cache_save,
                    job.video_id,
                    job.media_type,
                    job.quality,
                    res.filepath,
                    meta_payload,
                )

            job.file_path = res.filepath
            job.filename = res.filename
            job.title = res.title
            job.channel = res.channel
            job.duration = res.duration
            job.file_size = res.file_size
            job.file_size_text = downloader.human_size(res.file_size)
            job.ext = res.ext
            job.bpm = res.bpm
            job.from_cache = False
            job.status = "ready"
            job.stage = "Ready to download"
            job.progress = 100
            job.speed = ""
            job.eta = ""

            self.storage.record_download(
                job.video_id, res.title, job.media_type, job.quality, res.file_size, res.duration, False
            )
            if not future.done():
                future.set_result(meta_payload)
            self._emit(job)

        except DownloadCancelled:
            shutil.rmtree(work_dir, ignore_errors=True)
            job.work_dir = ""
            job.file_path = ""
            job.status = "cancelled"
            job.stage = "Cancelled"
            job.error = "Download cancelled."
            if not future.done():
                future.set_exception(DownloadCancelled("Cancelled"))
            self._emit(job)

        except DownloadError as e:
            shutil.rmtree(work_dir, ignore_errors=True)
            job.work_dir = ""
            job.file_path = ""
            job.status = "error"
            job.stage = "Failed"
            job.error = str(e)
            if not future.done():
                future.set_exception(e)
            self._emit(job)

        except Exception as e:
            log.error("Unhandled job error for %s:\n%s", job.job_id, traceback.format_exc())
            shutil.rmtree(work_dir, ignore_errors=True)
            job.work_dir = ""
            job.file_path = ""
            job.status = "error"
            job.stage = "Failed"
            job.error = "An unexpected server error occurred while processing this media."
            if not future.done():
                future.set_exception(e)
            self._emit(job)

        finally:
            if self._in_flight.get(flight_key) is future:
                self._in_flight.pop(flight_key, None)

    async def _reaper_loop(self) -> None:
        """Periodically clean expired job directories and expired cache entries."""
        try:
            while True:
                await asyncio.sleep(300)
                self.cleanup_expired_jobs()
                downloader.cleanup_expired_disk_cache()
        except asyncio.CancelledError:
            pass

    def cleanup_expired_jobs(self) -> int:
        now = time.time()
        removed = 0
        with self._lock:
            stale_ids = [
                jid
                for jid, j in self._jobs.items()
                if j.status not in ("queued", "running")
                and (now - j.updated_at) > config.JOB_TTL_SECONDS
            ]
            for jid in stale_ids:
                job = self._jobs.pop(jid, None)
                if job and job.work_dir:
                    shutil.rmtree(job.work_dir, ignore_errors=True)
                removed += 1
        return removed

    def cleanup_all_orphan_job_dirs(self) -> int:
        removed = 0
        try:
            for name in os.listdir(config.JOBS_DIR):
                p = os.path.join(config.JOBS_DIR, name)
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                    removed += 1
        except OSError:
            pass
        return removed

    def shutdown(self) -> None:
        if self._reaper_task and not self._reaper_task.done():
            self._reaper_task.cancel()
        with self._lock:
            for job in self._jobs.values():
                job.cancel_event.set()
                with job.proc_lock:
                    if job.active_proc and job.active_proc.poll() is None:
                        try:
                            job.active_proc.terminate()
                        except OSError:
                            pass
        self.executor.shutdown(wait=False, cancel_futures=True)
