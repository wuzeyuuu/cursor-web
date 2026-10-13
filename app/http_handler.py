from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
from daily_indicator.review.ledger_server import LedgerHandler  # noqa: E402

from .agent_cli import (
    build_agent_args,
    ensure_chat_id,
    list_available_models,
    log_agent,
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
    request_is_https,
    session_cookie_header,
    verify_password,
)
from .chats import (
    chat_exists,
    chat_source,
    chat_store_path,
    list_sessions,
    load_history,
    parse_known,
    sessions_etag,
)
from .config import (
    AGENT_BIN,
    CHAT_ID_RE,
    DEFAULT_MODEL,
    POPULAR_MODELS,
    TEMPLATES_DIR,
    agent_install_hint,
)
from .jobs import (
    begin_turn,
    cancel_turn,
    edit_turn,
    end_turn,
    finish_job,
    get_job_status,
    is_stop_requested,
    list_active_jobs,
    promote_turn,
    request_stop,
    start_job,
    update_job,
    wait_turn,
)
from .store import load_secrets, load_state, save_secrets, save_state

def _template(name: str) -> str:
    return (TEMPLATES_DIR / name).read_text(encoding="utf-8")


def json_bytes(data: dict, code: int = 200) -> tuple[int, bytes]:
    return code, json.dumps(data).encode()


_LEDGER_EXACT = {
    "/api/health",
    "/api/universe",
    "/api/dates",
    "/api/mindset",
    "/api/questions",
    "/api/symbols",
    "/api/watchlist",
}


def _is_ledger_path(path: str) -> bool:
    if path in {"/reviewpools", "/reviewpools/"}:
        return True
    if path in {"/review", "/eod"} or path.startswith("/review/") or path.startswith("/eod/"):
        return True
    if path in _LEDGER_EXACT or path.startswith("/api/mindset/") or path.startswith("/api/questions/"):
        return True
    return (
        path.startswith("/api/symbol/")
        or path.startswith("/api/md/")
        or path.startswith("/api/chart/")
        or path.startswith("/api/drawings/")
    )


