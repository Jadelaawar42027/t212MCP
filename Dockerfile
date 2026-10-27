# syntax=docker/dockerfile:1

# ---- build: resolve the locked dependencies into a venv ----
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.12 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ---- runtime: slim image, non-root, no build tools ----
FROM python:3.12-slim
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=build --chown=10001:10001 /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000
USER 10001
EXPOSE 8000
# Binds 0.0.0.0:$PORT (Railway injects PORT).
CMD ["finance-mcp"]
