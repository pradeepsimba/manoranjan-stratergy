#!/bin/bash
# Checks the Let's Encrypt cert for algo.vaangamart.com and renews it if it's
# due, or issues a new one via certbot standalone if none exists yet. Run this
# AFTER `docker compose up` - the deploy-hook reloads the nginx container
# (see default.conf), which needs to already exist for that reload to do
# anything.
#
# One domain cert shared with kotak-neo-order-tool's own copy of this same
# script (both point at the same /etc/letsencrypt/live/algo.vaangamart.com
# lineage) - each app independently copies+restarts on its own, so this stays
# in sync regardless of which app's deploy last set certbot's stored
# deploy-hook (only one can be registered at a time for automatic
# background renewal; whichever app deploys within the ~30-day pre-expiry
# renewal window still refreshes its own copy either way).
set -e

DOMAIN="algo.vaangamart.com"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -z "$CERTBOT_EMAIL" ] && [ -f "$SCRIPT_DIR/.env" ]; then
  CERTBOT_EMAIL=$(grep -m1 '^CERTBOT_EMAIL=' "$SCRIPT_DIR/.env" | cut -d '=' -f2-)
fi

if [ -z "$CERTBOT_EMAIL" ]; then
  echo "CERTBOT_EMAIL is not set - add it to .env" >&2
  exit 1
fi

snap list certbot >/dev/null 2>&1 || sudo snap install certbot --classic

if sudo test -d "/etc/letsencrypt/live/$DOMAIN"; then
  echo "Existing cert found for $DOMAIN - checking renewal"
  sudo snap run certbot renew --cert-name "$DOMAIN" --no-random-sleep-on-renew
else
  echo "No existing cert for $DOMAIN - requesting one"
  sudo snap run certbot certonly --standalone \
    -d "$DOMAIN" \
    --agree-tos \
    -m "$CERTBOT_EMAIL" \
    --no-eff-email \
    -n \
    --deploy-hook "$SCRIPT_DIR/ssl-deploy-hook.sh"
fi

sudo RENEWED_LINEAGE="/etc/letsencrypt/live/$DOMAIN" "$SCRIPT_DIR/ssl-deploy-hook.sh"
