import time

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


def test_list_video_files_accepts_video_mime_suffixes(tmp_path) -> None:
    video_file = tmp_path / "recording.ogv"
    video_file.write_bytes(b"video")

    assert app.list_video_files(tmp_path) == [video_file]


def test_legacy_x_amplify_failure_explains_that_x_removed_the_video(monkeypatch, tmp_path) -> None:
    class FailedYoutubeDL:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> bool:
            return False

        def download(self, _urls) -> None:
            raise app.yt_dlp.utils.DownloadError("ERROR: [twitter:amplify] Unable to download webpage: HTTP Error 500: Domain Not Found (https://amp.twimg.com/v/example)")

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
