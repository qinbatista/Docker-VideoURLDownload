import asyncio
import json
import subprocess
import threading
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


def test_list_media_files_accepts_image_files(tmp_path) -> None:
    image_file = tmp_path / "photo.webp"
    image_file.write_bytes(b"image")

    assert app.list_media_files(tmp_path) == [image_file]


def test_page_image_parser_finds_open_graph_and_image_sources() -> None:
    parser = app.PageImageParser()
    parser.feed('<meta property="og:image" content="/preview.jpg"><img src="https://cdn.example/image.png">')

    assert parser.image_urls == ["/preview.jpg", "https://cdn.example/image.png"]


def test_iphone_compatible_mp4_is_remuxed_with_the_download_time(monkeypatch, tmp_path) -> None:
    video_file = tmp_path / "clip.mp4"
    video_file.write_bytes(b"original")

    def fake_run(arguments, **_kwargs):
        if arguments[0] == "ffprobe":
            return SimpleNamespace(stdout=json.dumps({"streams": [{"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p"}, {"codec_type": "audio", "codec_name": "aac"}]}))
        assert arguments[:2] == ["ffmpeg", "-y"]
        assert "libx264" not in arguments
        assert arguments[arguments.index("-c:v") + 1] == "copy"
        assert arguments[arguments.index("-c:a") + 1] == "copy"
        assert any(argument.startswith("creation_time=") for argument in arguments)
        Path(arguments[-1]).write_bytes(b"remuxed")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(app.subprocess, "run", fake_run)

    assert app.make_video_iphone_compatible(video_file) == video_file
    assert video_file.read_bytes() == b"remuxed"


def test_vp9_mp4_is_transcoded_to_iphone_compatible_mp4(monkeypatch, tmp_path) -> None:
    video_file = tmp_path / "clip.mp4"
    video_file.write_bytes(b"vp9")

    def fake_run(arguments, **_kwargs):
        if arguments[0] == "ffprobe":
            return SimpleNamespace(stdout=json.dumps({"streams": [{"codec_type": "video", "codec_name": "vp9", "pix_fmt": "yuv420p"}, {"codec_type": "audio", "codec_name": "aac"}]}))
        assert arguments[:2] == ["ffmpeg", "-y"]
        assert "libx264" in arguments
        assert "yuv420p" in arguments
        assert any(argument.startswith("creation_time=") for argument in arguments)
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


def test_webp_image_is_transcoded_to_an_iphone_compatible_jpeg(monkeypatch, tmp_path) -> None:
    image_file = tmp_path / "photo.webp"
    image_file.write_bytes(b"webp")

    def fake_run(arguments, **_kwargs):
        assert arguments[:2] == ["ffmpeg", "-y"]
        assert arguments[arguments.index("-frames:v") + 1] == "1"
        Path(arguments[-1]).write_bytes(b"jpeg")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(app.subprocess, "run", fake_run)

    converted_file = app.make_image_iphone_compatible(image_file)

    assert converted_file == tmp_path / "photo.jpg"
    assert converted_file.read_bytes() == b"jpeg"
    assert not image_file.exists()


def test_download_media_accepts_a_static_image_from_yt_dlp(monkeypatch, tmp_path) -> None:
    class ImageYoutubeDL:
        download_errors: list[str] = []

        def __init__(self, downloader_options) -> None:
            assert downloader_options["format"] == app.IPHONE_FORMAT_SELECTOR
            assert "bestvideo[vcodec^=avc1][ext=mp4]" in downloader_options["format"]
            assert "bestaudio[acodec^=mp4a][ext=m4a]" in downloader_options["format"]
            assert "[height<=720]" in downloader_options["format"]

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> bool:
            return False

        def download(self, _urls) -> int:
            (tmp_path / "001-photo.jpg").write_bytes(b"image")
            return 0

    monkeypatch.setattr(app, "PublicOnlyYoutubeDL", ImageYoutubeDL)

    assert app.download_media("https://images.example/photo.jpg", tmp_path, 1) == [tmp_path / "001-photo.jpg"]


