#!/usr/bin/env python3
"""Centralized configuration, quality presets, cookie bootstrap, and Railway runtime detection."""

import base64
import os
import stat
from typing import Dict, List, Optional


# ======================================================================================
#  OPTIONAL .env LOADER (ZERO EXTERNAL DEPENDENCIES)
# ======================================================================================

def _load_dotenv() -> None:
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(root_dir, ".env")
    if not os.path.isfile(env_path):
        return
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except OSError:
        pass


_load_dotenv()


# ======================================================================================
#  ENVIRONMENT DETECTION
# ======================================================================================

def is_railway() -> bool:
    return bool(
        os.environ.get("RAILWAY_ENVIRONMENT")
        or os.environ.get("RAILWAY_PROJECT_ID")
    )


def is_container() -> bool:
    return (
        os.path.exists("/.dockerenv")
        or os.path.exists("/run/.containerenv")
        or is_railway()
    )


def runtime_env_name() -> str:
    if is_railway():
        return "Railway"
    if is_container():
        return "Container"
    return "Local"


# ======================================================================================
#  CORE APPLICATION SETTINGS
# ======================================================================================

APP_NAME = "SR King"
APP_VERSION = "5.0.0"

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))

# Optional admin key for /api/admin/* maintenance endpoints
ADMIN_KEY = os.environ.get("ADMIN_KEY", "").strip()

# Storage paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")

DOWNLOAD_ROOT = os.environ.get(
    "DOWNLOAD_ROOT",
    "/tmp/srking_web_data" if is_container() else os.path.join(BASE_DIR, "data"),
)
CACHE_DIR = os.path.join(DOWNLOAD_ROOT, "cache")
JOBS_DIR = os.path.join(DOWNLOAD_ROOT, "jobs")
DB_PATH = os.path.join(DOWNLOAD_ROOT, "srking_web.db")

os.makedirs(DOWNLOAD_ROOT, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(JOBS_DIR, exist_ok=True)


# ======================================================================================
#  YOUTUBE COOKIES (SUPPORTS FILE PATH OR RAILWAY BASE64 ENV VAR)
# ======================================================================================

def _resolve_cookies_file() -> Optional[str]:
    direct_path = os.environ.get("YT_COOKIES_FILE", "").strip()
    if direct_path and os.path.isfile(direct_path):
        return direct_path

    b64_payload = os.environ.get("YT_COOKIES_BASE64", "").strip()
    if b64_payload:
        try:
            decoded = base64.b64decode(b64_payload)
            cookie_out = os.path.join(DOWNLOAD_ROOT, ".yt_cookies.txt")
            with open(cookie_out, "wb") as f:
                f.write(decoded)
            try:
                os.chmod(cookie_out, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
            return cookie_out
        except Exception:
            return None
    return None


YT_COOKIES_FILE: Optional[str] = _resolve_cookies_file()
YT_SKIP_TLS_VERIFY = os.environ.get("YT_SKIP_TLS_VERIFY", "0") == "1"


# ======================================================================================
#  LIMITS, CACHING & CONCURRENCY
# ======================================================================================

# Supports full movies & long videos (default: 4 hours, 4 GB)
MAX_DURATION_SECONDS = int(os.environ.get("MAX_DURATION_SECONDS", str(4 * 3600)))
MAX_FILE_SIZE_BYTES = int(float(os.environ.get("MAX_FILE_SIZE_GB", "4.0")) * 1024 * 1024 * 1024)

# Cache & temporary job retention
CACHE_ENABLED = os.environ.get("CACHE_ENABLED", "1") == "1"
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", str(6 * 3600)))
CACHE_MAX_BYTES = int(float(os.environ.get("CACHE_MAX_GB", "3.0")) * 1024 * 1024 * 1024)
# Only cache files up to 500 MB on disk so huge 4K movies don't evict every song
CACHE_SINGLE_FILE_MAX_BYTES = int(float(os.environ.get("CACHE_SINGLE_FILE_MAX_MB", "500")) * 1024 * 1024)

JOB_TTL_SECONDS = int(os.environ.get("JOB_TTL_SECONDS", "1800"))  # 30 minutes
INSPECT_CACHE_TTL_SECONDS = 600  # 10 minutes

MAX_WORKERS = max(1, int(os.environ.get("MAX_WORKERS", "3")))
MAX_ACTIVE_JOBS_PER_IP = max(1, int(os.environ.get("MAX_ACTIVE_JOBS_PER_IP", "2")))

# Rate limiting (per IP)
RATE_LIMIT_INSPECT_PER_MIN = int(os.environ.get("RATE_LIMIT_INSPECT_PER_MIN", "25"))
RATE_LIMIT_DOWNLOAD_PER_MIN = int(os.environ.get("RATE_LIMIT_DOWNLOAD_PER_MIN", "12"))


# ======================================================================================
#  MEDIA QUALITY PRESETS (YOUTUBE VIDEO, AUDIO & 16D SPATIAL)
# ======================================================================================

VIDEO_HEIGHTS: List[int] = [2160, 1440, 1080, 720, 480, 360]

VIDEO_LABELS: Dict[int, str] = {
    2160: "4K Ultra HD (2160p)",
    1440: "2K Quad HD (1440p)",
    1080: "Full HD (1080p)",
    720: "HD (720p)",
    480: "SD (480p)",
    360: "Data Saver (360p)",
}

AUDIO_PRESETS: Dict[str, Dict[str, object]] = {
    "mp3_320": {
        "id": "mp3_320",
        "label": "MP3 320 kbps",
        "badge": "Studio",
        "desc": "Maximum MP3 fidelity with embedded cover art.",
        "ext": "mp3",
        "bitrate": 320,
        "transcode": True,
    },
    "mp3_192": {
        "id": "mp3_192",
        "label": "MP3 192 kbps",
        "badge": "High",
        "desc": "Crisp high quality with balanced file size.",
        "ext": "mp3",
        "bitrate": 192,
        "transcode": True,
    },
    "mp3_128": {
        "id": "mp3_128",
        "label": "MP3 128 kbps",
        "badge": "Standard",
        "desc": "Compact size for quick downloads.",
        "ext": "mp3",
        "bitrate": 128,
        "transcode": True,
    },
    "m4a_orig": {
        "id": "m4a_orig",
        "label": "Original M4A",
        "badge": "Fastest",
        "desc": "Direct YouTube AAC stream with zero re-encoding.",
        "ext": "m4a",
        "bitrate": 0,
        "transcode": False,
    },
}

SPATIAL_16D_PRESETS: Dict[str, Dict[str, object]] = {
    "16d_320": {
        "id": "16d_320",
        "label": "16D Spatial · MP3 320 kbps",
        "badge": "Studio Earbuds",
        "desc": "Auto-detected BPM · 7-Zone 3D HRTF orbit with centered sub-bass.",
        "ext": "mp3",
        "bitrate": 320,
    },
    "16d_192": {
        "id": "16d_192",
        "label": "16D Spatial · MP3 192 kbps",
        "badge": "Balanced Earbuds",
        "desc": "Auto-detected BPM · 7-Zone 3D HRTF orbit, faster render & smaller file.",
        "ext": "mp3",
        "bitrate": 192,
    },
}
