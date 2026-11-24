#!/usr/bin/env python3
"""YouTube media inspector, High-Quality Video+Audio merger, Audio extractor, and 7-Zone 3D HRTF 16D Spatial DSP."""

import array
import json
import logging
import math
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import yt_dlp

from app import config

log = logging.getLogger("SRKing.Downloader")


# ======================================================================================
#  EXCEPTIONS & MODELS
# ======================================================================================


class DownloadError(Exception):
    """User-facing download or inspection error with safe diagnostic text."""


class DownloadCancelled(DownloadError):
    """Raised when a user cancels an in-progress download or DSP render."""


@dataclass
class MediaResult:
    filepath: str
    filename: str
    work_dir: str
    video_id: str
    title: str
    channel: str
    duration: int
    file_size: int
    ext: str
    media_type: str
    quality: str
    from_cache: bool
    bpm: Optional[float] = None


# ======================================================================================
#  STRICT YOUTUBE URL PARSING & UTILITIES
# ======================================================================================

_YOUTUBE_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.|m\.|music\.)?"
    r"(?:youtube\.com/(?:watch\?(?:[^\s]*&)?v=|shorts/|live/|embed/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})(?:[?&#/][^\s]*)?$",
    re.IGNORECASE,
)

_YOUTUBE_URL_SEARCH_RE = re.compile(
    r"(?:https?://)?(?:www\.|m\.|music\.)?"
    r"(?:youtube\.com/(?:watch\?(?:[^\s]*&)?v=|shorts/|live/|embed/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})",
    re.IGNORECASE,
)

