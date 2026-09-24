# ── NSE Paper Trader — production image ─────────────────────────────────────────
FROM python:3.12-slim

# Asia/Kolkata is required: the scheduler drives phases off IST wall-clock.
ENV TZ=Asia/Kolkata \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# tzdata so zoneinfo can resolve Asia/Kolkata; curl for the healthcheck.
# (build-essential/wget + the TA-Lib C library compile that used to live here
# were REMOVED 2026-09-24 — TA-Lib's own gate module, app/engine/bn_signals.py/
# nf_signals.py, was fully deleted 2026-09-21; a repo-wide grep confirmed zero
# remaining `talib` imports anywhere across several independent review rounds
# before this was finally acted on — every rebuild was paying for a full
# native C compile, plus build-essential/wget staying baked into the final
# runtime image, for a dependency nothing imported any more. See CLAUDE.md's
# former "TA-Lib is now dead weight" gotcha, now removed alongside this.)
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

# Liveness: /healthz is the one route with no login required (see main.py) -
# /api/status itself is behind login now, same as everything else in this app.
# Plain HTTP - TLS is terminated by the nginx service in front of this one
# (see docker-compose.yml / default.conf), not by uvicorn itself.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:8080/healthz || exit 1

# Single worker on purpose: AppState is an in-process singleton and the
# scheduler/WebSocket feed must not be duplicated across workers.
# --forwarded-allow-ips='*': this container is expose-only (unreachable except
# via the nginx service, which sets X-Forwarded-Proto - see default.conf), so
# uvicorn's default of trusting only 127.0.0.1 would otherwise silently keep
# request.url.scheme "http" here and main.py's login cookie would never get
# its Secure flag, even though the public connection really is HTTPS.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--forwarded-allow-ips=*"]
