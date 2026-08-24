FROM python:3.11.15-slim-bookworm AS runtime

ARG UV_VERSION=0.12.5
WORKDIR /app

RUN pip install --no-cache-dir "uv==${UV_VERSION}" \
    && groupadd --system --gid 10001 sponsorflow \
    && useradd --system --uid 10001 --gid sponsorflow --home-dir /home/sponsorflow sponsorflow

COPY pyproject.toml uv.lock alembic.ini ./
COPY backend ./backend
RUN uv sync --frozen --no-dev

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONPATH=/app/backend \
    HOME=/home/sponsorflow \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
