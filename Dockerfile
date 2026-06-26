# syntax=docker/dockerfile:1
# Lightweight, CPU-only image (well under the 500MB recommendation).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code and sample pack.
COPY app ./app
COPY SUST_Preli_Sample_Cases.json ./SUST_Preli_Sample_Cases.json

# Non-root user for safety.
RUN useradd --create-home appuser
USER appuser

EXPOSE 8000

# Bind to 0.0.0.0 and honour the PORT env var (shell form so $PORT expands).
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