class Handler(LedgerHandler):
    server_version = "cursor-web-agent/1"

    def _serve_ledger(self) -> bool:
        path = urlparse(self.path).path
        if not _is_ledger_path(path):
            return False
        if self.command != "OPTIONS" and not authorized(self):
            if path.startswith("/api/"):
                self._need_auth()
            else:
                self._redirect("/login")
            return True
        method = getattr(LedgerHandler, f"do_{self.command}", None)
        if method is None:
            self._send(405, b'{"error":"method not allowed"}', "application/json")
            return True
        method(self)
        return True

    def do_PUT(self) -> None:
        if self._serve_ledger():
            return
        self._send(404, b'{"error":"not found"}', "application/json")

    def do_DELETE(self) -> None:
        if self._serve_ledger():
            return
        self._send(404, b'{"error":"not found"}', "application/json")

    def do_OPTIONS(self) -> None:
        if self._serve_ledger():
            return
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

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

    def _send_qq_dd(self, path: str) -> None:
        from daily_indicator.cli.eod_board_http import _qq_dd_root, _safe_file, send_file, send_json

        rel = path
        for prefix in ("/review/qq-dd", "/eod/qq-dd"):
            if path == prefix or path.startswith(prefix + "/"):
                rel = path[len(prefix) :].lstrip("/") or "index.html"
                break
        if rel.endswith("/"):
            rel = rel + "index.html"
        file_path = _safe_file(_qq_dd_root(), rel)
        if file_path:
            send_file(self, file_path)
            return
        send_json(self, {"error": "missing qq_dd_review", "file": rel, "dir": str(_qq_dd_root())}, 404)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in {"/review/qq-dd", "/eod/qq-dd"} or path.startswith("/review/qq-dd/") or path.startswith("/eod/qq-dd/"):
            if not authorized(self):
                self._redirect("/login")
                return
            self._send_qq_dd(path)
            return
        if self._serve_ledger():
            return
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
            chat_id = state.get("chat_id")
            code, body = json_bytes(
                {
                    "ok": True,
                    "has_key": has_key,
                    "agent": AGENT_BIN.exists(),
                    "workspace": state["workspace"],
                    "chat_id": chat_id,
                    "chat_source": chat_source(state["workspace"], chat_id or ""),
                    "mode": state.get("mode", "agent"),
                    "model": state.get("model", DEFAULT_MODEL),
                    "speed": state.get("speed", "default"),
                    "resolved_model": resolve_model(
                        state.get("model", DEFAULT_MODEL), state.get("speed", "default")
                    ),
                    "job": get_job_status(chat_id),
                    "jobs": list_active_jobs(),
                    "show_thinking": bool(state.get("show_thinking")),
                }
            )
            self._send(code, body, "application/json")
            return
        if path == "/api/sessions":
            qs = parse_qs(urlparse(self.path).query)
            known = parse_known((qs.get("known") or [""])[0])
            state = load_state()
            sessions = list_sessions(state["workspace"], known=known)
            etag = sessions_etag(sessions)
            inm = (self.headers.get("If-None-Match") or "").strip().strip('"')
            if inm and inm == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            code, body = json_bytes({"ok": True, "sessions": sessions, "etag": etag})
            self._send(code, body, "application/json", extra_headers=[("ETag", etag)])
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
            after_rowid = None
            before_rowid = None
            try:
                if qs.get("after_rowid"):
                    after_rowid = int(qs["after_rowid"][0])
                if qs.get("before_rowid"):
                    before_rowid = int(qs["before_rowid"][0])
            except ValueError:
                after_rowid = None
                before_rowid = None
            hist = load_history(
                state["workspace"],
                chat_id,
                limit=limit,
                after_rowid=after_rowid,
                before_rowid=before_rowid,
            )
            source = chat_source(state["workspace"], chat_id)
            code, body = json_bytes(
                {"ok": True, "chat_id": chat_id, "source": source, **hist}
            )
            self._send(code, body, "application/json")
            return
        self._send(404, b'{"error":"not found"}', "application/json")

    def do_POST(self) -> None:
        if self._serve_ledger():
            return
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
                cookies=[session_cookie_header(token, secure=request_is_https(self))],
            )
            return

        if path == "/api/logout":
            revoke_session(get_session_token(self))
            self._send(
                200,
                json.dumps({"ok": True}).encode(),
                "application/json",
                cookies=[clear_session_cookie_header(secure=request_is_https(self))],
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
                candidate = payload["api_key"].strip()
                stored_hash = secrets.get("password_hash") or ""
                env_pw = os.environ.get("AGENT_WEB_PASSWORD", "").strip()
                if (stored_hash and verify_password(candidate, stored_hash)) or (env_pw and candidate == env_pw):
                    self._send(
                        400,
                        json.dumps(
                            {"error": "这是网页登录密码，不是 Cursor API key。到 Dashboard 复制 key 后再保存。"},
                            ensure_ascii=False,
                        ).encode(),
                        "application/json",
                    )
                    return
                secrets["cursor_api_key"] = candidate
                save_secrets(secrets)
            if payload.get("workspace"):
                state["workspace"] = payload["workspace"].strip()
            if payload.get("mode") in ("agent", "ask", "plan"):
                state["mode"] = payload["mode"]
            if payload.get("model"):
                state["model"] = payload["model"].strip()
            if payload.get("speed") in ("default", "fast"):
                state["speed"] = payload["speed"]
            if "show_thinking" in payload:
                state["show_thinking"] = bool(payload.get("show_thinking"))
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
                        "show_thinking": bool(state.get("show_thinking")),
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
            if not chat_exists(state["workspace"], chat_id):
                self._send(404, b'{"error":"session not found"}', "application/json")
                return
            state["chat_id"] = chat_id
            save_state(state)
            hist = load_history(state["workspace"], chat_id, limit=40)
            source = chat_source(state["workspace"], chat_id)
            self._send(
                200,
                json.dumps(
                    {"ok": True, "chat_id": chat_id, "source": source, **hist},
                    ensure_ascii=False,
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
                payload.get("chat_id"),
            )
            return
        if path == "/api/stop":
            state = load_state()
            chat_id = (payload.get("chat_id") or state.get("chat_id") or "").strip()
            stopped = request_stop(chat_id) if chat_id else False
            self._send(
                200,
                json.dumps(
                    {
                        "ok": True,
                        "stopped": stopped,
                        "job": get_job_status(chat_id),
                        "jobs": list_active_jobs(),
                    },
                    ensure_ascii=False,
                ).encode(),
                "application/json",
            )
            return
        if path == "/api/queue/cancel":
            chat_id = (payload.get("chat_id") or "").strip()
            try:
                turn_id = int(payload.get("turn_id"))
            except (TypeError, ValueError):
                self._send(400, b'{"error":"invalid turn_id"}', "application/json")
                return
            ok = cancel_turn(chat_id, turn_id) if chat_id else False
            self._send(
                200,
                json.dumps(
                    {"ok": ok, "job": get_job_status(chat_id), "jobs": list_active_jobs()},
                    ensure_ascii=False,
                ).encode(),
                "application/json",
            )
            return
        if path == "/api/queue/edit":
            chat_id = (payload.get("chat_id") or "").strip()
            text = (payload.get("text") or "").strip()
            try:
                turn_id = int(payload.get("turn_id"))
            except (TypeError, ValueError):
                self._send(400, b'{"error":"invalid turn_id"}', "application/json")
                return
            ok = edit_turn(chat_id, turn_id, text) if chat_id else False
            self._send(
                200,
                json.dumps({"ok": ok, "job": get_job_status(chat_id)}, ensure_ascii=False).encode(),
                "application/json",
            )
            return
        if path == "/api/queue/bump":
            chat_id = (payload.get("chat_id") or "").strip()
            try:
                turn_id = int(payload.get("turn_id"))
            except (TypeError, ValueError):
                self._send(400, b'{"error":"invalid turn_id"}', "application/json")
                return
            ok = promote_turn(chat_id, turn_id) if chat_id else False
            self._send(
                200,
                json.dumps(
                    {"ok": ok, "job": get_job_status(chat_id), "jobs": list_active_jobs()},
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
        chat_id_hint: str | None = None,
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
        hint = (chat_id_hint or "").strip()
        if hint and CHAT_ID_RE.match(hint):
            state["chat_id"] = hint
        save_state(state)
        forked_from = None
        existing = state.get("chat_id")
        if existing and not chat_store_path(state["workspace"], existing).exists():
            forked_from = existing
            state["chat_id"] = None
            save_state(state)
        try:
            chat_id = ensure_chat_id(state)
        except Exception as exc:
            self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json")
            return

        turn = begin_turn(chat_id, text)
        if turn is None:
            self._send(
                429,
                json.dumps(
                    {
                        "error": "该会话排队已满，等当前任务结束再发",
                        "job": get_job_status(chat_id),
                    },
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
            emit({"type": "job", **get_job_status(chat_id), "turn_id": turn.id})

        def emit_all(obj: dict) -> None:
            if obj.get("type") == "tool":
                update_job(chat_id, last_tool=obj.get("text"), last_event="tool")
                emit(obj)
                emit_job()
            elif obj.get("type") == "text":
                update_job(chat_id, last_event="回复中")
                emit(obj)
            elif obj.get("type") == "thinking":
                update_job(chat_id, last_event="思考中")
                emit(obj)
            else:
                emit(obj)

        emit(
            {
                "type": "chat",
                "chat_id": chat_id,
                "source": "cli",
                "forked_from": forked_from,
                "turn_id": turn.id,
            }
        )
        emit(
            {
                "type": "turn",
                "turn_id": turn.id,
                "chat_id": chat_id,
                "prompt": text,
                "state": "queued",
            }
        )
        emit_job()
        saw_done = False
        try:
            if not wait_turn(chat_id, turn, emit):
                state_name = "cancelled" if turn.cancelled and not is_stop_requested(chat_id) else "stopped"
                emit({"type": "job", "state": state_name, "chat_id": chat_id, "turn_id": turn.id})
                emit({"type": "turn", "turn_id": turn.id, "state": state_name})
                emit({"type": "done"})
                return
            prompt = (turn.prompt or text).strip() or text
            args = build_agent_args(state, chat_id, prompt)
            start_job(chat_id, prompt, turn)
            emit({"type": "turn", "turn_id": turn.id, "state": "running", "prompt": prompt})
            emit_job()
            saw_done = stream_chat_with_retries(args, emit_all, emit_job, chat_id)
            if is_stop_requested(chat_id):
                finish_job(chat_id, "stopped")
            elif saw_done:
                finish_job(chat_id, "success")
            else:
                finish_job(chat_id, "error", "任务失败或已中断")
                log_agent(f"job error chat={chat_id} turn={turn.id} 任务失败或已中断")
        except Exception as exc:
            if is_stop_requested(chat_id):
                finish_job(chat_id, "stopped")
            else:
                finish_job(chat_id, "error", str(exc))
                log_agent(f"job exception chat={chat_id}: {exc}")
                emit_all({"type": "error", "text": str(exc)})
        finally:
            end_turn(chat_id, turn)
            emit_job()
            if not saw_done:
                emit({"type": "done"})
