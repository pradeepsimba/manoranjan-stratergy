from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.services.auth import authenticate_user, get_current_user, register_user

router = APIRouter(prefix="/api/auth")


class Credentials(BaseModel):
    username: str
    password: str


def _public_user(user: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": user["id"],
        "username": user["username"],
        "funds": float(user["funds"]),
    }


@router.post("/register")
async def register(req: Credentials, request: Request) -> Dict[str, Any]:
    db = request.app.state.db
    try:
        user = await register_user(db, req.username, req.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    request.session["user_id"] = user["id"]
    return _public_user(user)


@router.post("/login")
async def login(req: Credentials, request: Request) -> Dict[str, Any]:
    db = request.app.state.db
    try:
        user = await authenticate_user(db, req.username, req.password)
    except ValueError as e:
        raise HTTPException(401, str(e))
    request.session["user_id"] = user["id"]
    return _public_user(user)


@router.post("/logout")
async def logout(request: Request) -> Dict[str, Any]:
    request.session.clear()
    return {"ok": True}


@router.get("/me")
async def me(user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    return _public_user(user)