_VALID_VID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_FILENAME_SAFE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def detect_youtube_url(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Strictly validate and canonicalize a YouTube URL to prevent SSRF or non-YT URLs."""
    if not text:
        return None, None
    cleaned = text.strip()
    match = _YOUTUBE_URL_RE.match(cleaned) or _YOUTUBE_URL_SEARCH_RE.search(cleaned)
    if not match:
        return None, None
    video_id = match.group(1)
    if not _VALID_VID_RE.match(video_id):
        return None, None
    return f"https://www.youtube.com/watch?v={video_id}", video_id


def sanitize_filename(name: str, ext: str, suffix: str = "", max_len: int = 90) -> str:
    clean = _FILENAME_SAFE_RE.sub("", (name or "srking_media")).strip(" .")
    clean = re.sub(r"\s+", " ", clean)
    if not clean:
        clean = "srking_media"
    if suffix:
        max_base = max(20, max_len - len(suffix) - 1)
        clean = f"{clean[:max_base].rstrip(' .')} {suffix}"
    else:
        clean = clean[:max_len].rstrip(" .")
    return f"{clean}.{ext.lstrip('.')}"


def human_size(num: Optional[float]) -> str:
    if not num or num <= 0:
        return ""
    val = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1024:
            return f"{val:.1f} {unit}"
        val /= 1024
    return f"{val:.2f} TB"


def format_duration(seconds: Optional[int]) -> str:
    if not seconds or seconds <= 0:
        return "--:--"
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# ======================================================================================
#  RUNTIME CAPABILITIES & SAFE SUBPROCESS RUNNER
# ======================================================================================

NODE_PATH = shutil.which("node")
FFMPEG_PATH = shutil.which("ffmpeg")
if not NODE_PATH:
    log.warning("Node.js not found — YouTube nsig challenges may fail on some videos.")
if not FFMPEG_PATH:
    log.warning("ffmpeg not found — HD video merging, MP3 conversion, and 16D spatial DSP disabled.")

_UPDATE_LOCK = threading.Lock()


def get_ytdlp_version() -> str:
    return getattr(yt_dlp.version, "__version__", "unknown")


def update_ytdlp_sync() -> Tuple[bool, str]:
    """Manually triggered safe yt-dlp update for admin maintenance."""
    if not _UPDATE_LOCK.acquire(blocking=False):
        return False, "An update is already running."
    try:
        base = [sys.executable, "-m", "pip", "install", "-U", "--no-cache-dir"]
        ok = False
        for extra in ([], ["--break-system-packages"]):
            try:
                res = subprocess.run(
                    base + extra + ["yt-dlp"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=300,
                )
                if res.returncode == 0:
                    ok = True
                    break
            except Exception:
                continue
        if ok:
            loaded = get_ytdlp_version()
            new_ver = loaded
            try:
                ver_res = subprocess.run(
                    [sys.executable, "-c", "import yt_dlp; print(yt_dlp.version.__version__)"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if ver_res.returncode == 0 and ver_res.stdout.strip():
                    new_ver = ver_res.stdout.strip()
            except Exception:
                pass
            if new_ver != loaded:
                return True, f"Updated yt-dlp to {new_ver} (loaded: {loaded}). Restart service to activate."
            return True, f"yt-dlp is already up to date ({loaded})."
        return False, "pip install yt-dlp failed. Check container permissions or network."
    finally:
        _UPDATE_LOCK.release()


def _run_cancellable_subprocess(
    cmd: List[str],
    timeout: int = 600,
    cancel_event: Optional[threading.Event] = None,
    register_proc: Optional[Callable[[Optional[subprocess.Popen]], None]] = None,
) -> subprocess.CompletedProcess:
    """Run an external command (like FFmpeg) while allowing immediate cancellation."""
    if cancel_event and cancel_event.is_set():
        raise DownloadCancelled("Download cancelled.")

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if register_proc:
        register_proc(proc)

    start = time.monotonic()
    try:
        while proc.poll() is None:
            if cancel_event and cancel_event.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise DownloadCancelled("Download cancelled.")
            if (time.monotonic() - start) > timeout:
                proc.kill()
                raise DownloadError("Media processing timed out. Try a lower resolution or shorter video.")
            time.sleep(0.2)
        stdout, stderr = proc.communicate()
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    finally:
        if register_proc:
            register_proc(None)


# ======================================================================================
#  DISK CACHE (MEDIA-TYPE + QUALITY AWARE, ATOMIC, BOUNDED)
# ======================================================================================

_CACHE_LOCK = threading.Lock()


def _link_or_copy(src: str, dst: str) -> str:
    if hasattr(os, "link"):
        try:
            os.link(src, dst)
            return dst
        except OSError:
            pass
    shutil.copy2(src, dst)
    return dst


def _disk_cache_key(video_id: str, media_type: str, quality: str) -> str:
    return f"{video_id}__{media_type}__{quality}"


def _cache_meta_path(cache_key: str) -> str:
    return os.path.join(config.CACHE_DIR, f"{cache_key}.json")


def _cache_evict(cache_key: str) -> None:
    meta_path = _cache_meta_path(cache_key)
    ext = None
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                ext = json.load(f).get("ext")
        except Exception:
            pass
    candidates = [meta_path, meta_path + ".tmp"]
    if ext:
        candidates.append(os.path.join(config.CACHE_DIR, f"{cache_key}.{ext}"))
        candidates.append(os.path.join(config.CACHE_DIR, f"{cache_key}.{ext}.tmp"))
    for known_ext in ("mp4", "mkv", "webm", "mp3", "m4a", "opus"):
        candidates.append(os.path.join(config.CACHE_DIR, f"{cache_key}.{known_ext}"))
        candidates.append(os.path.join(config.CACHE_DIR, f"{cache_key}.{known_ext}.tmp"))
    for path in set(candidates):
        try:
            os.remove(path)
        except OSError:
            pass


def disk_cache_lookup(
    video_id: str, media_type: str, quality: str
) -> Optional[Tuple[str, Dict[str, Any]]]:
    if not config.CACHE_ENABLED:
        return None
    cache_key = _disk_cache_key(video_id, media_type, quality)
    meta_path = _cache_meta_path(cache_key)
    if not os.path.isfile(meta_path):
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        ext = meta.get("ext") or "mp4"
        media_path = os.path.join(config.CACHE_DIR, f"{cache_key}.{ext}")
        if not os.path.isfile(media_path) or os.path.getsize(media_path) == 0:
            _cache_evict(cache_key)
            return None
        if time.time() - os.path.getmtime(media_path) > config.CACHE_TTL_SECONDS:
            _cache_evict(cache_key)
            return None
        os.utime(media_path, None)
        os.utime(meta_path, None)
        return media_path, meta
    except Exception:
        _cache_evict(cache_key)
        return None


def disk_cache_save(
    video_id: str, media_type: str, quality: str, filepath: str, meta: Dict[str, Any]
) -> None:
    if not config.CACHE_ENABLED:
        return
    try:
        if os.path.getsize(filepath) > config.CACHE_SINGLE_FILE_MAX_BYTES:
            return
    except OSError:
        return

    cache_key = _disk_cache_key(video_id, media_type, quality)
    ext = (meta.get("ext") or os.path.splitext(filepath)[1].lstrip(".") or "mp4").lower()
    media_path = os.path.join(config.CACHE_DIR, f"{cache_key}.{ext}")
    meta_path = _cache_meta_path(cache_key)

    with _CACHE_LOCK:
        if os.path.isfile(media_path) and os.path.isfile(meta_path):
            return
        tmp_media = media_path + ".tmp"
        tmp_meta = meta_path + ".tmp"
        try:
            _link_or_copy(filepath, tmp_media)
            os.replace(tmp_media, media_path)
            with open(tmp_meta, "w", encoding="utf-8") as f:
                json.dump(meta, f)
            os.replace(tmp_meta, meta_path)
        except Exception as e:
            log.warning("Disk cache save failed: %s", e)
            for p in (tmp_media, tmp_meta):
                try:
                    os.remove(p)
                except OSError:
                    pass
            return
        _cache_enforce_limit_locked()


def _cache_enforce_limit_locked() -> None:
    try:
        entries = []
        total = 0
        for name in os.listdir(config.CACHE_DIR):
            if name.endswith(".json") or name.endswith(".tmp"):
                continue
            path = os.path.join(config.CACHE_DIR, name)
            try:
                st = os.stat(path)
                key = os.path.splitext(name)[0]
                entries.append((st.st_mtime, key, st.st_size))
                total += st.st_size
            except OSError:
                continue
        if total <= config.CACHE_MAX_BYTES:
            return
        entries.sort()
        for _, key, size in entries:
            if total <= config.CACHE_MAX_BYTES:
                break
            _cache_evict(key)
            total -= size
    except OSError:
        pass


def disk_cache_stats() -> Tuple[int, int]:
    count, total = 0, 0
    try:
        for name in os.listdir(config.CACHE_DIR):
            if name.endswith(".json") or name.endswith(".tmp"):
                continue
            path = os.path.join(config.CACHE_DIR, name)
            try:
                if os.path.isfile(path):
                    count += 1
                    total += os.path.getsize(path)
            except OSError:
                continue
    except OSError:
        pass
    return count, total


def disk_cache_clear() -> Tuple[int, int]:
    removed, freed = 0, 0
    with _CACHE_LOCK:
        try:
            for name in os.listdir(config.CACHE_DIR):
                path = os.path.join(config.CACHE_DIR, name)
                try:
                    if os.path.isfile(path):
                        size = os.path.getsize(path)
                        os.remove(path)
                        if not name.endswith(".json"):
                            removed += 1
                        freed += size
                except OSError:
                    continue
        except OSError:
            pass
    return removed, freed


def cleanup_expired_disk_cache() -> int:
    if not config.CACHE_ENABLED:
        return 0
    now = time.time()
    removed = 0
    with _CACHE_LOCK:
        try:
            for name in os.listdir(config.CACHE_DIR):
                if not name.endswith(".json"):
                    continue
                key = name[:-5]
                meta_path = os.path.join(config.CACHE_DIR, name)
                try:
                    if now - os.path.getmtime(meta_path) > config.CACHE_TTL_SECONDS:
                        _cache_evict(key)
                        removed += 1
                except OSError:
                    continue
        except OSError:
            pass
    return removed


# ======================================================================================
#  7-ZONE TRUE 3D HRTF BEAT-SYNCED 16D SPATIAL AUDIO ENGINE
# ======================================================================================


def estimate_bpm_and_phase(
    filepath: str,
    duration: int = 0,
    cancel_event: Optional[threading.Event] = None,
    register_proc: Optional[Callable[[Optional[subprocess.Popen]], None]] = None,
) -> Tuple[float, float]:
    """Fast zero-dependency BPM + beat-phase detector using onset autocorrelation (~0.25s)."""
    if not FFMPEG_PATH or not os.path.isfile(filepath):
        return 120.0, 0.0
    try:
        start_s = int(duration * 0.25) if duration > 45 else 0
        res = _run_cancellable_subprocess(
            [
                FFMPEG_PATH,
                "-v", "quiet",
                "-ss", str(start_s),
                "-t", "24",
                "-i", filepath,
                "-ac", "1",
                "-ar", "8000",
                "-f", "s16le",
                "pipe:1",
            ],
            timeout=30,
            cancel_event=cancel_event,
            register_proc=register_proc,
        )
        raw = res.stdout
        if len(raw) < 16000:
            return 120.0, 0.0

        samples = array.array("h")
        samples.frombytes(raw[: len(raw) - (len(raw) % 2)])

        hop = 160
        fps = 8000.0 / hop  # 50.0 Hz
        n_frames = len(samples) // hop
        if n_frames < 100:
            return 120.0, 0.0

        energies = [
            math.sqrt(sum(x * x for x in samples[i * hop : (i + 1) * hop]) / hop)
            for i in range(n_frames)
        ]
        onset = [max(0.0, energies[i] - energies[i - 1]) for i in range(1, len(energies))]
        mean_o = sum(onset) / len(onset)
        if mean_o <= 1e-6:
            return 120.0, 0.0
        centered = [x - mean_o for x in onset]

        min_lag = int(fps * 60.0 / 160.0)
        max_lag = int(fps * 60.0 / 80.0)
        best_lag = 25
        best_score = -1.0
        n = len(centered)
        for lag in range(min_lag, min(max_lag + 1, n // 2)):
            score = sum(centered[i] * centered[i + lag] for i in range(n - lag))
            if score > best_score:
                best_score = score
                best_lag = lag

        bpm = max(80.0, min(160.0, round((fps * 60.0) / best_lag, 1)))

        best_phase_frame = 0
        best_phase_energy = -1.0
        for p in range(best_lag):
            e_sum = sum(onset[idx] for idx in range(p, n, best_lag))
            if e_sum > best_phase_energy:
                best_phase_energy = e_sum
                best_phase_frame = p
        phase_sec = round((best_phase_frame / fps) % (60.0 / bpm), 3)
        return bpm, phase_sec
    except DownloadCancelled:
        raise
    except Exception:
        return 120.0, 0.0


def build_16d_filter_complex(
    bpm: float,
    effective_dur: int,
    fade_in: float = 1.2,
    fade_out: float = 2.0,
    phase: float = 0.0,
) -> str:
    """True 7-Zone 3D HRTF Beat-Synced 16D Spatial Audio Engine."""
    beat_s = 60.0 / max(80.0, min(160.0, bpm))
    az_w = 2.0 * math.pi / (8.0 * beat_s)   # Full 360 horizontal orbit every 8 beats
    el_w = 2.0 * math.pi / (6.0 * beat_s)   # Full Top <-> Bottom wave every 6 beats
    bt_w = math.pi / beat_s                 # Beat-locked accent pulse

    t_expr = f"(t-{phase:.3f})"
    pulse = f"(1+0.12*pow(max(0,cos({bt_w:.4f}*{t_expr})),4))"

    g_front  = f"0.58*max(0,cos({az_w:.4f}*{t_expr}))*{pulse}"
    g_back   = f"0.46*max(0,-cos({az_w:.4f}*{t_expr}))*{pulse}"
    g_right  = f"0.78*max(0,sin({az_w:.4f}*{t_expr}))*{pulse}"
    g_left   = f"0.78*max(0,-sin({az_w:.4f}*{t_expr}))*{pulse}"
    g_top    = f"0.58*max(0,sin({el_w:.4f}*{t_expr}))*{pulse}"
    g_bottom = f"0.52*max(0,-sin({el_w:.4f}*{t_expr}))*{pulse}"

    post_chain = ["amix=inputs=7:normalize=0", "dynaudnorm", "alimiter=limit=0.95"]
    if fade_in > 0 and effective_dur > fade_in + 2:
        post_chain.append(f"afade=t=in:st=0:d={fade_in:.2f}")
    if fade_out > 0 and effective_dur > fade_out + 2:
        post_chain.append(f"afade=t=out:st={max(1.0, effective_dur - fade_out):.2f}:d={fade_out:.2f}")
    post_str = ",".join(post_chain)

    return (
        "[0:a]asplit=7[s_sub][s_fr][s_bk][s_lt][s_rt][s_tp][s_bt];"
        "[s_sub]lowpass=f=110,bass=g=3.0:f=60:w=0.6[sub];"
        f"[s_fr]highpass=f=110,equalizer=f=3200:t=q:w=1.2:g=4.5,equalizer=f=1200:t=q:w=1.5:g=1.5,"
        f"volume='{g_front}':eval=frame[fr];"
        f"[s_bk]highpass=f=110,equalizer=f=4000:t=q:w=1.0:g=-5.5,extrastereo=m=1.4,"
        f"aecho=0.8:0.75:24|48:0.28|0.16,volume='{g_back}':eval=frame[bk];"
        f"[s_lt]highpass=f=110,adelay=0|0.55,pan=stereo|c0=1.0*c0|c1=0.15*c1,"
        f"volume='{g_left}':eval=frame[lt];"
        f"[s_rt]highpass=f=110,adelay=0.55|0,pan=stereo|c0=0.15*c0|c1=1.0*c1,"
        f"volume='{g_right}':eval=frame[rt];"
        f"[s_tp]highpass=f=220,equalizer=f=4500:t=q:w=1.5:g=-6.0,equalizer=f=8200:t=q:w=1.8:g=7.0,"
        f"aecho=0.9:0.7:16:0.18,volume='{g_top}':eval=frame[tp];"
        f"[s_bt]highpass=f=110,lowpass=f=5200,equalizer=f=380:t=q:w=1.2:g=5.0,"
        f"aecho=0.9:0.7:12:0.20,volume='{g_bottom}':eval=frame[bt];"
        f"[sub][fr][bk][lt][rt][tp][bt]{post_str}[aout]"
    )


# ======================================================================================
#  YT-DLP CLIENT ROTATION & ERROR CLASSIFICATION
# ======================================================================================

_CLIENT_LOCK = threading.Lock()
_LAST_WORKING_CLIENT: Optional[str] = None

_PREFERRED_CLIENTS = ["tv_embedded", "tv", "android_vr", "android", "web_safari"]

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
]

_RETRYABLE_MARKERS = (
    "403", "forbidden", "sign in", "signin", "confirm you", "not a bot",
    "login", "log in", "bot check", "player response",
    "unable to extract player", "nsig", "n challenge", "javascript runtime",
    "content isn't available", "requested format is not available",
    "no video formats found", "no formats found",
    "page needs to be reloaded", "reload the page",
    "unable to download video data", "http error 403",
)


def get_last_working_client() -> Optional[str]:
    with _CLIENT_LOCK:
        return _LAST_WORKING_CLIENT


def set_last_working_client(client: Optional[str]) -> None:
    global _LAST_WORKING_CLIENT
    with _CLIENT_LOCK:
        _LAST_WORKING_CLIENT = client


def _build_client_order() -> List[str]:
    order = list(_PREFERRED_CLIENTS)
    current = get_last_working_client()
    if current and current in order:
        order.remove(current)
        order.insert(0, current)
    return order


def _is_retryable(msg: str) -> bool:
    lower = msg.lower()
    return any(marker in lower for marker in _RETRYABLE_MARKERS)


def _classify_ydl_error(err: Exception) -> DownloadError:
    if isinstance(err, DownloadError):
        return err
    msg = str(err).lower()
    if "page needs to be reloaded" in msg or "reload the page" in msg:
        return DownloadError("YouTube triggered a temporary anti-bot check. Please retry in a moment.")
    if "sign in" in msg or "confirm you" in msg or "not a bot" in msg:
        return DownloadError("YouTube requested verification for this server IP. Retry shortly or configure YT_COOKIES_BASE64.")
    if "403" in msg or "forbidden" in msg:
        return DownloadError("YouTube returned HTTP 403. Please retry in a moment.")
    if "private" in msg:
        return DownloadError("This YouTube video is private and cannot be downloaded.")
    if "age" in msg or "explicit" in msg:
        return DownloadError("This video is age-restricted and requires YouTube cookies.")
    if "live" in msg or "premiere" in msg:
        return DownloadError("Live streams and upcoming premieres are not supported.")
    if "rate-limit" in msg or "429" in msg:
        return DownloadError("YouTube rate limit reached. Please wait a few minutes and try again.")
    if "unavailable" in msg or "removed" in msg or "copyright" in msg:
        return DownloadError("This YouTube video is unavailable or has been removed.")
    if "no video formats found" in msg or "requested format is not available" in msg:
        return DownloadError("Requested quality is not available for this video. Try another quality.")
    return DownloadError("Could not process this YouTube link. Please verify the URL and retry.")


def _yt_extractor_args(player_client: Optional[List[str]] = None) -> Dict[str, Any]:
    yt_args: Dict[str, Any] = {"formats": ["missing_pot"]}
    if player_client:
        yt_args["player_client"] = player_client
    return {"youtube": yt_args}


def _base_ydl_opts(
    out_template: Optional[str] = None,
    player_client: Optional[List[str]] = None,
    progress_hook: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "nocheckcertificate": config.YT_SKIP_TLS_VERIFY,
        "geo_bypass": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 20,
        "cachedir": False,
        "extractor_retries": 1,
        "concurrent_fragment_downloads": 8,
        "http_chunk_size": 4 * 1024 * 1024,
        "buffersize": 8 * 1024 * 1024,
        "extractor_args": _yt_extractor_args(player_client),
        "http_headers": {
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.youtube.com/",
        },
    }
    if out_template:
        opts["outtmpl"] = out_template
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
    if NODE_PATH:
        opts["js_runtimes"] = {"node": {}}
    if config.YT_COOKIES_FILE and os.path.isfile(config.YT_COOKIES_FILE):
        opts["cookiefile"] = config.YT_COOKIES_FILE
    return opts


# ======================================================================================
#  METADATA INSPECTION (WITH 10-MIN IN-MEMORY CACHE)
# ======================================================================================

_INSPECT_CACHE_LOCK = threading.Lock()
_INSPECT_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def _estimate_stream_bytes(fmt: Dict[str, Any], duration: int) -> int:
    size = fmt.get("filesize") or fmt.get("filesize_approx")
    if size and size > 0:
        return int(size)
    tbr = fmt.get("tbr") or fmt.get("vbr") or fmt.get("abr")
    if tbr and duration > 0:
        return int((float(tbr) * 1000.0 / 8.0) * duration)
    return 0


def _build_inspect_payload(meta: Dict[str, Any], video_id: str) -> Dict[str, Any]:
    duration = int(meta.get("duration") or 0)
    title = (meta.get("title") or "Untitled Video").strip()[:180]
    channel = (meta.get("channel") or meta.get("uploader") or "YouTube").strip()[:100]
    thumbnail = meta.get("thumbnail") or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    formats = meta.get("formats") or []

    # Best audio stream size estimate (for adding to video-only streams)
    audio_streams = [
        f for f in formats
        if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
    ]
    best_audio_bytes = 0
    best_m4a_bytes = 0
    for af in audio_streams:
        b = _estimate_stream_bytes(af, duration)
        if b > best_audio_bytes:
            best_audio_bytes = b
        if af.get("ext") == "m4a" and b > best_m4a_bytes:
            best_m4a_bytes = b

    # Group available video heights cleanly
    best_by_height: Dict[int, Dict[str, Any]] = {}
    for f in formats:
        vcodec = f.get("vcodec")
        height = f.get("height")
        if not height or vcodec in (None, "none"):
            continue
        # Map height to standard bucket
        bucket = None
        for h in config.VIDEO_HEIGHTS:
            if height >= h - 40:
                bucket = h
                break
        if bucket is None:
            continue

        is_mp4 = (f.get("ext") == "mp4")
        fps = int(round(f.get("fps") or 30))
        tbr = float(f.get("tbr") or f.get("vbr") or 0)
        has_audio = f.get("acodec") not in (None, "none")
        est_bytes = _estimate_stream_bytes(f, duration)
        if not has_audio and est_bytes > 0:
            est_bytes += best_audio_bytes

        # Prefer MP4 container, higher fps, higher bitrate
        score = (1 if is_mp4 else 0, 1 if fps >= 50 else 0, tbr)
        prev = best_by_height.get(bucket)
        if prev is None or score > prev["_score"]:
            best_by_height[bucket] = {
                "id": f"{bucket}p",
                "height": bucket,
                "label": config.VIDEO_LABELS.get(bucket, f"{bucket}p"),
                "fps": fps,
                "container": "MP4",
                "estimated_bytes": est_bytes,
                "estimated_size": human_size(est_bytes) if est_bytes else "Auto",
                "_score": score,
            }

    video_options = []
    for h in config.VIDEO_HEIGHTS:
        if h in best_by_height:
            item = dict(best_by_height[h])
            item.pop("_score", None)
            video_options.append(item)

    # Fallback if YouTube returned only progressive or unusual heights
    if not video_options:
        video_options.append(
            {
                "id": "720p",
                "height": 720,
                "label": "Best Available MP4",
                "fps": 30,
                "container": "MP4",
                "estimated_bytes": 0,
                "estimated_size": "Auto",
            }
        )

    # Audio presets with estimated sizes
    audio_options = []
    for key, preset in config.AUDIO_PRESETS.items():
        if preset["transcode"]:
            est = int((int(preset["bitrate"]) * 1000 / 8) * duration) if duration > 0 else 0
        else:
            est = best_m4a_bytes or best_audio_bytes
        audio_options.append(
            {
                "id": key,
                "label": preset["label"],
                "badge": preset["badge"],
                "desc": preset["desc"],
                "ext": str(preset["ext"]).upper(),
                "estimated_bytes": est,
                "estimated_size": human_size(est) if est else "Auto",
            }
        )

    # 16D Spatial presets
    spatial_options = []
    for key, preset in config.SPATIAL_16D_PRESETS.items():
        est = int((int(preset["bitrate"]) * 1000 / 8) * duration) if duration > 0 else 0
        spatial_options.append(
            {
                "id": key,
                "label": preset["label"],
                "badge": preset["badge"],
                "desc": preset["desc"],
                "ext": "MP3",
                "estimated_bytes": est,
                "estimated_size": human_size(est) if est else "Auto",
            }
        )

    return {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": title,
        "channel": channel,
        "duration": duration,
        "duration_text": format_duration(duration),
        "thumbnail": thumbnail,
        "video_options": video_options,
        "audio_options": audio_options,
        "spatial_options": spatial_options,
        "ffmpeg_available": bool(FFMPEG_PATH),
    }


def inspect_youtube_sync(
    video_id: str,
    on_client_success: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Inspect a YouTube video and return metadata + available download qualities."""
    now = time.time()
    with _INSPECT_CACHE_LOCK:
        cached = _INSPECT_CACHE.get(video_id)
        if cached and (now - cached[0]) < config.INSPECT_CACHE_TTL_SECONDS:
            return cached[1]

    url = f"https://www.youtube.com/watch?v={video_id}"
    last_err: Optional[Exception] = None
    clients = _build_client_order()

    for client in clients:
        opts = _base_ydl_opts(player_client=[client])
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                meta = ydl.extract_info(url, download=False)
                if meta is None:
                    raise DownloadError("No downloadable media found for this YouTube link.")
                if "entries" in meta and meta["entries"]:
                    meta = meta["entries"][0]
                if meta.get("is_live") or meta.get("live_status") in ("is_live", "is_upcoming"):
                    raise DownloadError("Live streams and upcoming premieres cannot be downloaded.")
                duration = int(meta.get("duration") or 0)
                if duration > config.MAX_DURATION_SECONDS:
                    max_hrs = config.MAX_DURATION_SECONDS / 3600
                    raise DownloadError(
                        f"Video is too long ({format_duration(duration)}). Maximum supported length is {max_hrs:.1f} hours."
                    )
                set_last_working_client(client)
                if on_client_success:
                    on_client_success(client)
                payload = _build_inspect_payload(meta, video_id)
                with _INSPECT_CACHE_LOCK:
                    if len(_INSPECT_CACHE) > 250:
                        _INSPECT_CACHE.clear()
                    _INSPECT_CACHE[video_id] = (now, payload)
                return payload
        except DownloadError:
            raise
        except Exception as e:
            last_err = e
            if not _is_retryable(str(e)):
                raise _classify_ydl_error(e)
            continue

    if last_err:
        raise _classify_ydl_error(last_err)
    raise DownloadError("YouTube blocked all extraction clients. Please retry shortly.")


# ======================================================================================
#  MEDIA DOWNLOAD & PROCESSING PIPELINE
# ======================================================================================


def _clear_workdir(work_dir: str) -> None:
    for name in os.listdir(work_dir):
        p = os.path.join(work_dir, name)
        try:
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                os.remove(p)
        except OSError:
            pass


def _locate_downloaded_file(work_dir: str) -> str:
    files = [
        os.path.join(work_dir, f)
        for f in os.listdir(work_dir)
        if os.path.isfile(os.path.join(work_dir, f))
        and not f.endswith((".part", ".ytdl", ".tmp", ".jpg", ".jpeg", ".png", ".webp", ".json"))
    ]
    if not files:
        raise DownloadError("Downloaded media file could not be located.")
    return max(files, key=os.path.getsize)


def _locate_thumbnail_jpg(
    work_dir: str,
    cancel_event: Optional[threading.Event] = None,
    register_proc: Optional[Callable[[Optional[subprocess.Popen]], None]] = None,
) -> Optional[str]:
    for name in os.listdir(work_dir):
        if name.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            path = os.path.join(work_dir, name)
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                if path.lower().endswith((".webp", ".png")) and FFMPEG_PATH:
                    jpg_path = os.path.splitext(path)[0] + "_cover.jpg"
                    res = _run_cancellable_subprocess(
                        [FFMPEG_PATH, "-y", "-i", path, "-vf", "scale=600:-1", jpg_path],
                        timeout=30,
                        cancel_event=cancel_event,
                        register_proc=register_proc,
                    )
                    if res.returncode == 0 and os.path.isfile(jpg_path) and os.path.getsize(jpg_path) > 0:
                        return jpg_path
                elif path.lower().endswith((".jpg", ".jpeg")):
                    return path
    return None


def _encode_audio_mp3(
    src_path: str,
    work_dir: str,
    video_id: str,
    bitrate_kbps: int,
    title: str,
    artist: str,
    cover_path: Optional[str] = None,
    filter_complex: Optional[str] = None,
    cancel_event: Optional[threading.Event] = None,
    register_proc: Optional[Callable[[Optional[subprocess.Popen]], None]] = None,
) -> str:
    if not FFMPEG_PATH:
        raise DownloadError("FFmpeg is not installed on the server.")

    out_path = os.path.join(work_dir, f"{video_id}_out.mp3")
    cmd = [FFMPEG_PATH, "-y", "-i", src_path]

    has_cover = bool(cover_path and os.path.isfile(cover_path))
    if has_cover:
        cmd += ["-i", cover_path]

    if filter_complex:
        cmd += ["-filter_complex", filter_complex, "-map", "[aout]"]
    else:
        cmd += ["-map", "0:a:0"]

    if has_cover:
        cmd += [
            "-map", "1:v:0",
            "-c:v", "mjpeg",
            "-id3v2_version", "3",
            "-metadata:s:v", "title=Album cover",
            "-metadata:s:v", "comment=Cover (front)",
        ]
    else:
        cmd += ["-vn", "-id3v2_version", "3"]

    cmd += [
        "-c:a", "libmp3lame",
        "-b:a", f"{bitrate_kbps}k",
        "-ar", "44100",
        "-ac", "2",
        "-metadata", f"title={title}",
        "-metadata", f"artist={artist}",
        out_path,
    ]

    res = _run_cancellable_subprocess(
        cmd, timeout=900, cancel_event=cancel_event, register_proc=register_proc
    )
    if res.returncode != 0 or not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
        # Retry without cover art if cover attachment caused an issue
        if has_cover:
            return _encode_audio_mp3(
                src_path=src_path,
                work_dir=work_dir,
                video_id=video_id,
                bitrate_kbps=bitrate_kbps,
                title=title,
                artist=artist,
                cover_path=None,
                filter_complex=filter_complex,
                cancel_event=cancel_event,
                register_proc=register_proc,
            )
        raise DownloadError("FFmpeg failed while encoding audio. Please try another quality.")

    try:
        os.remove(src_path)
    except OSError:
        pass
    return out_path


def download_media_sync(
    video_id: str,
    media_type: str,
    quality: str,
    work_dir: str,
    cancel_event: Optional[threading.Event] = None,
    on_progress: Optional[Callable[[str, int, str, str], None]] = None,
    register_proc: Optional[Callable[[Optional[subprocess.Popen]], None]] = None,
    on_client_success: Optional[Callable[[str], None]] = None,
) -> MediaResult:
    """
    Download and process YouTube media into work_dir.
    media_type: 'video' | 'audio' | 'spatial16d'
    on_progress(stage, percent, speed_text, eta_text)
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    out_template = os.path.join(work_dir, "%(id)s.%(ext)s")

    # Determine format chain
    if media_type == "video":
        try:
            height = int(quality.lower().rstrip("p"))
        except ValueError:
            height = 1080
        if height not in config.VIDEO_HEIGHTS:
            height = 1080
        quality = f"{height}p"
        if FFMPEG_PATH:
            format_chain = [
                f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
                f"bestvideo[height<={height}]+bestaudio/"
                f"best[height<={height}][ext=mp4]/best[height<={height}]/best",
                f"best[height<={height}]/best",
            ]
        else:
            format_chain = [f"best[height<={height}][ext=mp4]/best[height<={height}]/best"]
        need_thumb = False
    elif media_type == "audio":
        if quality not in config.AUDIO_PRESETS:
            quality = "mp3_320"
        preset = config.AUDIO_PRESETS[quality]
        if quality == "m4a_orig":
            format_chain = ["bestaudio[ext=m4a]/bestaudio/best", "best[acodec!=none]"]
            need_thumb = False
        else:
            format_chain = ["bestaudio/best", "best[acodec!=none]"]
            need_thumb = True
    elif media_type == "spatial16d":
        if quality not in config.SPATIAL_16D_PRESETS:
            quality = "16d_320"
        format_chain = ["bestaudio/best", "best[acodec!=none]"]
        need_thumb = True
    else:
        raise DownloadError("Unsupported media type.")

    last_report = [0.0]

    def _report(stage: str, pct: int, speed: str = "", eta: str = "") -> None:
        if on_progress:
            on_progress(stage, max(0, min(100, pct)), speed, eta)

    def _progress_hook(d: Dict[str, Any]) -> None:
        if cancel_event and cancel_event.is_set():
            raise DownloadCancelled("Download cancelled.")
        status = d.get("status")
        now = time.monotonic()
        if status == "downloading" and (now - last_report[0]) >= 0.6:
            last_report[0] = now
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            speed = d.get("speed") or 0
            eta = d.get("eta")
            speed_str = f"{human_size(speed)}/s" if speed else ""
            eta_str = format_duration(int(eta)) if eta and eta > 0 else ""
            # Scale download progress to 5..78% so post-processing has clean headroom
            if total > 0:
                raw_pct = int(downloaded * 100 / total)
                scaled = 5 + int(raw_pct * 0.73)
                label = "Downloading video & audio" if media_type == "video" else "Downloading audio stream"
                _report(f"{label} ({raw_pct}%)", scaled, speed_str, eta_str)
            else:
                _report(f"Downloading ({human_size(downloaded)})", 35, speed_str, eta_str)
        elif status == "finished":
            if media_type == "video":
                _report("Merging video & audio into MP4", 82)
            else:
                _report("Stream downloaded, preparing audio", 78)

    _report("Connecting to YouTube", 3)

    info = None
    last_err: Optional[Exception] = None
    clients = _build_client_order()

    for client in clients:
        for fmt in format_chain:
            if cancel_event and cancel_event.is_set():
                raise DownloadCancelled("Download cancelled.")
            _clear_workdir(work_dir)
            opts = _base_ydl_opts(out_template, player_client=[client], progress_hook=_progress_hook)
            opts["format"] = fmt
            if media_type == "video":
                opts["merge_output_format"] = "mp4"
                opts["format_sort"] = ["res", "fps", "vcodec:h264", "acodec:m4a"]
                if FFMPEG_PATH:
                    opts["postprocessor_args"] = {"merger": ["-movflags", "+faststart"]}
            else:
                opts["format_sort"] = ["abr", "asr"]
                opts["writethumbnail"] = need_thumb

            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    meta = ydl.extract_info(url, download=False)
                    if meta is None:
                        raise DownloadError("No downloadable media found at this YouTube link.")
                    if "entries" in meta and meta["entries"]:
                        meta = meta["entries"][0]
                    if meta.get("is_live") or meta.get("live_status") in ("is_live", "is_upcoming"):
                        raise DownloadError("Live streams and upcoming premieres are not supported.")
                    duration = int(meta.get("duration") or 0)
                    if duration > config.MAX_DURATION_SECONDS:
                        raise DownloadError(
                            f"Video exceeds max length ({format_duration(config.MAX_DURATION_SECONDS)})."
                        )
                    if cancel_event and cancel_event.is_set():
                        raise DownloadCancelled("Download cancelled.")
                    info = ydl.process_ie_result(meta, download=True)
                    if "entries" in info and info["entries"]:
                        info = info["entries"][0]
                set_last_working_client(client)
                if on_client_success:
                    on_client_success(client)
                break
            except DownloadError:
                raise
            except Exception as e:
                last_err = e
                if not _is_retryable(str(e)):
                    raise _classify_ydl_error(e)
                continue
        if info is not None:
            break

    if info is None:
        if last_err:
            raise _classify_ydl_error(last_err)
        raise DownloadError("YouTube blocked all download attempts. Please retry shortly.")

    if cancel_event and cancel_event.is_set():
        raise DownloadCancelled("Download cancelled.")

    filepath = _locate_downloaded_file(work_dir)
    duration = int(info.get("duration") or 0)
    title = (info.get("title") or "SR King Media").strip()[:160]
    channel = (info.get("artist") or info.get("channel") or info.get("uploader") or "YouTube").strip()[:100]
    detected_bpm: Optional[float] = None

    if media_type == "audio":
        preset = config.AUDIO_PRESETS[quality]
        if preset["transcode"] and FFMPEG_PATH:
            _report(f"Converting to {preset['label']} with cover art", 85)
            cover_jpg = _locate_thumbnail_jpg(work_dir, cancel_event=cancel_event, register_proc=register_proc)
            filepath = _encode_audio_mp3(
                src_path=filepath,
                work_dir=work_dir,
                video_id=video_id,
                bitrate_kbps=int(preset["bitrate"]),
                title=title,
                artist=channel,
                cover_path=cover_jpg,
                cancel_event=cancel_event,
                register_proc=register_proc,
            )
        ext = os.path.splitext(filepath)[1].lstrip(".").lower() or str(preset["ext"])
        clean_name = sanitize_filename(title, ext)

    elif media_type == "spatial16d":
        if not FFMPEG_PATH:
            raise DownloadError("FFmpeg is required for 16D Spatial Audio rendering.")
        preset = config.SPATIAL_16D_PRESETS[quality]
        _report("Analyzing track BPM & beat phase", 80)
        bpm, phase = estimate_bpm_and_phase(
            filepath, duration, cancel_event=cancel_event, register_proc=register_proc
        )
        detected_bpm = bpm
        _report(f"Rendering 7-Zone 16D Spatial Audio ({bpm:.0f} BPM)", 86)
        fc = build_16d_filter_complex(bpm=bpm, effective_dur=duration, fade_in=1.2, fade_out=2.0, phase=phase)
        cover_jpg = _locate_thumbnail_jpg(work_dir, cancel_event=cancel_event, register_proc=register_proc)
        filepath = _encode_audio_mp3(
            src_path=filepath,
            work_dir=work_dir,
            video_id=video_id,
            bitrate_kbps=int(preset["bitrate"]),
            title=f"{title} (16D Spatial)",
            artist=channel,
            cover_path=cover_jpg,
            filter_complex=fc,
            cancel_event=cancel_event,
            register_proc=register_proc,
        )
        ext = "mp3"
        clean_name = sanitize_filename(title, ext, suffix="[16D Spatial]")

    else:
        ext = os.path.splitext(filepath)[1].lstrip(".").lower() or "mp4"
        clean_name = sanitize_filename(title, ext, suffix=f"[{quality}]")

    file_size = os.path.getsize(filepath)
    if file_size > config.MAX_FILE_SIZE_BYTES:
        raise DownloadError(
            f"File size ({human_size(file_size)}) exceeds server limit ({human_size(config.MAX_FILE_SIZE_BYTES)})."
        )

    _report("Ready to download", 100)
    return MediaResult(
        filepath=filepath,
        filename=clean_name,
        work_dir=work_dir,
        video_id=video_id,
        title=title,
        channel=channel,
        duration=duration,
        file_size=file_size,
        ext=ext,
        media_type=media_type,
        quality=quality,
        from_cache=False,
        bpm=detected_bpm,
    )
