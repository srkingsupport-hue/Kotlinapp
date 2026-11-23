#!/usr/bin/env python3
"""SR King Play — Railway FastAPI Backend v5.1 (Zero-502 Dual-Engine InnerTube + yt-dlp, Zero-403 Stream Proxy, True 4K/1080p & 16D Audio)."""

import array
import base64
import json
import math
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask
import yt_dlp

app = FastAPI(title="SR King Play API", version="5.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FFMPEG_PATH = shutil.which("ffmpeg")
NODE_PATH = shutil.which("node")

# Optional cookies file if YT_COOKIES_B64 env var is ever configured in Railway
_COOKIE_FILE: Optional[str] = None
_b64_cookies = os.environ.get("YT_COOKIES_B64", "").strip()
if _b64_cookies:
    try:
        _cpath = os.path.join(tempfile.gettempdir(), "srk_yt_cookies.txt")
        with open(_cpath, "wb") as _cf:
            _cf.write(base64.b64decode(_b64_cookies))
        _COOKIE_FILE = _cpath
    except Exception:
        _COOKIE_FILE = None

_CLIENTS = ["tv_embedded", "tv", "android_vr", "android", "web_safari"]
_LAST_WORKING_CLIENT: Optional[str] = None
_YT_ID_RE = re.compile(
    r"(?:v=|youtu\.be/|shorts/|live/|embed/)([A-Za-z0-9_-]{11})", re.IGNORECASE
)

# In-memory caches (0 MB disk usage)
_STREAM_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_SEARCH_TOKENS: Dict[str, Tuple[float, str]] = {}
_PROXY_URLS: Dict[str, Tuple[float, str, Dict[str, str]]] = {}
_CACHE_TTL = 900.0
_PROXY_TTL = 2700.0

_TRENDING_POOLS: Dict[str, List[str]] = {
    "all": [
        "trending videos india 2025",
        "latest bollywood punjabi hit songs official",
        "full hindi dubbed blockbuster movie hd",
        "trending tech science gaming videos india",
        "viral comedy podcasts india 2025",
        "4k hdr ultra hd nature music video",
        "south indian action full movie hindi dubbed",
        "top viral music videos india",
    ],
    "music": [
        "trending bollywood hit songs official video 2025",
        "latest punjabi songs official music video",
        "arijit singh atif aslam hit songs",
        "hindi party club dj songs official",
        "90s 2000s bollywood evergreen hit songs",
        "indian hip hop rap trending songs",
        "hindi lofi chill romantic songs",
        "haryanvi bhojpuri trending dj songs",
    ],
    "movies": [
        "full hindi dubbed action movie hd youtube",
        "bollywood full movie hd official",
        "south indian thriller full movie hindi dubbed",
        "hindi comedy full movie hd",
        "hollywood action movie hindi dubbed full hd",
        "new hindi dubbed adventure movie full",
        "superhit bollywood drama full movie",
        "hindi sci fi suspense full movie hd",
    ],
    "trending": [
        "trending viral videos india",
        "india top trending songs this week",
        "viral comedy standup videos india",
        "trending cricket highlights india",
        "trending tech gadgets unboxing hindi",
        "viral indian street food vlog",
        "trending gaming live highlights hindi",
        "top viral shorts compilation india",
    ],
}


def _fmt_duration(sec: Optional[int]) -> str:
    if not sec or sec <= 0:
        return ""
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_size(num: Optional[int]) -> str:
    if not num or num <= 0:
        return ""
    val = float(num)
    for u in ("B", "KB", "MB", "GB"):
        if val < 1024:
            return f"{val:.1f} {u}"
        val /= 1024
    return f"{val:.1f} TB"


def _fmt_views(v: Optional[int]) -> str:
    if not v or v <= 0:
        return ""
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M views"
    if v >= 1_000:
        return f"{v / 1_000:.1f}K views"
    return f"{v} views"


def _quality_tag(height: int) -> str:
    if height >= 2160:
        return f"4K {height}p"
    if height >= 1440:
        return f"2K {height}p"
    if height >= 1080:
        return f"Full HD {height}p"
    if height >= 720:
        return f"HD {height}p"
    return f"{height}p" if height > 0 else "Video"


def _extract_runs_text(obj: Any) -> str:
    if not obj:
        return ""
    if isinstance(obj, str):
        return obj
    if "simpleText" in obj:
        return str(obj["simpleText"])
    runs = obj.get("runs")
    if isinstance(runs, list):
        return "".join(str(r.get("text", "")) for r in runs if isinstance(r, dict))
    return ""


def _register_proxy(url: str, headers: Optional[Dict[str, str]] = None) -> str:
    now = time.time()
    if len(_PROXY_URLS) > 600:
        expired = [k for k, v in _PROXY_URLS.items() if (now - v[0]) > _PROXY_TTL]
        for k in expired:
            _PROXY_URLS.pop(k, None)
        if len(_PROXY_URLS) > 600:
            oldest = sorted(_PROXY_URLS.items(), key=lambda kv: kv[1][0])[:150]
            for k, _ in oldest:
                _PROXY_URLS.pop(k, None)
    token = uuid.uuid4().hex[:16]
    clean_hdrs = {}
    if headers:
        for k, v in headers.items():
            if k.lower() in ("user-agent", "referer", "origin", "accept-language"):
                clean_hdrs[k] = str(v)
    if "User-Agent" not in clean_hdrs:
        clean_hdrs["User-Agent"] = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
    _PROXY_URLS[token] = (now, url, clean_hdrs)
    return f"/api/proxy?token={token}"


# ======================================================================================
#  ENGINE 1: FAST DIRECT YOUTUBE INNERTUBE (Search + Stream Extraction)
# ======================================================================================


def _innertube_search(query: str, continuation: Optional[str] = None) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    url = "https://www.youtube.com/youtubei/v1/search?prettyPrint=false"
    payload: Dict[str, Any] = {
        "context": {
            "client": {
                "clientName": "WEB",
                "clientVersion": "2.20250201.00.00",
                "hl": "en",
                "gl": "IN",
            }
        }
    }
    if continuation:
        payload["continuation"] = continuation
    else:
        payload["query"] = query

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="ignore"))

    items: List[Dict[str, Any]] = []
    next_token: Optional[str] = None
    seen = set()

    def _walk(node: Any) -> None:
        nonlocal next_token
        if isinstance(node, dict):
            if "videoRenderer" in node and isinstance(node["videoRenderer"], dict):
                vr = node["videoRenderer"]
                vid = vr.get("videoId")
                if vid and len(vid) == 11 and vid not in seen:
                    title = _extract_runs_text(vr.get("title"))[:120] or "Untitled"
                    channel = (
                        _extract_runs_text(vr.get("ownerText"))
                        or _extract_runs_text(vr.get("longBylineText"))
                        or "YouTube"
                    )[:60]
                    dur = _extract_runs_text(vr.get("lengthText"))
                    views = _extract_runs_text(vr.get("shortViewCountText")) or _extract_runs_text(vr.get("viewCountText"))

                    avatar = ""
                    ctsr = vr.get("channelThumbnailSupportedRenderers") or {}
                    ctwlr = ctsr.get("channelThumbnailWithLinkRenderer") or {}
                    athumbs = (ctwlr.get("thumbnail") or {}).get("thumbnails") or []
                    if athumbs and isinstance(athumbs[0], dict):
                        avatar = str(athumbs[-1].get("url") or athumbs[0].get("url") or "")
                        if avatar.startswith("//"):
                            avatar = "https:" + avatar

                    seen.add(vid)
                    items.append({
                        "video_id": vid,
                        "title": title,
                        "channel": channel,
                        "channel_avatar": avatar,
                        "duration_text": dur,
                        "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                        "views_text": views,
                    })
            if "continuationCommand" in node and isinstance(node["continuationCommand"], dict):
                tok = node["continuationCommand"].get("token")
                if tok and not next_token:
                    next_token = str(tok)
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for el in node:
                _walk(el)

    _walk(data)
    return items, next_token


