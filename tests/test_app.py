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
