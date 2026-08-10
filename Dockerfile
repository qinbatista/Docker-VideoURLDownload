FROM node:22-bookworm-slim AS javascript_runtime

FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 XDG_CACHE_HOME=/tmp

RUN apt-get update && apt-get install --no-install-recommends -y ca-certificates ffmpeg libstdc++6 && rm -rf /var/lib/apt/lists/*
RUN addgroup --gid 10001 app && adduser --uid 10001 --gid 10001 --disabled-password --gecos "" app

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY --from=javascript_runtime /usr/local/bin/node /usr/local/bin/node

COPY app.py entrypoint.py ./
RUN mkdir -p /app/data && chown app:app /app/data

EXPOSE 8787

ENTRYPOINT ["python", "entrypoint.py"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8787", "--proxy-headers"]

FROM runtime AS test

USER root
COPY requirements-dev.txt .
RUN python -m pip install --no-cache-dir -r requirements-dev.txt
COPY tests ./tests
COPY compose.yaml ./compose.yaml
COPY .github/workflows/publish-image.yml ./.github/workflows/publish-image.yml
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
