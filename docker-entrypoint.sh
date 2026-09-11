#!/bin/sh
# Starts uvicorn with TLS if a cert has been provisioned into ./certs (see
# ssl.sh / ssl-deploy-hook.sh), otherwise falls back to plain HTTP - lets the
# container come up cleanly on a fresh deploy before the very first cert
# exists, instead of crash-looping on a missing --ssl-certfile.
set -e

if [ -f /app/certs/fullchain.pem ] && [ -f /app/certs/privkey.pem ]; then
  exec uvicorn main:app --host 0.0.0.0 --port 8080 --workers 1 \
    --ssl-certfile /app/certs/fullchain.pem --ssl-keyfile /app/certs/privkey.pem
else
  exec uvicorn main:app --host 0.0.0.0 --port 8080 --workers 1
fi
