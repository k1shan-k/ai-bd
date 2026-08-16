FROM python:3.11-slim AS runtime
WORKDIR /app
COPY pyproject.toml alembic.ini ./
COPY backend ./backend
RUN pip install --no-cache-dir .
ENV PYTHONPATH=/app/backend
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
