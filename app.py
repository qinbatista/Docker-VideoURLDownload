from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import mimetypes
import os
import secrets
import shutil
import socket
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Annotated
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request as UrllibRequest, build_opener

import yt_dlp
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


VIDEO_SUFFIXES = {".3gp", ".avi", ".flv", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".ogv", ".ts", ".webm"}
IMAGE_SUFFIXES = {".avif", ".gif", ".heic", ".heif", ".jpeg", ".jpg", ".png", ".webp"}
VIDEO_MIME_PREFIX = "video/"
IMAGE_MIME_PREFIX = "image/"
IPHONE_IMAGE_SUFFIXES = {".gif", ".heic", ".heif", ".jpeg", ".jpg", ".png"}
MAX_PAGE_HTML_BYTES = 1_000_000
IMAGE_READ_CHUNK_BYTES = 64 * 1024
IPHONE_FORMAT_SELECTOR = "bestvideo[vcodec^=avc1][ext=mp4][height<=720]+bestaudio[acodec^=mp4a][ext=m4a]/best[vcodec^=avc1][acodec^=mp4a][ext=mp4][height<=720]/bv*+ba/b"
VIDEO_DOWNLOAD_PATH = "/video-download"


def positive_integer(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise RuntimeError(f"{name} must be at least 1.")
    return value


@dataclass(frozen=True)
class Settings:
    data_directory: Path
    retention_seconds: int
    cleanup_interval_seconds: int
    max_items_per_request: int
    max_file_size_bytes: int
    max_concurrent_downloads: int
    api_key: str | None
    allow_anonymous: bool
    public_base_url: str | None


settings = Settings(data_directory=Path(os.getenv("DATA_DIR", "/app/data")), retention_seconds=positive_integer("RETENTION_SECONDS", 3600), cleanup_interval_seconds=positive_integer("CLEANUP_INTERVAL_SECONDS", 60), max_items_per_request=positive_integer("MAX_ITEMS_PER_REQUEST", 50), max_file_size_bytes=positive_integer("MAX_FILE_SIZE_BYTES", 2147483648), max_concurrent_downloads=positive_integer("MAX_CONCURRENT_DOWNLOADS", 2), api_key=os.getenv("API_KEY") or None, allow_anonymous=os.getenv("ALLOW_ANONYMOUS", "false").strip().lower() in {"1", "true", "yes"}, public_base_url=os.getenv("PUBLIC_BASE_URL", "").rstrip("/") or None)
jobs_directory = settings.data_directory / "jobs"
download_semaphore = asyncio.Semaphore(settings.max_concurrent_downloads)
shortcut_job_tasks: set[asyncio.Task[None]] = set()


class DownloadFailed(Exception):
    pass


class DownloadRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    max_items: int | None = Field(default=None, ge=1)


class DownloadFile(BaseModel):
    name: str
    download_path: str
    download_url: str


class DownloadResponse(BaseModel):
    job_id: str
    expires_at: datetime
    files: list[DownloadFile]


class ShortcutDownloadResponse(BaseModel):
    success: bool
    job_id: str | None = None
    status: str
    poll_url: str | None = None
    expires_at: datetime | None = None
    # An error is meaningful only once a job has failed.  The Shortcut keeps
    # the complete poll dictionary in a named variable, so an omitted error
    # value does not interrupt its pending-download loop.
    error: str | None = None
    # Keep this present even while the background job is pending.  Shortcuts
    # handles an empty JSON list reliably, whereas a missing key can abort a
    # repeat before it reaches the next poll.
    files: list[DownloadFile] = Field(default_factory=list)


def ensure_data_paths() -> None:
    jobs_directory.mkdir(parents=True, exist_ok=True)


def resolve_public_host(hostname: str, port: int) -> None:
    try:
        addresses = {record[4][0] for record in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)}
    except socket.gaierror as error:
        raise ValueError("The source host could not be resolved.") from error
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("Only public internet hosts are allowed.")


def validate_public_http_url(candidate_url: str) -> None:
    try:
        parsed_url = urlsplit(candidate_url)
        port = parsed_url.port
    except ValueError as error:
        raise ValueError("A valid public HTTP or HTTPS URL is required.") from error
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname or parsed_url.username or parsed_url.password:
        raise ValueError("A valid public HTTP or HTTPS URL is required.")
    resolve_public_host(parsed_url.hostname, port or (443 if parsed_url.scheme == "https" else 80))