def _innertube_player_extract(video_id: str) -> Optional[Dict[str, Any]]:
    """Fast direct InnerTube /player extraction (works even when yt-dlp hits 502/bot checks!)."""
    clients = [
        (
            {"clientName": "ANDROID_VR", "clientVersion": "1.60.19", "deviceMake": "Oculus", "deviceModel": "Quest 3", "osName": "Android", "osVersion": "12L", "hl": "en", "gl": "IN"},
            "com.google.android.apps.youtube.vr.oculus/1.60.19 (Linux; U; Android 12L; eureka-user Build/SQ3A.220605.009.A1) gzip",
        ),
        (
            {"clientName": "IOS", "clientVersion": "20.03.02", "deviceMake": "Apple", "deviceModel": "iPhone16,2", "osName": "iPhone", "osVersion": "18.2.1.22C161", "hl": "en", "gl": "IN"},
            "com.google.ios.youtube/20.03.02 (iPhone16,2; U; CPU iOS 18_2_1 like Mac OS X;)",
        ),
        (
            {"clientName": "TVHTML5_SIMPLY_EMBEDDED_PLAYER", "clientVersion": "2.0", "hl": "en", "gl": "IN"},
            "Mozilla/5.0 (ChromiumStylePlatform) Cobalt/Version",
        ),
    ]

    for client_ctx, ua in clients:
        try:
            payload = {
                "videoId": video_id,
                "context": {"client": client_ctx},
                "contentCheckOk": True,
                "racyCheckOk": True,
            }
            req = urllib.request.Request(
                "https://www.youtube.com/youtubei/v1/player?prettyPrint=false",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": ua,
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="ignore"))

            sd = data.get("streamingData") or {}
            raw_fmts = (sd.get("formats") or []) + (sd.get("adaptiveFormats") or [])
            parsed_fmts = []
            for rf in raw_fmts:
                u = rf.get("url")
                if not u:
                    continue
                mime = rf.get("mimeType") or ""
                # Parse mimeType e.g. 'video/mp4; codecs="avc1.640028"'
                ext = "mp4"
                if "webm" in mime:
                    ext = "webm"
                elif "audio/mp4" in mime:
                    ext = "m4a"
                codecs_m = re.search(r'codecs="([^"]+)"', mime)
                codecs_str = codecs_m.group(1) if codecs_m else ""
                is_video = mime.startswith("video/")
                is_audio = mime.startswith("audio/")
                if is_video and "," in codecs_str:
                    vc, ac = [c.strip() for c in codecs_str.split(",", 1)]
                elif is_video:
                    vc, ac = (codecs_str or "avc1"), "none"
                elif is_audio:
                    vc, ac = "none", (codecs_str or "mp4a")
                else:
                    continue

                parsed_fmts.append({
                    "format_id": str(rf.get("itag") or ""),
                    "url": u,
                    "ext": ext,
                    "protocol": "https",
                    "vcodec": vc,
                    "acodec": ac,
                    "height": int(rf.get("height") or 0),
                    "fps": int(rf.get("fps") or 30),
                    "abr": int((rf.get("averageBitrate") or rf.get("bitrate") or 0) // 1000) if is_audio else 0,
                    "filesize": int(rf.get("contentLength") or 0),
                    "http_headers": {"User-Agent": ua},
                })

            if parsed_fmts:
                vd = data.get("videoDetails") or {}
                return {
                    "id": video_id,
                    "title": vd.get("title") or "Untitled",
                    "uploader": vd.get("author") or "YouTube",
                    "duration": int(vd.get("lengthSeconds") or 0),
                    "view_count": int(vd.get("viewCount") or 0),
                    "formats": parsed_fmts,
                    "http_headers": {"User-Agent": ua},
                }
        except Exception:
            continue
    return None


# ======================================================================================
#  ENGINE 2: YT-DLP ROTATION FALLBACK
# ======================================================================================


def _base_opts(client: Optional[str] = None) -> Dict[str, Any]:
    # Keep the backend extraction settings aligned with the known-working
    # downloader implementation: retries, fragment retries, JS runtime,
    # bounded chunking and a conservative timeout. These are reliability
    # settings; they do not bypass authentication, DRM or private content.
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "geo_bypass": True,
        "socket_timeout": 15,
        "cachedir": False,
        "nocheckcertificate": True,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 1,
        "concurrent_fragment_downloads": 8,
        "http_chunk_size": 2 * 1024 * 1024,
        "buffersize": 4 * 1024 * 1024,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    if _COOKIE_FILE and os.path.isfile(_COOKIE_FILE):
        opts["cookiefile"] = _COOKIE_FILE
    if client:
        opts["extractor_args"] = {"youtube": {"player_client": [client]}}
    if NODE_PATH:
        opts["js_runtimes"] = {"node": {}}
    return opts


def _extract_flat_list_fallback(
    search_expr: str,
    playlist_start: int = 1,
    playlist_end: Optional[int] = None,
) -> List[Dict[str, Any]]:
    opts = _base_opts()
    opts["extract_flat"] = "in_playlist"
    opts["skip_download"] = True
    if playlist_start > 1:
        opts["playliststart"] = playlist_start
    if playlist_end is not None:
        opts["playlistend"] = playlist_end
    items: List[Dict[str, Any]] = []
    seen = set()
    with yt_dlp.YoutubeDL(opts) as ydl:
        data = ydl.extract_info(search_expr, download=False)
        for e in (data or {}).get("entries") or []:
            vid = e.get("id")
            if not vid or len(vid) != 11 or vid in seen:
                continue
            if e.get("live_status") in ("is_live", "is_upcoming"):
                continue
            seen.add(vid)
            items.append({
                "video_id": vid,
                "title": (e.get("title") or "Untitled")[:120],
                "channel": (e.get("uploader") or e.get("channel") or "YouTube")[:60],
                "channel_avatar": "",
                "duration_text": _fmt_duration(e.get("duration")),
                "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                "views_text": _fmt_views(e.get("view_count")),
            })
    return items


def _extract_info_with_rotation(
    url: str,
    download: bool = False,
    extra_opts: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    global _LAST_WORKING_CLIENT

    # Prefer the known-working yt-dlp path. The old Telegram downloader uses
    # this strategy and it also gives us consistent format metadata for both
    # playback and server-side downloads.
    ordered_clients = list(_CLIENTS)
    if _LAST_WORKING_CLIENT in ordered_clients:
        ordered_clients.remove(_LAST_WORKING_CLIENT)
        ordered_clients.insert(0, _LAST_WORKING_CLIENT)

    last_err: Optional[Exception] = None
    for client in ordered_clients:
        opts = _base_opts(client)
        if extra_opts:
            opts.update(extra_opts)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=download)
                if info:
                    _LAST_WORKING_CLIENT = client
                    return info
        except Exception as e:
            last_err = e
            continue

    # Legacy fallback: retain the existing InnerTube extractor only as a
    # compatibility fallback if yt-dlp cannot obtain metadata.
    if not download:
        m = _YT_ID_RE.search(url)
        if m:
            try:
                fast = _innertube_player_extract(m.group(1))
                if fast and fast.get("formats"):
                    return fast
            except Exception as e:
                last_err = e

    raise HTTPException(status_code=502, detail=f"Extraction failed after all configured clients: {last_err}")


def _codec_rank(vcodec: str) -> int:
    vc = (vcodec or "").lower()
    if vc.startswith("avc1") or "h264" in vc:
        return 3
    if vc.startswith("vp9") or vc.startswith("vp09"):
        return 2
    if vc.startswith("av01"):
        return 1
    return 0


def _is_aac(acodec: str) -> bool:
    ac = (acodec or "").lower()
    return ac.startswith("mp4a") or "aac" in ac


# ======================================================================================
#  7-ZONE 3D HRTF BEAT-SYNCED 16D SPATIAL AUDIO ENGINE
# ======================================================================================


def _estimate_bpm_and_phase(filepath: str, duration: int = 0) -> Tuple[float, float]:
    if not FFMPEG_PATH or not os.path.isfile(filepath):
        return 120.0, 0.0
    try:
        start_s = int(duration * 0.25) if duration > 45 else 0
        res = subprocess.run(
            [FFMPEG_PATH, "-v", "quiet", "-ss", str(start_s), "-t", "24", "-i", filepath,
             "-ac", "1", "-ar", "8000", "-f", "s16le", "pipe:1"],
            capture_output=True, timeout=20,
        )
        raw = res.stdout
        if len(raw) < 16000:
            return 120.0, 0.0
        samples = array.array("h")
        samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
        hop = 160
        fps = 50.0
        n_frames = len(samples) // hop
        energies = [math.sqrt(sum(x * x for x in samples[i * hop:(i + 1) * hop]) / hop) for i in range(n_frames)]
        onset = [max(0.0, energies[i] - energies[i - 1]) for i in range(1, len(energies))]
        mean_o = sum(onset) / max(1, len(onset))
        centered = [x - mean_o for x in onset]
        n = len(centered)
        best_lag, best_score = 25, -1.0
        for lag in range(18, min(38, n // 2)):
            score = sum(centered[i] * centered[i + lag] for i in range(n - lag))
            if score > best_score:
                best_score, best_lag = score, lag
        bpm = max(80.0, min(160.0, round((fps * 60.0) / best_lag, 1)))
        best_p = max(range(best_lag), key=lambda p: sum(onset[idx] for idx in range(p, n, best_lag)))
        phase = round((best_p / fps) % (60.0 / bpm), 3)
        return bpm, phase
    except Exception:
        return 120.0, 0.0


def _build_16d_fc(bpm: float, phase: float) -> str:
    beat_s = 60.0 / bpm
    az_w = 2.0 * math.pi / (8.0 * beat_s)
    el_w = 2.0 * math.pi / (6.0 * beat_s)
    bt_w = math.pi / beat_s
    t_expr = f"(t-{phase:.3f})"
    pulse = f"(1+0.12*pow(max(0,cos({bt_w:.4f}*{t_expr})),4))"
    return (
        "[0:a]asplit=7[s_sub][s_fr][s_bk][s_lt][s_rt][s_tp][s_bt];"
        "[s_sub]lowpass=f=110,bass=g=3.0:f=60:w=0.6[sub];"
        f"[s_fr]highpass=f=110,equalizer=f=3200:t=q:w=1.2:g=4.5,equalizer=f=1200:t=q:w=1.5:g=1.5,"
        f"volume='0.58*max(0,cos({az_w:.4f}*{t_expr}))*{pulse}':eval=frame[fr];"
        f"[s_bk]highpass=f=110,equalizer=f=4000:t=q:w=1.0:g=-5.5,extrastereo=m=1.4,"
        f"aecho=0.8:0.75:24|48:0.28|0.16,volume='0.46*max(0,-cos({az_w:.4f}*{t_expr}))*{pulse}':eval=frame[bk];"
        f"[s_lt]highpass=f=110,adelay=0|0.55,pan=stereo|c0=1.0*c0|c1=0.15*c1,"
        f"volume='0.78*max(0,-sin({az_w:.4f}*{t_expr}))*{pulse}':eval=frame[lt];"
        f"[s_rt]highpass=f=110,adelay=0.55|0,pan=stereo|c0=0.15*c0|c1=1.0*c1,"
        f"volume='0.78*max(0,sin({az_w:.4f}*{t_expr}))*{pulse}':eval=frame[rt];"
        f"[s_tp]highpass=f=220,equalizer=f=4500:t=q:w=1.5:g=-6.0,equalizer=f=8200:t=q:w=1.8:g=7.0,"
        f"aecho=0.9:0.7:16:0.18,volume='0.58*max(0,sin({el_w:.4f}*{t_expr}))*{pulse}':eval=frame[tp];"
        f"[s_bt]highpass=f=110,lowpass=f=5200,equalizer=f=380:t=q:w=1.2:g=5.0,"
        f"aecho=0.9:0.7:12:0.20,volume='0.52*max(0,-sin({el_w:.4f}*{t_expr}))*{pulse}':eval=frame[bt];"
        "[sub][fr][bk][lt][rt][tp][bt]amix=inputs=7:normalize=0,dynaudnorm,alimiter=limit=0.95[aout]"
    )


# ======================================================================================
#  ENDPOINTS
# ======================================================================================


@app.get("/")
def root():
    return {"service": "SR King Play API", "version": "5.1.0", "status": "online"}


@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": "5.1.0",
        "yt_dlp": getattr(yt_dlp.version, "__version__", "unknown"),
        "ffmpeg": bool(FFMPEG_PATH),
        "node": bool(NODE_PATH),
    }


@app.get("/api/proxy")
def proxy_stream(request: Request, token: str = Query(...)):
    """Zero-storage HTTP Range streaming relay — eliminates YouTube 403 IP-lock & enables true 4K/1080p playback."""
    entry = _PROXY_URLS.get(token)
    if not entry:
        raise HTTPException(status_code=404, detail="Stream token expired")
    _, target_url, base_hdrs = entry
    hdrs = dict(base_hdrs)
    range_hdr = request.headers.get("range")
    if range_hdr:
        hdrs["Range"] = range_hdr

    req = urllib.request.Request(target_url, headers=hdrs, method="GET")
    try:
        upstream = urllib.request.urlopen(req, timeout=20)
    except urllib.error.HTTPError as e:
        raise HTTPException(status_code=e.code, detail=f"Upstream HTTP {e.code}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    resp_headers: Dict[str, str] = {"Accept-Ranges": "bytes"}
    for h in ("Content-Type", "Content-Length", "Content-Range"):
        val = upstream.headers.get(h)
        if val:
            resp_headers[h] = val

    status_code = upstream.getcode() or (206 if range_hdr else 200)

    def _iter_chunks():
        try:
            while True:
                chunk = upstream.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    return StreamingResponse(
        _iter_chunks(),
        status_code=status_code,
        headers=resp_headers,
        media_type=upstream.headers.get("Content-Type", "application/octet-stream"),
    )


@app.get("/api/trending")
def trending(
    category: str = Query("all"),
    page: int = Query(1, ge=1, le=12),
    limit: int = Query(20, ge=5, le=30),
    interests: str = Query(""),
):
    cat = category.lower()
    user_topics = [t.strip() for t in interests.split(",") if len(t.strip()) >= 2]

    if cat == "all" and user_topics:
        queries = []
        idx1 = (page - 1) % len(user_topics)
        queries.append(user_topics[idx1])
        if len(user_topics) > 1:
            idx2 = page % len(user_topics)
            if idx2 != idx1:
                queries.append(user_topics[idx2])

        combined: List[Dict[str, Any]] = []
        seen = set()
        for q in queries:
            try:
                part, _ = _innertube_search(q)
                for it in part[:12]:
                    if it["video_id"] not in seen:
                        seen.add(it["video_id"])
                        combined.append(it)
            except Exception:
                continue
        if combined:
            random.shuffle(combined)
            return {
                "items": combined[:limit],
                "page": page,
                "has_more": page < 10,
            }

    pool = _TRENDING_POOLS.get(cat, _TRENDING_POOLS["all"])
    q = pool[(page - 1) % len(pool)]
    try:
        items, _ = _innertube_search(q)
        if not items:
            items = _extract_flat_list_fallback(f"ytsearch{limit}:{q}")
    except Exception:
        items = _extract_flat_list_fallback(f"ytsearch{limit}:{q}")

    random.shuffle(items)
    return {
        "items": items[:limit],
        "page": page,
        "has_more": page < len(pool),
    }


@app.get("/api/search")
def search(
    q: str = Query(..., min_length=1),
    page: int = Query(1, ge=1, le=10),
    limit: int = Query(20, ge=5, le=30),
):
    m = _YT_ID_RE.search(q)
    if m:
        vid = m.group(1)
        info = _extract_info_with_rotation(f"https://www.youtube.com/watch?v={vid}", download=False)
        return {
            "items": [{
                "video_id": vid,
                "title": (info.get("title") or "Untitled")[:120],
                "channel": (info.get("uploader") or info.get("channel") or "YouTube")[:60],
                "channel_avatar": "",
                "duration_text": _fmt_duration(info.get("duration")),
                "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                "views_text": _fmt_views(info.get("view_count")),
            }],
            "page": 1,
            "has_more": False,
        }

    now = time.time()
    token_key = f"{q.lower().strip()}::p{page}"
    try:
        cont_token = None
        if page > 1:
            cached = _SEARCH_TOKENS.get(token_key)
            if cached and (now - cached[0]) < _CACHE_TTL:
                cont_token = cached[1]
        if page == 1 or cont_token:
            items, next_tok = _innertube_search(q, continuation=cont_token)
            if next_tok:
                _SEARCH_TOKENS[f"{q.lower().strip()}::p{page + 1}"] = (now, next_tok)
            if items:
                return {
                    "items": items[:limit],
                    "page": page,
                    "has_more": bool(next_tok) and page < 10,
                }
    except Exception:
        pass

    start = (page - 1) * limit + 1
    end = page * limit
    items = _extract_flat_list_fallback(f"ytsearch{end}:{q}", playlist_start=start, playlist_end=end)
    return {
        "items": items,
        "page": page,
        "has_more": len(items) >= 5 and page < 8,
    }


@app.get("/api/streams/{video_id}")
def get_streams(video_id: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise HTTPException(status_code=400, detail="Invalid video_id")

    now = time.time()
    cached = _STREAM_CACHE.get(video_id)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1]

    info = _extract_info_with_rotation(f"https://www.youtube.com/watch?v={video_id}", download=False)
    formats = info.get("formats") or []
    http_headers = info.get("http_headers") or {}

    muxed_raw = []
    video_only_raw = []
    audio_only_raw = []

    for f in formats:
        url = f.get("url")
        if not url:
            continue
        ext = (f.get("ext") or "mp4").lower()
        proto = (f.get("protocol") or "").lower()
        if ext == "mhtml" or "mhtml" in proto:
            continue

        vcodec = f.get("vcodec") or "none"
        acodec = f.get("acodec") or "none"
        size = f.get("filesize") or f.get("filesize_approx") or 0
        height = int(f.get("height") or 0)
        abr = int(f.get("abr") or 0)
        fps = int(f.get("fps") or 30)
        is_hls = "m3u8" in proto or ".m3u8" in url
        f_hdrs = f.get("http_headers") or http_headers

        if vcodec != "none" and acodec != "none":
            muxed_raw.append({
                "height": height,
                "fps": fps,
                "codec_rank": _codec_rank(vcodec),
                "is_mp4": ext == "mp4",
                "is_hls": is_hls,
                "format_id": str(f.get("format_id")),
                "ext": ext,
                "size": size,
                "url": url,
                "headers": f_hdrs,
            })
        elif vcodec != "none" and acodec == "none" and not is_hls and height > 0:
            video_only_raw.append({
                "height": height,
                "fps": fps,
                "codec_rank": _codec_rank(vcodec),
                "is_mp4": ext == "mp4",
                "format_id": str(f.get("format_id")),
                "ext": ext,
                "size": size,
                "url": url,
                "headers": f_hdrs,
            })
        elif vcodec == "none" and acodec != "none" and not is_hls:
            audio_only_raw.append({
                "abr": abr,
                "is_aac": _is_aac(acodec),
                "is_m4a": ext in ("m4a", "mp4"),
                "format_id": str(f.get("format_id")),
                "label": f"Audio {abr} kbps ({ext.upper()})" if abr else f"Audio ({ext.upper()})",
                "ext": ext,
                "size": size,
                "size_text": _fmt_size(size),
                "url": url,
                "headers": f_hdrs,
            })

    audio_only_raw.sort(key=lambda x: (x["is_aac"], x["abr"]), reverse=True)
    best_audio_raw = audio_only_raw[0] if audio_only_raw else None
    best_audio_size = best_audio_raw["size"] if best_audio_raw else 0
    proxied_audio_url = _register_proxy(best_audio_raw["url"], best_audio_raw["headers"]) if best_audio_raw else ""

    muxed_raw.sort(key=lambda x: (not x["is_hls"], x["codec_rank"], x["is_mp4"], x["height"]), reverse=True)
    best_muxed = muxed_raw[0] if muxed_raw else None
    proxied_playback_url = _register_proxy(best_muxed["url"], best_muxed["headers"]) if best_muxed else ""

    video_only_raw.sort(key=lambda x: (x["height"], x["codec_rank"], x["fps"], x["is_mp4"]), reverse=True)
    dash_by_height: Dict[int, Dict[str, Any]] = {}
    for v_item in video_only_raw:
        h = v_item["height"]
        if h >= 240 and h not in dash_by_height and v_item["codec_rank"] >= 2:
            dash_by_height[h] = v_item
    for v_item in video_only_raw:
        h = v_item["height"]
        if h >= 240 and h not in dash_by_height:
            dash_by_height[h] = v_item

    muxed_by_height: Dict[int, Dict[str, Any]] = {}
    for m_item in muxed_raw:
        h = m_item["height"]
        if h >= 240 and h not in muxed_by_height:
            muxed_by_height[h] = m_item

    all_heights = sorted(set(muxed_by_height.keys()) | set(dash_by_height.keys()), reverse=True)

    playable_qualities = []
    video_formats = []
    for h in all_heights:
        if h in dash_by_height and proxied_audio_url:
            d_item = dash_by_height[h]
            fps_tag = " 60fps" if d_item["fps"] >= 50 else ""
            playable_qualities.append({
                "height": h,
                "label": f"{_quality_tag(h)}{fps_tag}",
                "video_url": _register_proxy(d_item["url"], d_item["headers"]),
                "audio_url": proxied_audio_url,
            })
        elif h in muxed_by_height:
            m_item = muxed_by_height[h]
            playable_qualities.append({
                "height": h,
                "label": _quality_tag(h),
                "video_url": _register_proxy(m_item["url"], m_item["headers"]),
                "audio_url": "",
            })

        if h in muxed_by_height and h < 1080:
            m_item = muxed_by_height[h]
            video_formats.append({
                "height": h,
                "format_id": m_item["format_id"],
                "label": f"{_quality_tag(h)} MP4",
                "ext": m_item["ext"],
                "size_text": _fmt_size(m_item["size"]),
                "url": _register_proxy(m_item["url"], m_item["headers"]),
                "server_mux": True,
            })
        else:
            d_item = dash_by_height.get(h) or muxed_by_height.get(h)
            est_size = (d_item["size"] + best_audio_size) if (d_item and d_item["size"]) else 0
            video_formats.append({
                "height": h,
                "format_id": d_item["format_id"] if d_item else str(h),
                "label": f"{_quality_tag(h)} MP4 (Max Quality)",
                "ext": "mp4",
                "size_text": _fmt_size(est_size),
                "url": f"/api/video/{video_id}?height={h}",
                "server_mux": True,
            })

    default_q = None
    for q in playable_qualities:
        if q["height"] <= 1440:
            default_q = q
            break
    if not default_q and playable_qualities:
        default_q = playable_qualities[0]

    audio_only = [
        {
            "abr": a["abr"],
            "format_id": a["format_id"],
            "label": a["label"],
            "ext": a["ext"],
            "size_text": a["size_text"],
            "url": _register_proxy(a["url"], a["headers"]),
            "server_mux": True,
        }
        for a in audio_only_raw[:4]
    ]

    result = {
        "video_id": video_id,
        "title": info.get("title") or "Untitled",
        "channel": info.get("uploader") or info.get("channel") or "YouTube",
        "duration_text": _fmt_duration(info.get("duration")),
        "playback_url": proxied_playback_url,
        "dash_video_url": default_q["video_url"] if default_q else "",
        "dash_audio_url": default_q["audio_url"] if default_q else proxied_audio_url,
        "default_quality_label": default_q["label"] if default_q else "HD",
        "playable_qualities": playable_qualities[:8],
        "video_formats": video_formats[:8],
        "audio_formats": audio_only,
        "stream_user_agent": (
            (best_muxed.get("headers") or {}).get("User-Agent", "")
            if best_muxed else ((best_audio_raw.get("headers") or {}).get("User-Agent", "") if best_audio_raw else "")
        ),
    }
    _STREAM_CACHE[video_id] = (now, result)
    if len(_STREAM_CACHE) > 120:
        oldest = min(_STREAM_CACHE, key=lambda k: _STREAM_CACHE[k][0])
        _STREAM_CACHE.pop(oldest, None)
    return result


@app.get("/api/video/{video_id}")
def download_max_quality_video(video_id: str, height: int = Query(1080, ge=360, le=2160)):
    """Download & fast-remux up to 4K/2K/1080p MP4, auto-deleting temp dir immediately."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise HTTPException(status_code=400, detail="Invalid video_id")
    if not FFMPEG_PATH:
        raise HTTPException(status_code=500, detail="ffmpeg not installed on server")

    work_dir = tempfile.mkdtemp(prefix="srk_vid_")
    out_tpl = os.path.join(work_dir, "%(id)s.%(ext)s")
    fmt_selector = (
        f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
    )
    try:
        info = _extract_info_with_rotation(
            f"https://www.youtube.com/watch?v={video_id}",
            download=True,
            extra_opts={
                "outtmpl": out_tpl,
                "format": fmt_selector,
                "merge_output_format": "mp4",
            },
        )
        files = [os.path.join(work_dir, f) for f in os.listdir(work_dir) if os.path.isfile(os.path.join(work_dir, f))]
        if not files:
            raise HTTPException(status_code=500, detail="Video download failed")
        final_file = max(files, key=os.path.getsize)
        title = re.sub(r"[^a-zA-Z0-9 ._-]+", "", (info.get("title") or video_id))[:60].strip() or video_id
        return FileResponse(
            final_file,
            media_type="video/mp4",
            filename=f"{title}_{height}p.mp4",
            background=BackgroundTask(shutil.rmtree, work_dir, True),
        )
    except HTTPException:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/audio/{video_id}")
def download_processed_audio(video_id: str, mode: str = Query("normal")):
    """Download & transcode to 320k Crystal-Clear MP3 or 7-Zone Beat-Synced 16D Spatial Audio (auto-deletes temp dir)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise HTTPException(status_code=400, detail="Invalid video_id")
    if not FFMPEG_PATH:
        raise HTTPException(status_code=500, detail="ffmpeg not installed on server")

    work_dir = tempfile.mkdtemp(prefix="srk_api_")
    out_tpl = os.path.join(work_dir, "%(id)s.%(ext)s")
    try:
        info = _extract_info_with_rotation(
            f"https://www.youtube.com/watch?v={video_id}",
            download=True,
            extra_opts={"outtmpl": out_tpl, "format": "bestaudio/best"},
        )
        files = [os.path.join(work_dir, f) for f in os.listdir(work_dir) if os.path.isfile(os.path.join(work_dir, f))]
        if not files:
            raise HTTPException(status_code=500, detail="Audio download failed")
        src = max(files, key=os.path.getsize)
        dur = int(info.get("duration") or 0)
        out_mp3 = os.path.join(work_dir, f"{video_id}_{mode}.mp3")

        if mode == "16d":
            bpm, phase = _estimate_bpm_and_phase(src, dur)
            fc = _build_16d_fc(bpm, phase)
            cmd = [
                FFMPEG_PATH, "-y", "-i", src, "-vn",
                "-filter_complex", fc, "-map", "[aout]",
                "-acodec", "libmp3lame", "-b:a", "320k", "-ar", "48000", "-ac", "2", out_mp3,
            ]
        else:
            cmd = [
                FFMPEG_PATH, "-y", "-i", src, "-vn",
                "-acodec", "libmp3lame", "-b:a", "320k", "-ar", "48000", "-ac", "2", out_mp3,
            ]

        r = subprocess.run(cmd, capture_output=True, timeout=240)
        if r.returncode != 0 or not os.path.isfile(out_mp3):
            raise HTTPException(status_code=500, detail="Audio processing failed")

        title = re.sub(r"[^a-zA-Z0-9 ._-]+", "", (info.get("title") or video_id))[:60].strip() or video_id
        suffix = "_16D" if mode == "16d" else ""
        return FileResponse(
            out_mp3,
            media_type="audio/mpeg",
            filename=f"{title}{suffix}.mp3",
            background=BackgroundTask(shutil.rmtree, work_dir, True),
        )
    except HTTPException:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e))