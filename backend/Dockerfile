FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/backend/.venv/bin:${PATH}"

RUN python -m pip install --no-cache-dir uv==0.11.32 \
    && groupadd --system llmbenchlab \
    && useradd --system --gid llmbenchlab --home-dir /app llmbenchlab

WORKDIR /app/backend

COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY backend/app ./app
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./alembic.ini
COPY benchmarks /app/benchmarks

RUN uv sync --frozen --no-dev \
    && mkdir -p /data \
    && chown -R llmbenchlab:llmbenchlab /app /data

USER llmbenchlab

EXPOSE 8000

CMD ["sh", "-c", "python -m app.db.prepare_migrations && alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
