# Stage 1: build the demo page.
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json* ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# Stage 2: the API, which also serves the built demo page.
FROM python:3.12-slim AS app
COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml uv.lock* README.md ./
RUN uv sync --no-dev --no-install-project
COPY src/ ./src/
RUN uv sync --no-dev

COPY --from=web /web/dist ./web/dist
ENV IMBUSY_WEB_DIST=/app/web/dist \
    DATABASE_PATH=/app/data/imbusy.sqlite3
RUN mkdir -p /app/data

EXPOSE 8000
CMD ["uv", "run", "--no-dev", "uvicorn", "imbusy.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
