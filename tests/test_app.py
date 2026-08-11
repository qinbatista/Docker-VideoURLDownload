import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yt_dlp_ejs

import app


def test_private_network_sources_are_rejected() -> None:
    with pytest.raises(ValueError, match="public"):
        app.validate_public_http_url("http://127.0.0.1/video.mp4")


def test_yt_dlp_default_install_includes_ejs() -> None:
    assert yt_dlp_ejs


def test_request_url_supports_yt_dlp_requests() -> None:
    class YtDlpRequest:
        url = "https://media.example/video.mp4"

    assert app.request_url(YtDlpRequest()) == "https://media.example/video.mp4"


def test_request_url_decodes_yt_dlp_byte_urls() -> None:
    class YtDlpRequest:
        url = b"https://media.example/video.mp4"

    assert app.request_url(YtDlpRequest()) == "https://media.example/video.mp4"


def test_list_video_files_accepts_ogv_video_files(tmp_path) -> None:
    video_file = tmp_path / "recording.ogv"
    video_file.write_bytes(b"video")

    assert app.list_video_files(tmp_path) == [video_file]


def test_iphone_compatible_mp4_skips_transcoding(monkeypatch, tmp_path) -> None:
    video_file = tmp_path / "clip.mp4"
    video_file.write_bytes(b"original")

    def fake_run(arguments, **_kwargs):
        assert arguments[0] == "ffprobe"
        return SimpleNamespace(stdout=json.dumps({"streams": [{"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p"}, {"codec_type": "audio", "codec_name": "aac"}]}))

    monkeypatch.setattr(app.subprocess, "run", fake_run)

    assert app.make_video_iphone_compatible(video_file) == video_file
    assert video_file.read_bytes() == b"original"


def test_vp9_mp4_is_transcoded_to_iphone_compatible_mp4(monkeypatch, tmp_path) -> None:
    video_file = tmp_path / "clip.mp4"
    video_file.write_bytes(b"vp9")

    def fake_run(arguments, **_kwargs):
        if arguments[0] == "ffprobe":
            return SimpleNamespace(stdout=json.dumps({"streams": [{"codec_type": "video", "codec_name": "vp9", "pix_fmt": "yuv420p"}, {"codec_type": "audio", "codec_name": "aac"}]}))
        assert arguments[:2] == ["ffmpeg", "-y"]
        assert "libx264" in arguments
        assert "yuv420p" in arguments
        Path(arguments[-1]).write_bytes(b"h264")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(app.subprocess, "run", fake_run)

    assert app.make_video_iphone_compatible(video_file) == video_file
    assert video_file.read_bytes() == b"h264"
    assert not (tmp_path / "clip.iphone.mp4").exists()


def test_iphone_conversion_failure_returns_a_clear_error(monkeypatch, tmp_path) -> None:
    video_file = tmp_path / "clip.webm"
    video_file.write_bytes(b"vp9")

    def fake_run(arguments, **_kwargs):
        if arguments[0] == "ffprobe":
            return SimpleNamespace(stdout=json.dumps({"streams": [{"codec_type": "video", "codec_name": "vp9", "pix_fmt": "yuv420p"}]}))
        Path(arguments[-1]).write_bytes(b"partial")
        raise subprocess.CalledProcessError(1, arguments)

    monkeypatch.setattr(app.subprocess, "run", fake_run)

    with pytest.raises(app.DownloadFailed, match="iPhone-compatible MP4"):
        app.make_video_iphone_compatible(video_file)

    assert not (tmp_path / "clip.iphone.mp4").exists()


def test_legacy_x_amplify_failure_explains_that_x_removed_the_video(monkeypatch, tmp_path) -> None:
    class FailedYoutubeDL:
        download_errors = ["ERROR: [twitter:amplify] example: Unable to download webpage: HTTP Error 500: Domain Not Found"]

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> bool:
            return False

        def download(self, _urls) -> int:
            return 1

    monkeypatch.setattr(app, "PublicOnlyYoutubeDL", FailedYoutubeDL)

    with pytest.raises(app.DownloadFailed, match="X post references an old video"):
        app.download_videos("https://x.com/example/status/1", tmp_path, 1)


def test_expired_job_cleanup_removes_only_expired_job(tmp_path) -> None:
    expired_job = tmp_path / "expired"
    active_job = tmp_path / "active"
    expired_job.mkdir()
    active_job.mkdir()
    app.write_job_manifest(expired_job, time.time() - 1)
    app.write_job_manifest(active_job, time.time() + 60)
    assert app.cleanup_expired_jobs(tmp_path) == ["expired"]
    assert not expired_job.exists()
    assert active_job.exists()
