# Build stage: resolve and install dependencies with uv.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Install third-party dependencies first so this layer caches across source edits.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Runtime stage: only the virtual environment and the source.
FROM python:3.12-slim-bookworm

RUN useradd --system --uid 1000 --create-home provisioner

COPY --from=builder --chown=provisioner:provisioner /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER provisioner
WORKDIR /app

# Mount the YAML config at /etc/provisioner/config.yaml and pass secrets as environment
# variables. See config.example.yaml and .env.example.
ENTRYPOINT ["sync-user-collections"]
