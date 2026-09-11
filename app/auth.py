from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler

from .config import (
    LOGIN_LOCKOUT_AFTER,
    LOGIN_LOCKOUT_SECONDS,
    LOGIN_RATE_MAX,
    LOGIN_RATE_WINDOW,
    SESSION_COOKIE,
    SESSION_TTL,
)
from .store import load_secrets, save_secrets

_auth_lock = threading.Lock()
_login_attempts: dict[str, list[float]] = {}
_login_lockouts: dict[str, float] = {}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return f"pbkdf2_sha256${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, digest_hex = stored.split("$", 2)
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), 200_000
        )
        return secrets.compare_digest(digest.hex(), digest_hex)
    except (ValueError, AttributeError):
        return False


def ensure_password_configured() -> None:
    data = load_secrets()
    env_pw = os.environ.get("AGENT_WEB_PASSWORD", "").strip()
    if env_pw:
        data["password_hash"] = hash_password(env_pw)
        save_secrets(data)
        print("Web login password set from AGENT_WEB_PASSWORD.", flush=True)
    if not data.get("password_hash"):
        raise SystemExit(
            "No web login password configured.\n"
            "Start with:\n"
            "  set AGENT_WEB_PASSWORD=your-long-random-password\n"
            "  python server.py\n"
            "On Linux/macOS:\n"
            "  AGENT_WEB_PASSWORD='your-long-random-password' python3 server.py"
        )


def _prune_sessions(sessions: dict) -> dict:
    now = time.time()
    return {tok: meta for tok, meta in sessions.items() if meta.get("expires", 0) > now}


def create_session() -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    data = load_secrets()
    sessions = _prune_sessions(data.get("sessions", {}))
    sessions[token] = {"created": now, "expires": now + SESSION_TTL}
    data["sessions"] = sessions
    save_secrets(data)
    return token


def revoke_session(token: str) -> None:
    if not token:
        return
    data = load_secrets()
    sessions = data.get("sessions", {})
    sessions.pop(token, None)
    data["sessions"] = sessions
    save_secrets(data)


def session_valid(token: str) -> bool:
    if not token:
        return False
    data = load_secrets()
    meta = data.get("sessions", {}).get(token)
    if not meta:
        return False
    if meta.get("expires", 0) <= time.time():
        revoke_session(token)
        return False
    return True


def client_ip(handler: BaseHTTPRequestHandler) -> str:
    forwarded = handler.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return handler.client_address[0]


def login_allowed(ip: str) -> tuple[bool, str]:
    now = time.time()
    with _auth_lock:
        locked_until = _login_lockouts.get(ip, 0)
        if locked_until > now:
            wait = int(locked_until - now)
            return False, f"登录尝试过多，请 {wait} 秒后再试"
        attempts = [t for t in _login_attempts.get(ip, []) if now - t < LOGIN_RATE_WINDOW]
        _login_attempts[ip] = attempts
        if len(attempts) >= LOGIN_RATE_MAX:
            return False, "登录过于频繁，请稍后再试"
    return True, ""


def record_login_failure(ip: str) -> None:
    now = time.time()
    with _auth_lock:
        attempts = _login_attempts.setdefault(ip, [])
        attempts.append(now)
        attempts[:] = [t for t in attempts if now - t < LOGIN_LOCKOUT_SECONDS]
        if len(attempts) >= LOGIN_LOCKOUT_AFTER:
            _login_lockouts[ip] = now + LOGIN_LOCKOUT_SECONDS


def record_login_success(ip: str) -> None:
    with _auth_lock:
        _login_attempts.pop(ip, None)
        _login_lockouts.pop(ip, None)


def get_session_token(handler: BaseHTTPRequestHandler) -> str:
    cookie_header = handler.headers.get("Cookie", "")
    if not cookie_header:
        return ""
    jar = SimpleCookie()
    jar.load(cookie_header)
    morsel = jar.get(SESSION_COOKIE)
    return morsel.value if morsel else ""


def request_is_https(handler: BaseHTTPRequestHandler) -> bool:
    proto = handler.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower()
    return proto == "https"


def session_cookie_header(
    token: str,
    max_age: int | None = None,
    *,
    secure: bool = False,
) -> str:
    parts = [
        f"{SESSION_COOKIE}={token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
    ]
    if secure:
        parts.append("Secure")
    if max_age is None:
        parts.append(f"Max-Age={SESSION_TTL}")
    else:
        parts.append(f"Max-Age={max_age}")
    return "; ".join(parts)


def clear_session_cookie_header(*, secure: bool = False) -> str:
    parts = [
        f"{SESSION_COOKIE}=",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        "Max-Age=0",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def authorized(handler: BaseHTTPRequestHandler) -> bool:
    return session_valid(get_session_token(handler))
