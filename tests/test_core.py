#!/usr/bin/env python3
"""Offline unit & FFmpeg 16D Spatial DSP verification tests for SR King Web."""

import math
import os
import shutil
import struct
import sys
import tempfile
import types
import wave

# Allow running directly from repo root
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

# Use isolated temp data dir before importing app modules
_TEST_ROOT = tempfile.mkdtemp(prefix="srking_test_")
os.environ["DOWNLOAD_ROOT"] = _TEST_ROOT

# Allow running core & FFmpeg tests even in minimal offline environments without yt_dlp installed
try:
    import yt_dlp  # noqa: F401
except ImportError:
    stub = types.ModuleType("yt_dlp")
    stub.version = types.SimpleNamespace(__version__="stub")
    sys.modules["yt_dlp"] = stub

from app import config, downloader, jobs
from app.storage import WebStorage


def test_youtube_url_validation() -> None:
    valid_cases = [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?t=42", "dQw4w9WgXcQ"),
        ("https://youtube.com/shorts/abcdefghijk", "abcdefghijk"),
        ("https://music.youtube.com/watch?v=1234567890A&feature=share", "1234567890A"),
        ("https://m.youtube.com/watch?v=_-123456789", "_-123456789"),
    ]
    for raw, expected_id in valid_cases:
        canonical, vid = downloader.detect_youtube_url(raw)
        assert vid == expected_id, f"Expected {expected_id}, got {vid} for {raw}"
        assert canonical == f"https://www.youtube.com/watch?v={expected_id}"

    invalid_cases = [
        "",
        "https://instagram.com/reel/abc12345678/",
        "https://vimeo.com/123456789",
        "http://127.0.0.1:8080/health",
        "file:///etc/passwd",
        "https://youtube.com/watch?v=short",
    ]
    for raw in invalid_cases:
        canonical, vid = downloader.detect_youtube_url(raw)
        assert canonical is None and vid is None, f"Should reject non-YouTube URL: {raw}"
    print("[PASS] YouTube URL validation & SSRF rejection")


def test_filename_sanitization_and_helpers() -> None:
    name = downloader.sanitize_filename('Arijit Singh: "Kesariya" <4K> / Official?', "mp4", suffix="[1080p]")
    assert name.endswith("[1080p].mp4")
    for bad in '<>:"/\\|?*':
        assert bad not in name
    assert downloader.format_duration(3665) == "1:01:05"
    assert downloader.format_duration(125) == "2:05"
    assert downloader.human_size(1024 * 1024 * 5) == "5.0 MB"
    print("[PASS] Filename sanitization & formatting helpers")


def test_storage_wal_and_metrics() -> None:
    db_file = os.path.join(_TEST_ROOT, "test.db")
    st = WebStorage(db_file)
    st.set_setting("last_working_client", "tv_embedded")
    assert st.get_setting("last_working_client") == "tv_embedded"
    st.record_download("dQw4w9WgXcQ", "Test Track", "spatial16d", "16d_320", 8500000, 210, False)
    st.record_download("dQw4w9WgXcQ", "Test Track", "video", "1080p", 55000000, 210, True)
    stats = st.get_stats()
    assert stats["total_downloads"] == 2
    assert stats["cache_hits"] == 1
    assert stats["by_type"]["spatial16d"] == 1
    jm = jobs.JobManager(storage=st, max_workers=1)
    assert jm.active_jobs_count() == 0
    jm.shutdown()
    st.close()
    print("[PASS] SQLite WAL storage, metrics & JobManager init")


def test_disk_cache_roundtrip() -> None:
    src = os.path.join(_TEST_ROOT, "sample.mp3")
    with open(src, "wb") as f:
        f.write(b"ID3" + b"\x00" * 4096)
    downloader.disk_cache_save(
        "dQw4w9WgXcQ",
        "audio",
        "mp3_320",
        src,
        {"title": "Sample", "ext": "mp3", "filename": "Sample.mp3"},
    )
    hit = downloader.disk_cache_lookup("dQw4w9WgXcQ", "audio", "mp3_320")
    assert hit is not None
    cached_path, meta = hit
    assert os.path.isfile(cached_path)
    assert meta["title"] == "Sample"
    removed, freed = downloader.disk_cache_clear()
    assert removed >= 1 and freed > 0
    print("[PASS] Disk cache save, lookup & clear")


def test_ffmpeg_16d_spatial_pipeline() -> None:
    if not downloader.FFMPEG_PATH:
        print("[SKIP] FFmpeg not installed in environment")
        return

    wav_path = os.path.join(_TEST_ROOT, "beat_120.wav")
    sr = 44100
    dur = 4
    n_samples = sr * dur
    with wave.open(wav_path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        frames = bytearray()
        for i in range(n_samples):
            t = i / sr
            beat_env = math.exp(-((t % 0.5) * 18.0))
            val = int(16000 * beat_env * math.sin(2 * math.pi * 220 * t))
            frames += struct.pack("<hh", val, val)
        wf.writeframes(bytes(frames))

    bpm, phase = downloader.estimate_bpm_and_phase(wav_path, duration=dur)
    assert 80.0 <= bpm <= 160.0, f"Unexpected BPM: {bpm}"
    fc = downloader.build_16d_filter_complex(bpm=bpm, effective_dur=dur, fade_in=0.5, fade_out=0.5, phase=phase)

    out_mp3 = downloader._encode_audio_mp3(
        src_path=wav_path,
        work_dir=_TEST_ROOT,
        video_id="test1234567",
        bitrate_kbps=192,
        title="Test 16D Track",
        artist="SR King",
        cover_path=None,
        filter_complex=fc,
    )
    assert os.path.isfile(out_mp3) and os.path.getsize(out_mp3) > 10000
    print(f"[PASS] FFmpeg 7-Zone 16D Spatial DSP pipeline (BPM={bpm}, output={os.path.getsize(out_mp3)} bytes)")


def main() -> None:
    try:
        test_youtube_url_validation()
        test_filename_sanitization_and_helpers()
        test_storage_wal_and_metrics()
        test_disk_cache_roundtrip()
        test_ffmpeg_16d_spatial_pipeline()
        print("\nALL TESTS PASSED.")
    finally:
        shutil.rmtree(_TEST_ROOT, ignore_errors=True)


if __name__ == "__main__":
    main()
