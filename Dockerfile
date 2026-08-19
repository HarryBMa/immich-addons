# Hub image. Carries the system tools the addons shell out to: ffmpeg (LUTs, video), exiftool
# (copying EXIF onto graded copies), and the Pango/Cairo stack WeasyPrint needs for the zine PDFs.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    REGISTRY_PATH=/app/registry/index.json

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libimage-exiftool-perl \
        libcairo2 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libgdk-pixbuf-2.0-0 \
        libffi8 \
        fonts-dejavu-core \
        poppler-utils \
        curl \
    && rm -rf /var/lib/apt/lists/*

# uv gives us the same resolver and lockfile as local development.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first, so editing source does not invalidate the layer.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

COPY src/ ./src/
COPY registry/ ./registry/
RUN uv sync --locked --no-dev

ENV PATH="/app/.venv/bin:${PATH}"

# /data holds luts/, music/, output/, cache/, config/ and jobs.sqlite — mount it as a volume.
VOLUME ["/data"]
EXPOSE 8484

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD curl -fsS http://localhost:8484/healthz || exit 1

CMD ["immich-addons-hub"]