def test_simultaneous_shortcut_jobs_are_isolated_and_queue_after_two(monkeypatch, tmp_path) -> None:
    async def verify_isolated_jobs() -> list[app.DownloadResponse]:
        recorded_job_directories: list[Path] = []
        release_downloads = threading.Event()
        two_downloads_started = threading.Event()
        download_count_lock = threading.Lock()
        active_download_count = 0
        peak_download_count = 0

        def fake_download_media(_source_url: str, job_directory: Path, _max_items: int) -> list[Path]:
            nonlocal active_download_count, peak_download_count
            with download_count_lock:
                active_download_count += 1
                peak_download_count = max(peak_download_count, active_download_count)
                recorded_job_directories.append(job_directory)
                if active_download_count == 2:
                    two_downloads_started.set()
            assert release_downloads.wait(timeout=1)
            media_file = job_directory / "clip.mp4"
            media_file.write_bytes(b"video")
            with download_count_lock:
                active_download_count -= 1
            return [media_file]

        monkeypatch.setattr(app, "jobs_directory", tmp_path / "jobs")
        monkeypatch.setattr(app, "download_semaphore", asyncio.Semaphore(2))
        monkeypatch.setattr(app, "validate_public_http_url", lambda _url: None)
        monkeypatch.setattr(app, "download_media", fake_download_media)
        request = SimpleNamespace(base_url="https://downloads.example/")
        youtube_download = asyncio.create_task(app.create_download(app.DownloadRequest(url="https://youtube.com/watch?v=example"), request))
        instagram_download = asyncio.create_task(app.create_download(app.DownloadRequest(url="https://instagram.com/reel/example"), request))
        x_download = asyncio.create_task(app.create_download(app.DownloadRequest(url="https://x.com/example/status/1"), request))
        assert await asyncio.to_thread(two_downloads_started.wait, 1)
        assert peak_download_count == 2
        assert len(recorded_job_directories) == 2
        await asyncio.sleep(0.05)
        assert len(recorded_job_directories) == 2
        release_downloads.set()
        downloads = await asyncio.gather(youtube_download, instagram_download, x_download)
        assert len(set(recorded_job_directories)) == 3
        assert all(job_directory.parent == app.jobs_directory for job_directory in recorded_job_directories)
        return downloads

    downloads = asyncio.run(verify_isolated_jobs())

    assert all(download.files[0].download_path.startswith(f"/v1/files/{download.job_id}/") for download in downloads)


def test_shortcut_download_starts_immediately_then_reports_completion(monkeypatch, tmp_path) -> None:
    async def verify_shortcut_job() -> None:
        download_started = threading.Event()
        release_download = threading.Event()

        def fake_download_media(_source_url: str, job_directory: Path, _max_items: int) -> list[Path]:
            download_started.set()
            assert release_download.wait(timeout=1)
            media_file = job_directory / "clip.mp4"
            media_file.write_bytes(b"video")
            return [media_file]

        monkeypatch.setattr(app, "jobs_directory", tmp_path / "jobs")
        monkeypatch.setattr(app, "shortcut_job_tasks", set())
        monkeypatch.setattr(app, "validate_public_http_url", lambda _url: None)
        monkeypatch.setattr(app, "download_media", fake_download_media)
        request = SimpleNamespace(base_url="https://downloads.example/")
        started_response = await app.create_shortcut_download(app.DownloadRequest(url="https://instagram.com/reel/example"), request)
        assert started_response.success is True
        assert started_response.status == "queued"
        assert started_response.job_id is not None
        assert started_response.files == []
        assert started_response.poll_url == f"https://downloads.example/v1/shortcut-downloads/{started_response.job_id}"
        assert await asyncio.to_thread(download_started.wait, 1)
        in_progress_response = await app.get_shortcut_download(started_response.job_id, request)
        assert in_progress_response.success is True
        assert in_progress_response.status == "downloading"
        assert in_progress_response.files == []
        assert in_progress_response.poll_url == started_response.poll_url
        release_download.set()
        for _ in range(20):
            completed_response = await app.get_shortcut_download(started_response.job_id, request)
            if completed_response.status == "completed":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("The queued Shortcut download did not complete.")
        assert completed_response.success is True
        assert completed_response.status == "completed"
        assert completed_response.poll_url == started_response.poll_url
        assert completed_response.files
        assert completed_response.files[0].download_path.startswith(f"/v1/files/{started_response.job_id}/")

    asyncio.run(verify_shortcut_job())


def test_pending_shortcut_response_keeps_an_explicit_empty_files_array() -> None:
    response = app.ShortcutDownloadResponse(success=True, job_id="job", status="queued", poll_url="https://downloads.example/v1/shortcut-downloads/job")

    assert response.model_dump(exclude_none=True)["files"] == []


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
        app.download_media("https://x.com/example/status/1", tmp_path, 1)


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


def test_expired_job_cleanup_preserves_a_queued_shortcut_job(tmp_path) -> None:
    queued_job = tmp_path / "queued"
    queued_job.mkdir()
    app.write_job_manifest(queued_job, None, status="queued")

    assert app.cleanup_expired_jobs(tmp_path, now_epoch=time.time() + app.settings.retention_seconds + 1) == []
    assert queued_job.exists()


def test_server_restart_marks_an_unfinished_shortcut_job_as_failed(monkeypatch, tmp_path) -> None:
    queued_job = tmp_path / "jobs" / "queued"
    queued_job.mkdir(parents=True)
    app.write_job_manifest(queued_job, None, status="downloading")
    monkeypatch.setattr(app, "jobs_directory", tmp_path / "jobs")

    app.recover_unfinished_shortcut_jobs()

    manifest = app.read_job_manifest(queued_job)
    assert manifest is not None
    assert manifest["status"] == "failed"
    assert manifest["error"] == "The server restarted before this download could finish."
    assert app.read_job_expiry(queued_job) is not None
