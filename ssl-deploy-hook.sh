#!/bin/bash
# certbot deploy-hook: copies the current cert into certs/ (the directory
# docker-compose mounts into the nginx container at /etc/nginx/ssl) and
# reloads nginx so it picks up the new files - nginx supports a graceful
# config/cert reload without dropping connections, unlike uvicorn (which is
# plain HTTP only now; nginx is the one terminating TLS - see default.conf).
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIVE_DIR="${RENEWED_LINEAGE:?RENEWED_LINEAGE not set}"

NGINX_CONTAINER_NAME="mano-nginx"
if [ -f "$SCRIPT_DIR/.env" ]; then
  from_env=$(grep -m1 '^NGINX_CONTAINER_NAME=' "$SCRIPT_DIR/.env" | cut -d '=' -f2-)
  [ -n "$from_env" ] && NGINX_CONTAINER_NAME="$from_env"
fi

mkdir -p "$SCRIPT_DIR/certs"
cp "$LIVE_DIR/fullchain.pem" "$SCRIPT_DIR/certs/fullchain.pem"
cp "$LIVE_DIR/privkey.pem" "$SCRIPT_DIR/certs/privkey.pem"
chmod 644 "$SCRIPT_DIR/certs/fullchain.pem" "$SCRIPT_DIR/certs/privkey.pem"

docker exec "$NGINX_CONTAINER_NAME" nginx -s reload 2>/dev/null || docker restart "$NGINX_CONTAINER_NAME" 2>/dev/null || true
