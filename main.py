#!/usr/bin/env python3
"""SR King Play — Railway FastAPI Backend v4.0 (Exact YouTube Search + Channel Logos, Instant Cache, 4K/1080p Muxing & 16D Audio)."""

import array
import json
import math
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
import yt_dlp

app = FastAPI(title="SR King Play API", version="4.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FFMPEG_PATH = shutil.which("ffmpeg")
NODE_PATH = shutil.which("node")

_CLIENTS = ["android", "tv_embedded", "android_vr", "tv", "web_safari"]
_YT_ID_RE = re.compile(
    r"(?:v=|youtu\.be/|shorts/|live/|embed/)([A-Za-z0-9_-]{11})", re.IGNORECASE
)

# Fast in-memory caches (TTL 15 min) for instant playback & smooth infinite scroll
_STREAM_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_SEARCH_TOKENS: Dict[str, Tuple[float, str]] = {}
_CACHE_TTL = 900.0

_TRENDING_POOLS: Dict[str, List[str]] = {
    "all": [
        "trending hindi bollywood hit songs 2025",
        "latest punjabi hit songs official video",
        "full hindi dubbed action movie hd",
        "top viral music videos india 2025",
        "arijit singh best hit songs official",
        "south indian hindi dubbed blockbuster full movie",
        "bollywood party dj remix songs",
        "trending hindi romantic songs official video",
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
        "trending viral videos music india",
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


# ======================================================================================
#  FAST EXACT YOUTUBE INNERTUBE SEARCH (Returns Exact Results + Real Channel Logos!)
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

                    # Extract real channel avatar logo URL!
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


def _base_opts(client: Optional[str] = None) -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "geo_bypass": True,
        "socket_timeout": 12,
        "cachedir": False,
        "nocheckcertificate": True,
    }
    if client:
        opts["extractor_args"] = {"youtube": {"player_client": [client], "formats": ["missing_pot"]}}
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
    last_err = None
    for client in _CLIENTS:
        opts = _base_opts(client)
        if extra_opts:
            opts.update(extra_opts)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=download)
                if info:
                    return info
        except Exception as e:
            last_err = e
            continue
    raise HTTPException(status_code=502, detail=f"Extraction failed: {last_err}")


def _is_h264(vcodec: str) -> bool:
    vc = (vcodec or "").lower()
    return vc.startswith("avc1") or "h264" in vc


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
    return {"service": "SR King Play API", "version": "4.0.0", "status": "online"}


@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": "4.0.0",
        "yt_dlp": getattr(yt_dlp.version, "__version__", "unknown"),
        "ffmpeg": bool(FFMPEG_PATH),
        "node": bool(NODE_PATH),
    }


