# ── NSE Paper Trader — production image ─────────────────────────────────────────
FROM python:3.12-slim

# Asia/Kolkata is required: the scheduler drives phases off IST wall-clock.
ENV TZ=Asia/Kolkata \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# tzdata so zoneinfo can resolve Asia/Kolkata; curl for the container healthcheck.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata curl \
 && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
 && echo $TZ > /etc/timezone \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first so the layer caches across code-only changes.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Application code.
COPY main.py .
COPY app/    ./app/
COPY static/ ./static/

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser \
 && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# Liveness: the dashboard status endpoint responds even before market open.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:8080/api/status || exit 1

# Single worker on purpose: AppState is an in-process singleton and the
# scheduler/WebSocket feed must not be duplicated across workers.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
