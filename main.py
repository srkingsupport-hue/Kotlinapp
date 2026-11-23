#!/usr/bin/env python3
"""SR King Play — Railway FastAPI Backend (Ad-Free Search, HD Streams, Multi-GB Movies & 16D Spatial Audio)."""

import array
import math
import os
import random
import re
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
import yt_dlp

app = FastAPI(title="SR King Play API", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FFMPEG_PATH = shutil.which("ffmpeg")
NODE_PATH = shutil.which("node")

_CLIENTS = ["tv_embedded", "tv", "android_vr", "android", "web_safari"]
_YT_ID_RE = re.compile(
    r"(?:v=|youtu\.be/|shorts/|live/|embed/)([A-Za-z0-9_-]{11})", re.IGNORECASE
)

_TRENDING_QUERIES = {
    "all": "trending hindi bollywood hit songs and full movies 2025",
    "music": "trending bollywood punjabi hit songs official video",
    "movies": "full hindi dubbed action movie hd youtube",
    "trending": "trending viral videos music india",
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


def _base_opts(client: Optional[str] = None) -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "geo_bypass": True,
        "socket_timeout": 15,
        "cachedir": False,
    }
    if client:
        opts["extractor_args"] = {"youtube": {"player_client": [client], "formats": ["missing_pot"]}}
    if NODE_PATH:
        opts["js_runtimes"] = {"node": {}}
    return opts


def _extract_flat_list(search_expr: str) -> List[Dict[str, Any]]:
    opts = _base_opts()
    opts["extract_flat"] = "in_playlist"
    opts["skip_download"] = True
    items: List[Dict[str, Any]] = []
    with yt_dlp.YoutubeDL(opts) as ydl:
        data = ydl.extract_info(search_expr, download=False)
        for e in (data or {}).get("entries") or []:
            vid = e.get("id")
            if not vid or len(vid) != 11:
                continue
            if e.get("live_status") in ("is_live", "is_upcoming"):
                continue
            items.append({
                "video_id": vid,
                "title": (e.get("title") or "Untitled")[:120],
                "channel": (e.get("uploader") or e.get("channel") or "YouTube")[:60],
                "duration_text": _fmt_duration(e.get("duration")),
                "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                "views_text": _fmt_views(e.get("view_count")),
            })
    return items


def _extract_info_with_rotation(url: str, download: bool = False, extra_opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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


@app.get("/health")
def health():
    return {
        "status": "ok",
        "yt_dlp": getattr(yt_dlp.version, "__version__", "unknown"),
        "ffmpeg": bool(FFMPEG_PATH),
        "node": bool(NODE_PATH),
    }


@app.get("/api/trending")
def trending(category: str = Query("all")):
    q = _TRENDING_QUERIES.get(category.lower(), _TRENDING_QUERIES["all"])
    items = _extract_flat_list(f"ytsearch16:{q}")
    random.shuffle(items)
    return {"items": items}


@app.get("/api/search")
def search(q: str = Query(..., min_length=1), limit: int = Query(18, ge=1, le=30)):
    # If user pasted a direct YouTube link, resolve that exact video
    m = _YT_ID_RE.search(q)
    if m:
        vid = m.group(1)
        info = _extract_info_with_rotation(f"https://www.youtube.com/watch?v={vid}", download=False)
        return {
            "items": [{
                "video_id": vid,
                "title": (info.get("title") or "Untitled")[:120],
                "channel": (info.get("uploader") or info.get("channel") or "YouTube")[:60],
                "duration_text": _fmt_duration(info.get("duration")),
                "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                "views_text": _fmt_views(info.get("view_count")),
            }]
        }
    return {"items": _extract_flat_list(f"ytsearch{limit}:{q}")}


@app.get("/api/streams/{video_id}")
def get_streams(video_id: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise HTTPException(status_code=400, detail="Invalid video_id")

    info = _extract_info_with_rotation(f"https://www.youtube.com/watch?v={video_id}", download=False)
    formats = info.get("formats") or []

    # Collect progressive (muxed video+audio) streams for seamless ExoPlayer playback & direct multi-GB download
    muxed = []
    audio_only = []
    seen_heights = set()

    for f in formats:
        url = f.get("url")
        if not url:
            continue
        vcodec = f.get("vcodec") or "none"
        acodec = f.get("acodec") or "none"
        ext = f.get("ext") or "mp4"
        size = f.get("filesize") or f.get("filesize_approx")

        if vcodec != "none" and acodec != "none":
            height = int(f.get("height") or 0)
            if height in seen_heights:
                continue
            seen_heights.add(height)
            muxed.append({
                "height": height,
                "format_id": str(f.get("format_id")),
                "label": f"{height}p {ext.upper()}" if height else f"Video {ext.upper()}",
                "ext": ext,
                "size_text": _fmt_size(size),
                "url": url,
            })
        elif vcodec == "none" and acodec != "none":
            abr = int(f.get("abr") or 0)
            audio_only.append({
                "abr": abr,
                "format_id": str(f.get("format_id")),
                "label": f"Audio {abr} kbps ({ext.upper()})" if abr else f"Audio ({ext.upper()})",
                "ext": ext,
                "size_text": _fmt_size(size),
                "url": url,
            })

    muxed.sort(key=lambda x: x["height"], reverse=True)
    audio_only.sort(key=lambda x: x["abr"], reverse=True)

    playback_url = muxed[0]["url"] if muxed else (audio_only[0]["url"] if audio_only else "")

    return {
        "video_id": video_id,
        "title": info.get("title") or "Untitled",
        "channel": info.get("uploader") or info.get("channel") or "YouTube",
        "duration_text": _fmt_duration(info.get("duration")),
        "playback_url": playback_url,
        "video_formats": muxed[:6],
        "audio_formats": audio_only[:4],
    }


@app.get("/api/audio/{video_id}")
def download_processed_audio(video_id: str, mode: str = Query("normal")):
    """Download & transcode to 192k MP3 or 7-Zone Beat-Synced 16D Spatial Audio."""
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