def request_url(request: object) -> str:
    for attribute_name in ("full_url", "url"):
        candidate_url = getattr(request, attribute_name, None)
        if isinstance(candidate_url, bytes):
            return candidate_url.decode("utf-8", errors="replace")
        if isinstance(candidate_url, str):
            return candidate_url
    get_full_url = getattr(request, "get_full_url", None)
    if callable(get_full_url):
        candidate_url = get_full_url()
        if isinstance(candidate_url, bytes):
            return candidate_url.decode("utf-8", errors="replace")
        if isinstance(candidate_url, str):
            return candidate_url
    raise ValueError("A valid public HTTP or HTTPS URL is required.")


def is_retired_x_amplify_error(error_text: str) -> bool:
    normalized_error = error_text.lower()
    return ("twitter:amplify" in normalized_error or "amp.twimg.com" in normalized_error) and "domain not found" in normalized_error


class PublicOnlyYoutubeDL(yt_dlp.YoutubeDL):
    def __init__(self, *args, **kwargs) -> None:
        self.download_errors: list[str] = []
        super().__init__(*args, **kwargs)

    def urlopen(self, request):
        validate_public_http_url(request_url(request))
        return super().urlopen(request)

    def trouble(self, message=None, tb=None, is_error=True):
        if message is not None and is_error:
            self.download_errors.append(str(message))
        return super().trouble(message, tb, is_error)


class PublicOnlyRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, status_code, message, headers, redirect_url):
        validate_public_http_url(redirect_url)
        return super().redirect_request(request, file_pointer, status_code, message, headers, redirect_url)


class PageImageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.image_urls: list[str] = []

    def handle_starttag(self, tag: str, attributes: list[tuple[str, str | None]]) -> None:
        values = {name.lower(): value for name, value in attributes if value is not None}
        if tag == "meta":
            image_property = (values.get("property") or values.get("name") or values.get("itemprop") or "").lower()
            if image_property in {"og:image", "og:image:url", "twitter:image", "twitter:image:src", "image"}:
                self.add_url(values.get("content"))
        elif tag == "img":
            self.add_url(values.get("src"))

    def add_url(self, candidate_url: str | None) -> None:
        if candidate_url and candidate_url not in self.image_urls:
            self.image_urls.append(candidate_url)


def public_url_opener():
    return build_opener(PublicOnlyRedirectHandler())


def list_media_files(job_directory: Path) -> list[Path]:
    return [candidate for candidate in sorted(job_directory.iterdir()) if candidate.is_file() and is_media_file(candidate)]


def list_video_files(job_directory: Path) -> list[Path]:
    return [candidate for candidate in sorted(job_directory.iterdir()) if candidate.is_file() and is_video_file(candidate)]


def is_media_file(candidate: Path) -> bool:
    return is_video_file(candidate) or is_image_file(candidate)


def is_video_file(candidate: Path) -> bool:
    if candidate.name == "manifest.json":
        return False
    suffix = candidate.suffix.lower()
    if suffix in VIDEO_SUFFIXES:
        return True
    media_type = mimetypes.guess_type(candidate.name)[0]
    return media_type is not None and media_type.startswith(VIDEO_MIME_PREFIX)


def is_image_file(candidate: Path) -> bool:
    if candidate.name == "manifest.json":
        return False
    suffix = candidate.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return True
    media_type = mimetypes.guess_type(candidate.name)[0]
    return media_type is not None and media_type.startswith(IMAGE_MIME_PREFIX)


