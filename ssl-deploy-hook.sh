#!/bin/bash
# certbot deploy-hook: copies the current cert into certs/ (the directory
# docker-compose mounts into the app container at /app/certs) and restarts
# the app container so uvicorn picks it up - unlike nginx, uvicorn only reads
# --ssl-certfile/--ssl-keyfile at process startup, so a reload signal won't do.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIVE_DIR="${RENEWED_LINEAGE:?RENEWED_LINEAGE not set}"

APP_CONTAINER_NAME="mano-app"
if [ -f "$SCRIPT_DIR/.env" ]; then
  from_env=$(grep -m1 '^APP_CONTAINER_NAME=' "$SCRIPT_DIR/.env" | cut -d '=' -f2-)
  [ -n "$from_env" ] && APP_CONTAINER_NAME="$from_env"
fi

mkdir -p "$SCRIPT_DIR/certs"
cp "$LIVE_DIR/fullchain.pem" "$SCRIPT_DIR/certs/fullchain.pem"
cp "$LIVE_DIR/privkey.pem" "$SCRIPT_DIR/certs/privkey.pem"

docker restart "$APP_CONTAINER_NAME" 2>/dev/null || true
