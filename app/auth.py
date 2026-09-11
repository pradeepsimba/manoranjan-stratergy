from __future__ import annotations

"""
Whole-app login - a single shared username/password gate via a signed session
cookie (not a browser-native Basic Auth popup), applied to every page/API
route except /ws/dashboard (explicit user decision - the alert feature's
live price feed) and /login itself. Ported from kotak-neo-order-tool's
app/web/auth.py: constant-time credential comparison, a per-source-IP
failed-login lockout, and a signed (itsdangerous) session cookie - no
server-side session store needed.
"""

import hmac
import threading
import time
from dataclasses import dataclass

from fastapi import Cookie, HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import app.config as cfg

COOKIE_NAME = "session"
SESSION_MAX_AGE_SEC = 12 * 3600
MAX_FAILURES = 5
LOCKOUT_SEC = 15 * 60
IDLE_RETENTION_SEC = LOCKOUT_SEC


@dataclass
class _Failures:
    count: int
    locked_until: float | None
    last_attempt: float


class LoginAttemptLimiter:
    """Per-source-IP failed-login limiter, in-memory only - correct for the
    single-instance deployment this app targets. allow() both checks AND
    reserves the attempt under one lock acquisition so a burst of concurrent
    requests from the same IP can't each pass the check before any of them
    records a failure (which would multiply the effective attempt budget by
    the burst size instead of capping it at MAX_FAILURES).
    """

    def __init__(self):
        self._by_ip: dict[str, _Failures] = {}
        self._lock = threading.Lock()
        self._calls_since_sweep = 0

    def allow(self, ip: str) -> bool:
        with self._lock:
            self._maybe_sweep_locked()
            now = time.time()
            f = self._by_ip.get(ip)
            if f is not None and f.locked_until is not None and now <= f.locked_until:
                return False
            fresh_window = f is None or f.locked_until is not None
            count = 1 if fresh_window else f.count + 1
            locked_until = now + LOCKOUT_SEC if count >= MAX_FAILURES else None
            self._by_ip[ip] = _Failures(count, locked_until, now)
            return True

    def record_success(self, ip: str) -> None:
        with self._lock:
            self._by_ip.pop(ip, None)

    def _maybe_sweep_locked(self) -> None:
        self._calls_since_sweep += 1
        if self._calls_since_sweep % 200 != 0:
            return
        now = time.time()

        def is_stale(f: _Failures) -> bool:
            if f.locked_until is not None:
                return now > f.locked_until
            return now - f.last_attempt > IDLE_RETENTION_SEC

        stale = [ip for ip, f in self._by_ip.items() if is_stale(f)]
        for ip in stale:
            self._by_ip.pop(ip, None)


limiter = LoginAttemptLimiter()


def _serializer() -> URLSafeTimedSerializer:
    if not cfg.SESSION_SECRET:
        # Fail closed - an unset secret must not silently mean "no auth" or,
        # worse, a signing key of "" that anyone could forge tokens against.
        raise HTTPException(500, "SESSION_SECRET is not configured")
    return URLSafeTimedSerializer(cfg.SESSION_SECRET, salt="dashboard-session")


def is_logged_in(session: str | None) -> bool:
    if not session:
        return False
    try:
        payload = _serializer().loads(session, max_age=SESSION_MAX_AGE_SEC)
    except (BadSignature, SignatureExpired):
        return False
    return payload == {"user": cfg.SETTINGS_USER}


def check_credentials(username: str, password: str) -> bool:
    # Both comparisons always run, even once username_ok is already False -
    # `and`-short-circuiting would make "wrong username" measurably faster
    # than "right username, wrong password", leaking which one was correct
    # through timing despite each compare_digest() call being constant-time.
    username_ok = hmac.compare_digest(username, cfg.SETTINGS_USER)
    password_ok = bool(password) and bool(cfg.SETTINGS_PASSWORD) \
        and hmac.compare_digest(password, cfg.SETTINGS_PASSWORD)
    return username_ok and password_ok


def make_session_token() -> str:
    return _serializer().dumps({"user": cfg.SETTINGS_USER})


def require_login(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> None:
    """Gate for API routes - 401, no redirect (see app/api/dashboard.py)."""
    if not is_logged_in(session):
        raise HTTPException(status_code=401, detail="Not logged in")


def require_login_page(
    request: Request, session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> None:
    """Gate for page routes - redirects to /login (see main.py)."""
    if not is_logged_in(session):
        raise HTTPException(status_code=307, detail="Not logged in", headers={"Location": "/login"})