def make_video_iphone_compatible(video_file: Path) -> Path:
    iphone_video_file = video_file.with_name(f"{video_file.stem}.iphone.mp4")
    try:
        probe_result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,pix_fmt", "-of", "json", str(video_file)], check=True, capture_output=True, text=True, timeout=30)
        streams = json.loads(probe_result.stdout)["streams"]
        video_streams = [stream for stream in streams if stream["codec_type"] == "video"]
        audio_streams = [stream for stream in streams if stream["codec_type"] == "audio"]
        if not video_streams:
            raise ValueError("The downloaded file did not contain a video stream.")
        is_iphone_compatible = video_file.suffix.lower() == ".mp4" and all(stream["codec_name"] == "h264" and stream.get("pix_fmt") == "yuv420p" for stream in video_streams) and all(stream["codec_name"] == "aac" for stream in audio_streams)
        codec_arguments = ["-c:v", "copy", "-c:a", "copy"] if is_iphone_compatible else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac"]
        download_time = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        subprocess.run(["ffmpeg", "-y", "-i", str(video_file), "-map", "0:v:0", "-map", "0:a:0?", *codec_arguments, "-metadata", f"creation_time={download_time}", "-movflags", "+faststart", str(iphone_video_file)], check=True, capture_output=True, text=True, timeout=1800)
        if not iphone_video_file.is_file() or iphone_video_file.stat().st_size == 0:
            raise ValueError("FFmpeg did not create a video file.")
        destination_file = video_file.with_suffix(".mp4")
        iphone_video_file.replace(destination_file)
        if video_file != destination_file:
            video_file.unlink()
        return destination_file
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        iphone_video_file.unlink(missing_ok=True)
        raise DownloadFailed("The downloaded video could not be converted to an iPhone-compatible MP4.") from error


def make_image_iphone_compatible(image_file: Path) -> Path:
    if image_file.suffix.lower() in IPHONE_IMAGE_SUFFIXES:
        return image_file
    iphone_image_file = image_file.with_name(f"{image_file.stem}.iphone.jpg")
    try:
        subprocess.run(["ffmpeg", "-y", "-i", str(image_file), "-map", "0:v:0", "-frames:v", "1", "-q:v", "2", str(iphone_image_file)], check=True, capture_output=True, text=True, timeout=300)
        if not iphone_image_file.is_file() or iphone_image_file.stat().st_size == 0:
            raise ValueError("FFmpeg did not create an image file.")
        destination_file = image_file.with_suffix(".jpg")
        iphone_image_file.replace(destination_file)
        if image_file != destination_file:
            image_file.unlink()
        return destination_file
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        iphone_image_file.unlink(missing_ok=True)
        raise DownloadFailed("The downloaded image could not be converted to an iPhone-compatible JPEG.") from error


def make_media_iphone_compatible(media_file: Path) -> Path:
    if is_video_file(media_file):
        return make_video_iphone_compatible(media_file)
    if is_image_file(media_file):
        return make_image_iphone_compatible(media_file)
    raise DownloadFailed("The downloaded file was not an image or video.")


def page_image_urls(source_url: str) -> list[str]:
    try:
        request = UrllibRequest(source_url, headers={"User-Agent": "Mozilla/5.0"})
        with public_url_opener().open(request, timeout=30) as response:
            if response.headers.get_content_type() not in {"application/xhtml+xml", "text/html"}:
                return []
            page_bytes = response.read(MAX_PAGE_HTML_BYTES + 1)
            if len(page_bytes) > MAX_PAGE_HTML_BYTES:
                return []
            page_url = response.geturl()
            page_encoding = response.headers.get_content_charset() or "utf-8"
        parser = PageImageParser()
        parser.feed(page_bytes.decode(page_encoding, errors="replace"))
        return [urljoin(page_url, image_url) for image_url in parser.image_urls]
    except (OSError, ValueError):
        return []


