from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from .agent_cli import (
    build_agent_args,
    ensure_chat_id,
    list_available_models,
    resolve_model,
    stream_chat_with_retries,
)
from .auth import (
    authorized,
    clear_session_cookie_header,
    client_ip,
    create_session,
    get_session_token,
    login_allowed,
    record_login_failure,
    record_login_success,
    revoke_session,
    session_cookie_header,
    verify_password,
)
from .chats import chat_store_path, list_sessions, load_history
from .config import (
    AGENT_BIN,
    CHAT_ID_RE,
    DEFAULT_MODEL,
    POPULAR_MODELS,
    TEMPLATES_DIR,
    agent_install_hint,
)
from .jobs import finish_job, get_job_status, is_stop_requested, request_stop, start_job, update_job
from .store import load_secrets, load_state, save_secrets, save_state

def _template(name: str) -> str:
    return (TEMPLATES_DIR / name).read_text(encoding="utf-8")


def json_bytes(data: dict, code: int = 200) -> tuple[int, bytes]:
    return code, json.dumps(data).encode()


class Handler(BaseHTTPRequestHandler):
    server_version = "cursor-web-agent/1"

    def log_message(self, fmt: str, *args) -> None:
        if "/api/" in (args[0] if args else ""):
            super().log_message(fmt, *args)

    def _send(
        self,
        code: int,
        body: bytes,
        content_type: str,
        cookies: list[str] | None = None,
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for name, value in extra_headers:
                self.send_header(name, value)
        if cookies:
            for cookie in cookies:
                self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str, code: int = 302) -> None:
        self.send_response(code)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _need_auth(self) -> bool:
        if authorized(self):
            return True
        body = json.dumps({"error": "unauthorized"}).encode()
        self._send(401, body, "application/json")
        return False

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/login":
            self._send(200, _template("login.html").encode(), "text/html; charset=utf-8")
            return
        if path in ("/", "/index.html"):
            if not authorized(self):
                self._redirect("/login")
                return
            self._send(200, _template("index.html").encode(), "text/html; charset=utf-8")
            return
        if not self._need_auth():
            return
        if path == "/api/status":
            secrets = load_secrets()
            state = load_state()
            has_key = bool(secrets.get("cursor_api_key") or os.environ.get("CURSOR_API_KEY"))
            code, body = json_bytes(
                {
                    "ok": True,
                    "has_key": has_key,
                    "agent": AGENT_BIN.exists(),
                    "workspace": state["workspace"],
                    "chat_id": state.get("chat_id"),
                    "mode": state.get("mode", "agent"),
                    "model": state.get("model", DEFAULT_MODEL),
                    "speed": state.get("speed", "default"),
                    "resolved_model": resolve_model(
                        state.get("model", DEFAULT_MODEL), state.get("speed", "default")
                    ),
                    "job": get_job_status(),
                }
            )
            self._send(code, body, "application/json")
            return
        if path == "/api/sessions":
            state = load_state()
            sessions = list_sessions(state["workspace"])
            code, body = json_bytes({"ok": True, "sessions": sessions})
            self._send(code, body, "application/json")
            return
        if path == "/api/models":
            state = load_state()
            models = list_available_models()
            code, body = json_bytes(
                {
                    "ok": True,
                    "models": models,
                    "model": state.get("model", DEFAULT_MODEL),
                    "speed": state.get("speed", "default"),
                    "resolved_model": resolve_model(
                        state.get("model", DEFAULT_MODEL), state.get("speed", "default")
                    ),
                    "popular": POPULAR_MODELS,
                }
            )
            self._send(code, body, "application/json")
            return
        if path == "/api/history":
            qs = parse_qs(urlparse(self.path).query)
            chat_id = (qs.get("chat_id") or [""])[0].strip()
            state = load_state()
            if not chat_id:
                chat_id = state.get("chat_id") or ""
            if not chat_id or not CHAT_ID_RE.match(chat_id):
                self._send(400, b'{"error":"invalid chat_id"}', "application/json")
                return
            try:
                limit = int((qs.get("limit") or ["40"])[0])
            except ValueError:
                limit = 40
            if limit <= 0:
                limit = None
            messages, total = load_history(state["workspace"], chat_id, limit=limit)
            code, body = json_bytes(
                {
                    "ok": True,
                    "chat_id": chat_id,
                    "messages": messages,
                    "total": total,
                    "truncated": total > len(messages),
                }
            )
            self._send(code, body, "application/json")
            return
        self._send(404, b'{"error":"not found"}', "application/json")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            payload = {}

        if path == "/api/login":
            ip = client_ip(self)
            ok, msg = login_allowed(ip)
            if not ok:
                self._send(429, json.dumps({"error": msg}, ensure_ascii=False).encode(), "application/json")
                return
            password = (payload.get("password") or "").strip()
            secrets = load_secrets()
            if not password or not verify_password(password, secrets.get("password_hash", "")):
                record_login_failure(ip)
                self._send(401, json.dumps({"error": "密码错误"}, ensure_ascii=False).encode(), "application/json")
                return
            record_login_success(ip)
            token = create_session()
            self._send(
                200,
                json.dumps({"ok": True}).encode(),
                "application/json",
                cookies=[session_cookie_header(token)],
            )
            return

        if path == "/api/logout":
            revoke_session(get_session_token(self))
            self._send(
                200,
                json.dumps({"ok": True}).encode(),
                "application/json",
                cookies=[clear_session_cookie_header()],
            )
            return

        if not self._need_auth():
            return
        try:
            payload = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            self._send(400, b'{"error":"bad json"}', "application/json")
            return
        if path == "/api/settings":
            secrets = load_secrets()
            state = load_state()
            if payload.get("api_key"):
                secrets["cursor_api_key"] = payload["api_key"].strip()
                save_secrets(secrets)
            if payload.get("workspace"):
                state["workspace"] = payload["workspace"].strip()
            if payload.get("mode") in ("agent", "ask", "plan"):
                state["mode"] = payload["mode"]
            if payload.get("model"):
                state["model"] = payload["model"].strip()
            if payload.get("speed") in ("default", "fast"):
                state["speed"] = payload["speed"]
            save_state(state)
            has_key = bool(secrets.get("cursor_api_key") or os.environ.get("CURSOR_API_KEY"))
            self._send(
                200,
                json.dumps(
                    {
                        "ok": True,
                        "has_key": has_key,
                        "mode": state.get("mode", "agent"),
                        "model": state.get("model", DEFAULT_MODEL),
                        "speed": state.get("speed", "default"),
                        "resolved_model": resolve_model(
                            state.get("model", DEFAULT_MODEL), state.get("speed", "default")
                        ),
                    }
                ).encode(),
                "application/json",
            )
            return
        if path == "/api/new-chat":
            if not AGENT_BIN.exists():
                self._send(
                    500,
                    json.dumps({"error": agent_install_hint()}, ensure_ascii=False).encode(),
                    "application/json",
                )
                return
            state = load_state()
            state["chat_id"] = None
            try:
                chat_id = ensure_chat_id(state)
            except Exception as exc:
                self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json")
                return
            self._send(200, json.dumps({"ok": True, "chat_id": chat_id}).encode(), "application/json")
            return
        if path == "/api/switch-chat":
            chat_id = (payload.get("chat_id") or "").strip()
            if not CHAT_ID_RE.match(chat_id):
                self._send(400, b'{"error":"invalid chat_id"}', "application/json")
                return
            state = load_state()
            store = chat_store_path(state["workspace"], chat_id)
            if not store.exists():
                self._send(404, b'{"error":"session not found"}', "application/json")
                return
            state["chat_id"] = chat_id
            save_state(state)
            messages, total = load_history(state["workspace"], chat_id, limit=40)
            self._send(
                200,
                json.dumps(
                    {
                        "ok": True,
                        "chat_id": chat_id,
                        "messages": messages,
                        "total": total,
                        "truncated": total > len(messages),
                    }
                ).encode(),
                "application/json",
            )
            return
        if path == "/api/chat":
            self._stream_chat(
                payload.get("text") or "",
                payload.get("mode"),
                payload.get("model"),
                payload.get("speed"),
            )
            return
        if path == "/api/stop":
            stopped = request_stop()
            self._send(
                200,
                json.dumps(
                    {"ok": True, "stopped": stopped, "job": get_job_status()},
                    ensure_ascii=False,
                ).encode(),
                "application/json",
            )
            return
        self._send(404, b'{"error":"not found"}', "application/json")

    def _stream_chat(
        self,
        text: str,
        mode: str | None = None,
        model: str | None = None,
        speed: str | None = None,
    ) -> None:
        text = text.strip()
        if not text:
            self._send(400, b'{"error":"empty"}', "application/json")
            return
        if not AGENT_BIN.exists():
            self._send(
                500,
                json.dumps({"error": agent_install_hint()}, ensure_ascii=False).encode(),
                "application/json",
            )
            return
        secrets = load_secrets()
        if not (secrets.get("cursor_api_key") or os.environ.get("CURSOR_API_KEY")):
            self._send(
                400,
                json.dumps(
                    {
                        "error": "missing api key",
                        "hint": "在设置里粘贴 API key 并点保存，顶部状态变绿后再发消息",
                    },
                    ensure_ascii=False,
                ).encode(),
                "application/json",
            )
            return
        state = load_state()
        if mode in ("agent", "ask", "plan"):
            state["mode"] = mode
        if model:
            state["model"] = model.strip()
        if speed in ("default", "fast"):
            state["speed"] = speed
        save_state(state)
        try:
            chat_id = ensure_chat_id(state)
        except Exception as exc:
            self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json")
            return

        args = build_agent_args(state, chat_id, text)

        if not start_job(chat_id, text):
            self._send(
                409,
                json.dumps(
                    {"error": "已有任务运行中", "job": get_job_status()},
                    ensure_ascii=False,
                ).encode(),
                "application/json",
            )
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def emit(obj: dict) -> None:
            try:
                chunk = f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()
                self.wfile.write(chunk)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def emit_job() -> None:
            emit({"type": "job", **get_job_status()})

        def emit_all(obj: dict) -> None:
            if obj.get("type") == "tool":
                update_job(last_tool=obj.get("text"), last_event="tool")
                emit(obj)
                emit_job()
            elif obj.get("type") == "text":
                update_job(last_event="回复中")
                emit(obj)
            else:
                emit(obj)

        emit_job()
        saw_done = False
        try:
            saw_done = stream_chat_with_retries(args, emit_all, emit_job)
            if is_stop_requested():
                finish_job("stopped")
            elif saw_done:
                finish_job("success")
            else:
                finish_job("error", "任务失败或已中断")
        except Exception as exc:
            if is_stop_requested():
                finish_job("stopped")
            else:
                finish_job("error", str(exc))
                emit_all({"type": "error", "text": str(exc)})
        finally:
            emit_job()
            if not saw_done:
                emit({"type": "done"})