@app.get("/api/trending")
def trending(
    category: str = Query("all"),
    page: int = Query(1, ge=1, le=12),
    limit: int = Query(20, ge=5, le=30),
):
    pool = _TRENDING_POOLS.get(category.lower(), _TRENDING_POOLS["all"])
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
    # Direct YouTube URL support
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

    # Exact YouTube InnerTube Search (Fast ~250ms, real channel logos, exact matching!)
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

    # Fallback to yt-dlp search if InnerTube fails
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

    # Instant return from 15-min stream cache!
    now = time.time()
    cached = _STREAM_CACHE.get(video_id)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1]

    info = _extract_info_with_rotation(f"https://www.youtube.com/watch?v={video_id}", download=False)
    formats = info.get("formats") or []

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
        is_hls = "m3u8" in proto or ".m3u8" in url

        if vcodec != "none" and acodec != "none":
            muxed_raw.append({
                "height": height,
                "is_h264": _is_h264(vcodec),
                "is_mp4": ext == "mp4",
                "is_hls": is_hls,
                "format_id": str(f.get("format_id")),
                "ext": ext,
                "size": size,
                "url": url,
            })
        elif vcodec != "none" and acodec == "none" and not is_hls and height > 0:
            video_only_raw.append({
                "height": height,
                "is_h264": _is_h264(vcodec),
                "is_mp4": ext == "mp4",
                "format_id": str(f.get("format_id")),
                "ext": ext,
                "size": size,
                "url": url,
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
            })

    muxed_raw.sort(key=lambda x: (not x["is_hls"], x["is_h264"], x["is_mp4"], x["height"]), reverse=True)
    audio_only_raw.sort(key=lambda x: (x["is_aac"], x["is_m4a"], x["abr"]), reverse=True)
    best_audio_size = audio_only_raw[0]["size"] if audio_only_raw else 0

    # Build unified quality list from MAX (4K/2K/1080p) down to 360p!
    # Muxed heights use direct YouTube URL; higher DASH heights (1080p/1440p/2160p) use server fast remux (/api/video/{id}?height=...)
    muxed_by_height: Dict[int, Dict[str, Any]] = {}
    for m_item in muxed_raw:
        h = m_item["height"]
        if h > 0 and h not in muxed_by_height:
            muxed_by_height[h] = m_item

    video_only_raw.sort(key=lambda x: (x["height"], x["is_h264"], x["is_mp4"]), reverse=True)
    dash_by_height: Dict[int, Dict[str, Any]] = {}
    for v_item in video_only_raw:
        h = v_item["height"]
        if h not in dash_by_height:
            dash_by_height[h] = v_item

    all_heights = sorted(set(muxed_by_height.keys()) | set(dash_by_height.keys()), reverse=True)
    video_formats = []
    for h in all_heights:
        if h < 240:
            continue
        if h in muxed_by_height:
            m_item = muxed_by_height[h]
            video_formats.append({
                "height": h,
                "format_id": m_item["format_id"],
                "label": f"{_quality_tag(h)} MP4",
                "ext": m_item["ext"],
                "size_text": _fmt_size(m_item["size"]),
                "url": m_item["url"],
                "server_mux": False,
            })
        else:
            d_item = dash_by_height[h]
            est_size = (d_item["size"] + best_audio_size) if d_item["size"] else 0
            video_formats.append({
                "height": h,
                "format_id": d_item["format_id"],
                "label": f"{_quality_tag(h)} MP4 (Max Quality)",
                "ext": "mp4",
                "size_text": _fmt_size(est_size),
                "url": f"/api/video/{video_id}?height={h}",
                "server_mux": True,
            })

    audio_only = [
        {
            "abr": a["abr"],
            "format_id": a["format_id"],
            "label": a["label"],
            "ext": a["ext"],
            "size_text": a["size_text"],
            "url": a["url"],
        }
        for a in audio_only_raw[:4]
    ]

    # Pick best H.264 MP4 DASH video (<=1080p) + AAC audio for guaranteed black-screen-free HD playback
    hd_h264 = [v for v in video_only_raw if v["is_h264"] and v["is_mp4"] and 0 < v["height"] <= 1080]
    hd_h264.sort(key=lambda x: x["height"], reverse=True)
    best_dash_video = hd_h264[0]["url"] if hd_h264 else ""
    best_dash_audio = audio_only_raw[0]["url"] if audio_only_raw else ""
    playback_url = muxed_raw[0]["url"] if muxed_raw else ""

    result = {
        "video_id": video_id,
        "title": info.get("title") or "Untitled",
        "channel": info.get("uploader") or info.get("channel") or "YouTube",
        "duration_text": _fmt_duration(info.get("duration")),
        "playback_url": playback_url,
        "dash_video_url": best_dash_video,
        "dash_audio_url": best_dash_audio,
        "video_formats": video_formats[:8],
        "audio_formats": audio_only,
    }
    _STREAM_CACHE[video_id] = (now, result)
    # Keep cache bounded to 120 entries
    if len(_STREAM_CACHE) > 120:
        oldest = min(_STREAM_CACHE, key=lambda k: _STREAM_CACHE[k][0])
        _STREAM_CACHE.pop(oldest, None)
    return result


@app.get("/api/video/{video_id}")
def download_max_quality_video(video_id: str, height: int = Query(1080, ge=360, le=2160)):
    """Download & fast-remux (stream copy, no re-encode) up to 4K/1080p MP4, auto-deleting temp dir."""
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
    """Download & transcode to 192k MP3 or 7-Zone Beat-Synced 16D Spatial Audio (auto-deletes temp dir)."""
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
                "-acodec", "libmp3lame", "-b:a", "192k", "-ar", "44100", "-ac", "2", out_mp3,
            ]
        else:
            cmd = [
                FFMPEG_PATH, "-y", "-i", src, "-vn",
                "-acodec", "libmp3lame", "-b:a", "192k", "-ar", "44100", "-ac", "2", out_mp3,
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
