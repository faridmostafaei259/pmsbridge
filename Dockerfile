# syntax=docker/dockerfile:1

# Base images are pinned by tag and digest and written out in the FROM lines (Dependabot cannot
# update a reference that goes through a build argument). The Python image appears in two stages and
# both lines must stay identical.
FROM ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d AS uv

# ---- Build stage: resolve and install dependencies from uv.lock into a virtualenv ----
FROM python:3.12-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS builder
COPY --from=uv /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /build

# Dependencies first, so this layer is reused when only the source changes.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# ---- Runtime stage ----
FROM python:3.12-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS runtime

LABEL org.opencontainers.image.title="pmsbridge" \
      org.opencontainers.image.description="Self-hosted MCP gateway for property management systems" \
      org.opencontainers.image.source="https://github.com/faridmostafaei259/pmsbridge" \
      org.opencontainers.image.licenses="Elastic-2.0"

# Apply fixed operating-system security updates that are newer than the pinned base image.
RUN apt-get update \
    && apt-get upgrade --yes --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Fixed non-root user. /data is the only writable location; the compose file mounts a volume there
# and a new named volume inherits this ownership on first use.
RUN groupadd --system --gid 10001 pmsbridge \
    && useradd --system --uid 10001 --gid 10001 --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin pmsbridge \
    && mkdir /data \
    && chown 10001:10001 /data \
    && chmod 0750 /data

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER 10001:10001
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8000

# Python is already in the image, so no extra tools are needed. A non-200 answer or a refused
# connection makes the command exit non-zero.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]

STOPSIGNAL SIGTERM
ENTRYPOINT ["pmsbridge-app"]
