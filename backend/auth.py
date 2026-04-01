"""
Authentication routes: register, login, logout, session management, rate limiting.
"""

import hmac
import json
import os
import re
import secrets
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Cookie, HTTPException
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel

from .config import (
    ADMIN_EMAIL, ADMIN_SECRET, RATE_LIMIT_MAX, RATE_LIMIT_WINDOW,
    SESSION_COOKIE, SESSION_DAYS,
)
from .db import _ensure_admin_user, get_db
from .passwords import hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])

# ─────────────────────────────────────────────
# Rate limiter (in-memory, per-IP)
# ─────────────────────────────────────────────
_rate_buckets: dict[str, list[float]] = defaultdict(list)


def _check_rate_limit(ip: str) -> None:
    now = time.monotonic()
    bucket = _rate_buckets[ip]
    # Prune old entries
    _rate_buckets[ip] = [t for t in bucket if now - t < RATE_LIMIT_WINDOW]
    if len(_rate_buckets[ip]) >= RATE_LIMIT_MAX:
        raise HTTPException(status_code=429, detail="Too many attempts — try again later")
    _rate_buckets[ip].append(now)


# ─────────────────────────────────────────────
# Session helpers
# ─────────────────────────────────────────────
def _create_session(user_id: int) -> str:
    token   = secrets.token_hex(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat()
    conn = get_db()
    with conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, expires_at) VALUES (?,?,?)",
            (token, user_id, expires),
        )
        conn.commit()
    conn.close()
    return token


def get_user_from_token(token: str | None) -> dict | None:
    if not token:
        return None
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    row  = conn.execute(
        """SELECT u.id, u.email, u.display_name, u.role
           FROM sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token=? AND s.expires_at > ?""",
        (token, now),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def require_user(ogai_session: str | None = Cookie(default=None)) -> dict:
    user = get_user_from_token(ogai_session)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        secure=bool(os.environ.get("RENDER")),
        max_age=SESSION_DAYS * 86400,
        path="/",
    )


# ─────────────────────────────────────────────
# Pydantic models
# ─────────────────────────────────────────────
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RegisterPayload(BaseModel):
    email: str
    password: str
    display_name: str


class LoginPayload(BaseModel):
    email: str
    password: str


class AdminLoginPayload(BaseModel):
    secret: str


# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────
@router.post("/register", status_code=201)
def register(body: RegisterPayload):
    email    = body.email.strip().lower()
    name     = body.display_name.strip()
    password = body.password
    if not email or not name or not password:
        raise HTTPException(status_code=422, detail="All fields are required")
    if len(password) < 12:
        raise HTTPException(status_code=422, detail="Password must be at least 12 characters")
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="Invalid email address")

    conn = get_db()
    if conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
        conn.close()
        raise HTTPException(status_code=409, detail="An account with that email already exists")

    ph, ps = hash_password(password)
    with conn:
        conn.execute(
            "INSERT INTO users (email, display_name, password_hash, password_salt) VALUES (?,?,?,?)",
            (email, name, ph, ps),
        )
        conn.commit()
    user = conn.execute("SELECT id, email, display_name FROM users WHERE email=?", (email,)).fetchone()
    conn.close()
    return dict(user)


@router.post("/login")
def login(body: LoginPayload):
    email = body.email.strip().lower()
    conn  = get_db()
    user  = conn.execute(
        "SELECT id, email, display_name, password_hash, password_salt, role FROM users WHERE email=?",
        (email,),
    ).fetchone()
    conn.close()

    if not user or not verify_password(body.password, user["password_hash"], user["password_salt"]):
        raise HTTPException(status_code=401, detail="Incorrect email or password")

    token    = _create_session(user["id"])
    response = Response(
        content=json.dumps({
            "id": user["id"], "email": user["email"],
            "display_name": user["display_name"], "role": user["role"],
        }),
        media_type="application/json",
    )
    _set_session_cookie(response, token)
    return response


@router.post("/admin")
def admin_login(body: AdminLoginPayload):
    if not ADMIN_SECRET:
        raise HTTPException(status_code=503, detail="Admin secret not configured")
    if not hmac.compare_digest(body.secret, ADMIN_SECRET):
        raise HTTPException(status_code=401, detail="Invalid admin secret")

    _ensure_admin_user()
    conn = get_db()
    user = conn.execute(
        "SELECT id, email, display_name, role FROM users WHERE email=?", (ADMIN_EMAIL,)
    ).fetchone()
    conn.close()
    if not user:
        raise HTTPException(status_code=500, detail="Admin user could not be initialised")

    token    = _create_session(user["id"])
    response = Response(
        content=json.dumps({
            "id": user["id"], "email": user["email"],
            "display_name": user["display_name"], "role": user["role"],
        }),
        media_type="application/json",
    )
    _set_session_cookie(response, token)
    return response


@router.post("/logout")
def logout(ogai_session: str | None = Cookie(default=None)):
    if ogai_session:
        conn = get_db()
        with conn:
            conn.execute("DELETE FROM sessions WHERE token=?", (ogai_session,))
            conn.commit()
        conn.close()
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/me")
def me(ogai_session: str | None = Cookie(default=None)):
    user = get_user_from_token(ogai_session)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user
