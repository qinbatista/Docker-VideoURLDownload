# Docker Video URL Download

`Docker-VideoURLDownload` is a FastAPI container that accepts a public webpage or media URL, asks [yt-dlp](https://github.com/yt-dlp/yt-dlp) to extract every downloadable image and video entry it finds, and returns temporary download URLs for the completed files. If a public webpage has no yt-dlp media result, it also looks for public Open Graph/Twitter image metadata and page images.

yt-dlp is the maintained open-source extractor chosen for this service because it supports thousands of sites, including dedicated YouTube, X/Twitter, and Instagram extractors as well as generic/embed extraction. Provider support changes over time, so a site is only confirmed when a live request succeeds. The image includes `ffmpeg` and Node.js 22; the supported `yt-dlp[default]` install brings in its matched `yt-dlp-ejs` component for YouTube JavaScript challenges.

## API contract

`POST /v1/downloads` accepts JSON:

```json
{"url":"https://example.com/page-with-video","max_items":25}
```

Send `X-API-Key` unless `ALLOW_ANONYMOUS=true` has been deliberately enabled. The response contains one `download_path` and `download_url` for every image or video file actually downloaded. The URLs are valid for one hour after download completion, then the job directory is removed. `GET /health` is unauthenticated and reports the configured retention period. Interactive OpenAPI documentation is available at `/docs`.

`POST /v1/shortcut-downloads` has the same request shape and is intended for the iPhone Shortcut. It immediately returns `success`, a random `job_id`, `status: "queued"`, a `poll_url`, `files: []`, and `error: []`. The Shortcut must repeat a `GET` to that opaque `poll_url` until `status` is `completed` or `failed`; completed responses replace the empty array with downloaded media, while failed responses replace `error: []` with a readable error string. Creating a job remains protected by the Shortcut's embedded `X-API-Key`; its unguessable, temporary `poll_url` works without re-entering or exposing that key. Keeping both `files` and `error` present as empty lists while queued/downloading is compatible with Shortcuts' repeat behavior, so a missing dictionary value or an empty text value cannot abort the repeat before media is ready. The Share Sheet URL remains the only user input. This prevents Shortcuts from timing out while yt-dlp or FFmpeg work. Completed file URLs remain valid for one hour; queued, active, and failed jobs remain isolated in their own job directory.

`max_items` is optional and cannot exceed `MAX_ITEMS_PER_REQUEST` (50 by default). This is an explicit resource guard for large playlists or pages; raise that setting when the operator wants a larger complete collection.

## Safety boundary

- Only public `http` and `https` URLs are accepted. Loopback, private, link-local, and reserved network destinations are rejected before yt-dlp fetches them.
- The service does not accept cookies, login credentials, DRM bypasses, paywall bypasses, or private/age-restricted source handling.
- Submission requires an API key by default. Result URLs are unguessable, temporary capability URLs so a browser can download the completed file directly.
- The compose service binds to `127.0.0.1:8787` by default. Use an authenticated reverse proxy before deliberately changing `BIND_ADDRESS` to expose it publicly.

## Run and test

Create a private `.env` from `.env.example`, replace `API_KEY` with a long random value, then run `docker compose up -d --build`. The same `docker compose` command works on macOS, Linux, and Windows Docker Desktop.

Run the containerized unit checks with `docker compose --profile test run --rm test`. A public direct video such as `https://media.w3.org/2010/05/sintel/trailer.mp4` is suitable for an integration test because it exercises the generic extractor without relying on login-only platform content.

For a reverse proxy or a public hostname, set `PUBLIC_BASE_URL` to that externally reachable base address so returned `download_url` values use it. Without that setting, the API returns a same-origin URL based on the request host plus the stable `download_path`.

## GitHub image delivery

Every push to `main` runs the containerized tests, then builds and publishes the AMD64 and ARM64 API image to `ghcr.io/qinbatista/video-url-download:latest`. The `api` service uses that image by default and is marked for Watchtower. On the configured Pi, Watchtower checks the registry every five minutes and recreates only this API container when a newer image is published. Compose or `.env` changes still require a normal Compose deploy.

## Pi HTTPS proxy

The optional `proxy` profile runs Caddy on public ports `80` and `8787`. It obtains a trusted certificate with ACME HTTP-01 over port `80`, then proxies `https://la.qyp.life:8787` to the private Docker service `api:8787`. It never binds the API container publicly. On the Pi, keep `BIND_ADDRESS=127.0.0.1`, move the API's loopback port to `PORT=8788`, set `PUBLIC_BASE_URL=https://la.qyp.life:8787`, and run `docker compose --profile proxy up -d`. Port `443` is not used by this profile. The proxy uses public DNS resolvers because it must resolve ACME endpoints independently of the host's Docker daemon resolver; `CADDY_IMAGE` defaults to the official `caddy:2` image and can point to a locally preloaded equivalent when image pulls are unavailable.
