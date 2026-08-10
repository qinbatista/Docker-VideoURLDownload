# Docker-VideoURLDownload

- `app.py` owns the FastAPI API, public-URL validation, yt-dlp execution, temporary-file expiry, and result-file serving.
- `entrypoint.py` owns writable-volume setup, then drops the container process to the unprivileged `app` user.
- `Dockerfile` defines the fixed Linux runtime; `compose.yaml` is the local and server service contract.
- `.github/workflows/publish-image.yml` publishes AMD64/ARM64 API images to GHCR; the Pi's existing Watchtower consumes image updates for `api`.
- `Caddyfile` and the optional `proxy` Compose profile own TLS termination for the public hostname; the FastAPI container remains private on the Docker network.
- The service accepts only public HTTP(S) sources. It never accepts cookies, credentials, DRM bypasses, private-network targets, or unauthenticated submission requests by default.
- Persistent runtime data is the Docker-managed `video_url_download_data` volume. It is automatically deleted one hour after each completed job.
- Done means the test image passes unit checks, the API health endpoint responds, a public test video downloads, its returned URL serves the bytes, and expiry cleanup is covered by a test.
