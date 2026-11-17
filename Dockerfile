# Образ EraTrade: коллектор, сверка, API с панелью (позже — trader).

# Сборка панели (React) отдельным этапом: в итоговый образ попадает только dist.
FROM node:22-slim AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Зависимости отдельным слоем — пересобираются только при изменении lock-файла.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY config ./config
COPY --from=frontend /frontend/dist ./frontend/dist
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 10001 eratrade
USER eratrade

ENTRYPOINT ["/app/.venv/bin/eratrade"]
CMD ["--help"]
