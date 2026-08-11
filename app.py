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
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import yt_dlp
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


VIDEO_SUFFIXES = {".3gp", ".avi", ".flv", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".ogv", ".ts", ".webm"}
VIDEO_MIME_PREFIX = "video/"


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


class PublicOnlyYoutubeDL(yt_dlp.YoutubeDL):
    def urlopen(self, request):
        validate_public_http_url(request_url(request))
        return super().urlopen(request)


def list_video_files(job_directory: Path) -> list[Path]:
    return [candidate for candidate in sorted(job_directory.iterdir()) if candidate.is_file() and is_video_file(candidate)]


def is_video_file(candidate: Path) -> bool:
    if candidate.name == "manifest.json":
        return False
    suffix = candidate.suffix.lower()
    if suffix in VIDEO_SUFFIXES:
        return True
    media_type = mimetypes.guess_type(candidate.name)[0]
    return media_type is not None and media_type.startswith(VIDEO_MIME_PREFIX)


def download_videos(source_url: str, job_directory: Path, max_items: int) -> list[Path]:
    downloader_options = {"outtmpl": str(job_directory / "%(autonumber)03d-%(id)s.%(ext)s"), "format": "bv*+ba/b", "merge_output_format": "mp4", "noplaylist": False, "playlistend": max_items, "max_filesize": settings.max_file_size_bytes, "nopart": True, "continuedl": False, "overwrites": False, "quiet": True, "no_warnings": True, "noprogress": True, "ignoreerrors": True, "retries": 2, "fragment_retries": 2, "socket_timeout": 30, "concurrent_fragment_downloads": 2, "restrictfilenames": True, "js_runtimes": {"node": {}}}
    try:
        with PublicOnlyYoutubeDL(downloader_options) as downloader:
            downloader.download([source_url])
    except yt_dlp.utils.DownloadError as error:
        error_text = str(error).lower()
        if "amp.twimg.com" in error_text and "domain not found" in error_text:
            raise DownloadFailed("This X post references an old video that X no longer serves.") from error
        raise DownloadFailed from error
    video_files = list_video_files(job_directory)
    if not video_files:
        raise DownloadFailed
    return video_files


def write_job_manifest(job_directory: Path, expires_at: float) -> None:
    temporary_manifest = job_directory / "manifest.tmp"
    temporary_manifest.write_text(json.dumps({"expires_at": expires_at}), encoding="utf-8")
    temporary_manifest.replace(job_directory / "manifest.json")


def read_job_expiry(job_directory: Path) -> float | None:
    try:
        return float(json.loads((job_directory / "manifest.json").read_text(encoding="utf-8"))["expires_at"])
    except (OSError, TypeError, ValueError, json.JSONDecodeError, KeyError):
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
        expires_at = read_job_expiry(candidate) or (candidate.stat().st_mtime + settings.retention_seconds)
        if expires_at <= now_value:
            shutil.rmtree(candidate, ignore_errors=True)
            removed_job_ids.append(candidate.name)
    return removed_job_ids


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
    cleanup_task = asyncio.create_task(cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task


app = FastAPI(title="Docker Video URL Download", version="1.0.0", lifespan=lifespan)


async def require_api_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
    if settings.allow_anonymous:
        return
    if not settings.api_key or not secrets.compare_digest(x_api_key or "", settings.api_key):
        raise HTTPException(status_code=401, detail="A valid X-API-Key header is required.")


def request_base_url(request: Request) -> str:
    return settings.public_base_url or str(request.base_url).rstrip("/")


def file_link(request: Request, job_id: str, video_file: Path) -> DownloadFile:
    download_path = f"/v1/files/{job_id}/{video_file.name}"
    return DownloadFile(name=video_file.name, download_path=download_path, download_url=f"{request_base_url(request)}{download_path}")


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


@app.post("/v1/downloads", response_model=DownloadResponse, status_code=201, dependencies=[Depends(require_api_key)])
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
            video_files = await asyncio.to_thread(download_videos, download_request.url, job_directory, max_items)
    except DownloadFailed as error:
        shutil.rmtree(job_directory, ignore_errors=True)
        raise HTTPException(status_code=422, detail=str(error) or "No public downloadable video was found at that URL.") from error
    except Exception as error:
        shutil.rmtree(job_directory, ignore_errors=True)
        raise HTTPException(status_code=502, detail="The downloader could not complete the request.") from error
    expires_at_epoch = time.time() + settings.retention_seconds
    write_job_manifest(job_directory, expires_at_epoch)
    return DownloadResponse(job_id=job_id, expires_at=datetime.fromtimestamp(expires_at_epoch, timezone.utc), files=[file_link(request, job_id, video_file) for video_file in video_files])


@app.get("/v1/files/{job_id}/{filename}")
async def get_file(job_id: str, filename: str) -> FileResponse:
    job_directory = get_job_directory(job_id)
    expires_at = read_job_expiry(job_directory)
    if expires_at is None or expires_at <= time.time():
        shutil.rmtree(job_directory, ignore_errors=True)
        raise HTTPException(status_code=410, detail="This temporary download has expired.")
    if Path(filename).name != filename:
        raise HTTPException(status_code=404, detail="Download not found.")
    video_file = (job_directory / filename).resolve()
    if video_file.parent != job_directory.resolve() or not video_file.is_file() or not is_video_file(video_file):
        raise HTTPException(status_code=404, detail="Download not found.")
    return FileResponse(video_file, media_type=mimetypes.guess_type(video_file.name)[0] or "application/octet-stream", filename=video_file.name)
