# Stage 1: Build dependencies and sync virtual environment
FROM ghcr.io/astral-sh/uv:python3.12-alpine AS builder

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

WORKDIR /app

# Install dependencies first using cache mounts for maximum speed
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --frozen --no-install-project --no-dev

# Stage 2: Final lightweight production image
FROM python:3.12-alpine

WORKDIR /app

# Copy the pre-built virtual environment from the builder stage
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Copy your local data and source scripts
COPY epg_service.py ./

ENV PORT=9090
# Run uvicorn inside the optimized virtual environment
ENTRYPOINT ["sh", "-c", "uvicorn epg_service:app --host 0.0.0.0 --port ${PORT}"]
