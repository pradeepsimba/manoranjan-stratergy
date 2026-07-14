from __future__ import annotations

"""
Session-cookie auth. Password hashing uses stdlib hashlib.pbkdf2_hmac (no new
native-extension dependency — this repo already has one native-build headache
documented in CLAUDE.md for the now-deleted TA-Lib requirement, no need for a
second one). Session state itself is Starlette's signed-cookie
SessionMiddleware (main.py) — `request.session["user_id"]` is the only thing
stored server-side-free, so there is no server session table to manage.
"""

import hashlib
import hmac
import os
from typing import Any, Dict, Optional

from fastapi import HTTPException, Request

import app.config as cfg
from app.services.database import DatabaseService

_PBKDF2_ITERATIONS = 260_000


def hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    salt = salt or os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITERATIONS
    )
    return digest.hex(), salt


def verify_password(password: str, password_hash: str, password_salt: str) -> bool:
    candidate, _ = hash_password(password, password_salt)
    return hmac.compare_digest(candidate, password_hash)


async def register_user(db: DatabaseService, username: str, password: str) -> Dict[str, Any]:
    username = username.strip()
    if len(username) < 3:
        raise ValueError("username must be at least 3 characters")
    if len(password) < 6:
        raise ValueError("password must be at least 6 characters")
    if await db.get_user_by_username(username) is not None:
        raise ValueError("username already taken")
    password_hash, salt = hash_password(password)
    return await db.create_user(username, password_hash, salt, cfg.STARTING_FUNDS)


async def authenticate_user(db: DatabaseService, username: str, password: str) -> Dict[str, Any]:
    user = await db.get_user_by_username(username.strip())
    if user is None or not verify_password(password, user["password_hash"], user["password_salt"]):
        raise ValueError("invalid username or password")
    return user


async def get_current_user(request: Request) -> Dict[str, Any]:
    """FastAPI dependency — 401s if there's no logged-in user on this session."""
    user_id = request.session.get("user_id")
    if user_id is None:
        raise HTTPException(401, "Not logged in")
    db: DatabaseService = request.app.state.db
    user = await db.get_user(user_id)
    if user is None:
        request.session.clear()
        raise HTTPException(401, "Not logged in")
    return user


def user_id_from_session(conn: Any) -> Optional[int]:
    """
    Best-effort user id lookup for WS connect handshakes (no dependency
    injection there). `conn` is a Request or a WebSocket — both expose
    `.session` via SessionMiddleware, which applies to both scope types.
    """
    return conn.session.get("user_id")