def image_suffix(content_type: str) -> str:
    return {"image/avif": ".avif", "image/gif": ".gif", "image/heic": ".heic", "image/heif": ".heif", "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(content_type, mimetypes.guess_extension(content_type) or ".img")


def download_page_image(image_url: str, image_file: Path) -> Path | None:
    temporary_file = image_file.with_suffix(".part")
    destination_file = image_file
    try:
        validate_public_http_url(image_url)
        request = UrllibRequest(image_url, headers={"User-Agent": "Mozilla/5.0"})
        with public_url_opener().open(request, timeout=30) as response:
            content_type = response.headers.get_content_type().lower()
            if not content_type.startswith(IMAGE_MIME_PREFIX):
                return None
            destination_file = image_file.with_suffix(image_suffix(content_type))
            temporary_file = destination_file.with_suffix(f"{destination_file.suffix}.part")
            content_length = response.headers.get("Content-Length")
            if content_length is not None and int(content_length) > settings.max_file_size_bytes:
                return None
            total_bytes = 0
            with temporary_file.open("xb") as destination:
                while chunk := response.read(IMAGE_READ_CHUNK_BYTES):
                    total_bytes += len(chunk)
                    if total_bytes > settings.max_file_size_bytes:
                        raise ValueError("The image exceeded the maximum download size.")
                    destination.write(chunk)
        temporary_file.replace(destination_file)
        return make_image_iphone_compatible(destination_file)
    except (OSError, ValueError):
        temporary_file.unlink(missing_ok=True)
        destination_file.unlink(missing_ok=True)
        return None


def download_page_images(source_url: str, job_directory: Path, max_items: int) -> list[Path]:
    image_files: list[Path] = []
    for image_url in page_image_urls(source_url):
        if len(image_files) >= max_items:
            break
        image_file = job_directory / f"image-{len(image_files) + 1:03d}.jpg"
        downloaded_image = download_page_image(image_url, image_file)
        if downloaded_image is not None:
            image_files.append(downloaded_image)
    return image_files


def download_media(source_url: str, job_directory: Path, max_items: int) -> list[Path]:
    downloader_options = {"outtmpl": str(job_directory / "%(autonumber)03d-%(id)s.%(ext)s"), "format": IPHONE_FORMAT_SELECTOR, "merge_output_format": "mp4", "noplaylist": False, "playlistend": max_items, "max_filesize": settings.max_file_size_bytes, "nopart": True, "continuedl": False, "overwrites": False, "quiet": True, "no_warnings": True, "noprogress": True, "ignoreerrors": True, "retries": 2, "fragment_retries": 2, "socket_timeout": 30, "concurrent_fragment_downloads": 2, "restrictfilenames": True, "js_runtimes": {"node": {}}}
    downloader = PublicOnlyYoutubeDL(downloader_options)
    try:
        with downloader:
            downloader.download([source_url])
    except yt_dlp.utils.DownloadError as error:
        if is_retired_x_amplify_error(str(error)):
            raise DownloadFailed("This X post references an old video that X no longer serves.") from error
    media_files = list_media_files(job_directory)
    if media_files:
        return [make_media_iphone_compatible(media_file) for media_file in media_files]
    image_files = download_page_images(source_url, job_directory, max_items)
    if not image_files:
        if any(is_retired_x_amplify_error(error_message) for error_message in downloader.download_errors):
            raise DownloadFailed("This X post references an old video that X no longer serves.")
        raise DownloadFailed
    return image_files


def write_job_manifest(job_directory: Path, expires_at: float | None, status: str = "completed", error: str | None = None) -> None:
    temporary_manifest = job_directory / "manifest.tmp"
    temporary_manifest.write_text(json.dumps({"expires_at": expires_at, "status": status, "error": error}), encoding="utf-8")
    temporary_manifest.replace(job_directory / "manifest.json")


def read_job_manifest(job_directory: Path) -> dict[str, object] | None:
    try:
        manifest = json.loads((job_directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return manifest if isinstance(manifest, dict) else None


def read_job_expiry(job_directory: Path) -> float | None:
    try:
        manifest = read_job_manifest(job_directory)
        return float(manifest["expires_at"]) if manifest is not None else None
    except (TypeError, ValueError, KeyError):
        return None


def cleanup_expired_jobs(jobs_root: Path | None = None, now_epoch: float | None = None) -> list[str]:
    active_jobs_root = jobs_root or jobs_directory
    if not active_jobs_root.exists():
        return []
    now_value = now_epoch if now_epoch is not None else time.time()
    removed_job_ids: list[str] = []
    for candidate in active_jobs_root.iterdir():
        if not candidate.is_dir() or candidate.is_symlink():
            continue
        if read_job_manifest(candidate) is not None and read_job_expiry(candidate) is None:
            continue
        expires_at = read_job_expiry(candidate) or (candidate.stat().st_mtime + settings.retention_seconds)
        if expires_at <= now_value:
            shutil.rmtree(candidate, ignore_errors=True)
            removed_job_ids.append(candidate.name)
    return removed_job_ids


def recover_unfinished_shortcut_jobs() -> None:
    if not jobs_directory.exists():
        return
    for job_directory in jobs_directory.iterdir():
        if not job_directory.is_dir() or job_directory.is_symlink():
            continue
        manifest = read_job_manifest(job_directory)
        if manifest is None or manifest.get("status") not in {"queued", "downloading"}:
            continue
        write_job_manifest(job_directory, time.time() + settings.retention_seconds, status="failed", error="The server restarted before this download could finish.")


async def cleanup_loop() -> None:
    while True:
        await asyncio.sleep(settings.cleanup_interval_seconds)
        await asyncio.to_thread(cleanup_expired_jobs)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not settings.allow_anonymous and not settings.api_key:
        raise RuntimeError("API_KEY must be configured unless ALLOW_ANONYMOUS=true.")
    ensure_data_paths()
    cleanup_expired_jobs()
    recover_unfinished_shortcut_jobs()
    cleanup_task = asyncio.create_task(cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task
        if shortcut_job_tasks:
            await asyncio.gather(*tuple(shortcut_job_tasks), return_exceptions=True)


app = FastAPI(title="Docker Video URL Download", version="1.0.0", lifespan=lifespan)


async def require_api_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
    if settings.allow_anonymous:
        return
    if not settings.api_key or not secrets.compare_digest(x_api_key or "", settings.api_key):
        raise HTTPException(status_code=401, detail="A valid X-API-Key header is required.")


def request_base_url(request: Request) -> str:
    return settings.public_base_url or str(request.base_url).rstrip("/")


def file_link(request: Request, job_id: str, media_file: Path) -> DownloadFile:
    download_path = f"{VIDEO_DOWNLOAD_PATH}/{job_id}/{media_file.name}"
    return DownloadFile(name=media_file.name, download_path=download_path, download_url=f"{request_base_url(request)}{download_path}")


def shortcut_poll_url(request: Request, job_id: str) -> str:
    return f"{request_base_url(request)}{VIDEO_DOWNLOAD_PATH}/{job_id}"


def get_job_directory(job_id: str) -> Path:
    try:
        uuid.UUID(hex=job_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="Download not found.") from error
    job_directory = (jobs_directory / job_id).resolve()
    if job_directory.parent != jobs_directory.resolve():
        raise HTTPException(status_code=404, detail="Download not found.")
    return job_directory


@app.get("/health")
async def health() -> dict[str, int | str]:
    return {"status": "ok", "retention_seconds": settings.retention_seconds}


async def create_download(download_request: DownloadRequest, request: Request) -> DownloadResponse:
    try:
        validate_public_http_url(download_request.url)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    max_items = download_request.max_items or settings.max_items_per_request
    if max_items > settings.max_items_per_request:
        raise HTTPException(status_code=422, detail=f"max_items cannot exceed {settings.max_items_per_request}.")
    job_id = uuid.uuid4().hex
    job_directory = jobs_directory / job_id
    job_directory.mkdir(parents=True, exist_ok=False)
    try:
        async with download_semaphore:
            media_files = await asyncio.to_thread(download_media, download_request.url, job_directory, max_items)
    except DownloadFailed as error:
        shutil.rmtree(job_directory, ignore_errors=True)
        raise HTTPException(status_code=422, detail=str(error) or "No public downloadable image or video was found at that URL.") from error
    except Exception as error:
        shutil.rmtree(job_directory, ignore_errors=True)
        raise HTTPException(status_code=502, detail="The downloader could not complete the request.") from error
    expires_at_epoch = time.time() + settings.retention_seconds
    write_job_manifest(job_directory, expires_at_epoch)
    return DownloadResponse(job_id=job_id, expires_at=datetime.fromtimestamp(expires_at_epoch, timezone.utc), files=[file_link(request, job_id, media_file) for media_file in media_files])


async def process_shortcut_download(download_request: DownloadRequest, job_directory: Path, max_items: int) -> None:
    try:
        async with download_semaphore:
            write_job_manifest(job_directory, None, status="downloading")
            await asyncio.to_thread(download_media, download_request.url, job_directory, max_items)
    except DownloadFailed as error:
        write_job_manifest(job_directory, time.time() + settings.retention_seconds, status="failed", error=str(error) or "No public downloadable image or video was found at that URL.")
    except Exception:
        write_job_manifest(job_directory, time.time() + settings.retention_seconds, status="failed", error="The downloader could not complete the request.")
    else:
        write_job_manifest(job_directory, time.time() + settings.retention_seconds)


@app.post(VIDEO_DOWNLOAD_PATH, response_model=ShortcutDownloadResponse, response_model_exclude_none=True, dependencies=[Depends(require_api_key)])
async def create_shortcut_download(download_request: DownloadRequest, request: Request) -> ShortcutDownloadResponse:
    try:
        validate_public_http_url(download_request.url)
    except ValueError as error:
        return ShortcutDownloadResponse(success=False, status="failed", error=str(error))
    max_items = download_request.max_items or settings.max_items_per_request
    if max_items > settings.max_items_per_request:
        return ShortcutDownloadResponse(success=False, status="failed", error=f"max_items cannot exceed {settings.max_items_per_request}.")
    job_id = uuid.uuid4().hex
    job_directory = jobs_directory / job_id
    job_directory.mkdir(parents=True, exist_ok=False)
    write_job_manifest(job_directory, None, status="queued")
    shortcut_task = asyncio.create_task(process_shortcut_download(download_request, job_directory, max_items))
    shortcut_job_tasks.add(shortcut_task)
    shortcut_task.add_done_callback(shortcut_job_tasks.discard)
    return ShortcutDownloadResponse(success=True, job_id=job_id, status="queued", poll_url=shortcut_poll_url(request, job_id))


@app.get(f"{VIDEO_DOWNLOAD_PATH}/{{job_id}}", response_model=ShortcutDownloadResponse, response_model_exclude_none=True)
async def get_shortcut_download(job_id: str, request: Request) -> ShortcutDownloadResponse:
    job_directory = get_job_directory(job_id)
    if not job_directory.is_dir():
        raise HTTPException(status_code=404, detail="Download not found.")
    manifest = read_job_manifest(job_directory)
    if manifest is None:
        raise HTTPException(status_code=404, detail="Download not found.")
    expires_at_epoch = read_job_expiry(job_directory)
    if expires_at_epoch is not None and expires_at_epoch <= time.time():
        shutil.rmtree(job_directory, ignore_errors=True)
        raise HTTPException(status_code=410, detail="This temporary download has expired.")
    status = str(manifest.get("status", "completed"))
    expires_at = datetime.fromtimestamp(expires_at_epoch, timezone.utc) if expires_at_epoch is not None else None
    if status == "completed":
        media_files = list_media_files(job_directory)
        if not media_files:
            return ShortcutDownloadResponse(success=False, job_id=job_id, status="failed", expires_at=expires_at, error="The server completed the download without any media files.")
        return ShortcutDownloadResponse(success=True, job_id=job_id, status=status, poll_url=shortcut_poll_url(request, job_id), expires_at=expires_at, files=[file_link(request, job_id, media_file) for media_file in media_files])
    if status == "failed":
        error = manifest.get("error")
        return ShortcutDownloadResponse(success=False, job_id=job_id, status=status, poll_url=shortcut_poll_url(request, job_id), expires_at=expires_at, error=error if isinstance(error, str) and error else "The downloader failed without an error message.")
    return ShortcutDownloadResponse(success=True, job_id=job_id, status=status, poll_url=shortcut_poll_url(request, job_id), expires_at=expires_at)


@app.get(f"{VIDEO_DOWNLOAD_PATH}/{{job_id}}/{{filename}}")
async def get_file(job_id: str, filename: str) -> FileResponse:
    job_directory = get_job_directory(job_id)
    expires_at = read_job_expiry(job_directory)
    if expires_at is None or expires_at <= time.time():
        shutil.rmtree(job_directory, ignore_errors=True)
        raise HTTPException(status_code=410, detail="This temporary download has expired.")
    if Path(filename).name != filename:
        raise HTTPException(status_code=404, detail="Download not found.")
    media_file = (job_directory / filename).resolve()
    if media_file.parent != job_directory.resolve() or not media_file.is_file() or not is_media_file(media_file):
        raise HTTPException(status_code=404, detail="Download not found.")
    return FileResponse(media_file, media_type=mimetypes.guess_type(media_file.name)[0] or "application/octet-stream", filename=media_file.name)
