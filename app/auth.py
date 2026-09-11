from __future__ import annotations

"""
Settings-page auth — a single shared username/password gate (browser-native
HTTP Basic Auth), applied only to the Settings page and its /api/settings*
routes. Nothing else in the app (dashboard, positions, prices, manual
order/exit, backtest, the WebSocket feed) requires login — explicit user
decision, since only Settings can change live trading behavior.
"""

import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import app.config as cfg

_security = HTTPBasic()


def require_settings_auth(credentials: HTTPBasicCredentials = Depends(_security)) -> None:
    if not cfg.SETTINGS_PASSWORD:
        # Fail closed - an unset password must not silently mean "no auth".
        raise HTTPException(500, "SETTINGS_PASSWORD is not configured")

    user_ok = secrets.compare_digest(credentials.username, cfg.SETTINGS_USER)
    pass_ok = secrets.compare_digest(credentials.password, cfg.SETTINGS_PASSWORD)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
