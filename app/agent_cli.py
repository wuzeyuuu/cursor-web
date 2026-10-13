from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from collections import deque

from .config import (
    AGENT_BIN,
    AGENT_LOG_PATH,
    CHAT_ID_RE,
    DEFAULT_MODEL,
    MAX_AGENT_RETRIES,
    MODEL_LINE_RE,
    MODELS_CACHE_TTL,
)
from .jobs import is_stop_requested, set_job_proc, update_job
from .store import load_secrets, save_state

_models_lock = threading.Lock()
_models_cache: dict = {"ts": 0.0, "items": []}
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_LOCAL_PROXY_PORTS = (7897, 7890, 10809, 20171, 10808, 1080)
_log_lock = threading.Lock()
_KNOWN_STREAM_TYPES = {
    "assistant",
    "tool_call",
    "result",
    "error",
    "thinking",
    "system",
    "user",
    "init",
}


def log_agent(msg: str) -> None:
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg.rstrip() + "\n"
    print(line, end="", flush=True)
    try:
        AGENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _log_lock:
            with AGENT_LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(line)
    except OSError:
        pass


_AGENT_VERSION_RE = re.compile(
    r"^\d{4}\.\d{1,2}\.\d{1,2}(?:-\d{2}-\d{2}-\d{2})?-[a-f0-9]+$"
)


def _version_sort_key(name: str) -> tuple[int, str]:
    date_part = name.split("-", 1)[0]
    year, month, day = date_part.split(".")
    return (int(year + month.zfill(2) + day.zfill(2)), name)


def _node_launch(binary: Path) -> list[str] | None:
    """Run node + index.js directly. cmd.exe /c cuts the prompt at the first newline."""
    script_dir = binary.parent
    local_node = script_dir / "node.exe"
    local_index = script_dir / "index.js"
    if local_node.is_file() and local_index.is_file():
        return [str(local_node), str(local_index)]
    versions = script_dir / "versions"
    if not versions.is_dir():
        return None
    found: list[tuple[tuple[int, str], Path]] = []
    try:
        entries = list(versions.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.is_dir() or not _AGENT_VERSION_RE.match(entry.name):
            continue
        node = entry / "node.exe"
        index = entry / "index.js"
        if node.is_file() and index.is_file():
            found.append((_version_sort_key(entry.name), entry))
    if not found:
        return None
    entry = max(found, key=lambda item: item[0])[1]
    return [str(entry / "node.exe"), str(entry / "index.js")]


def agent_argv(args: list[str]) -> list[str]:
    binary = Path(AGENT_BIN)
    if binary.suffix.lower() in (".cmd", ".bat"):
        launch = _node_launch(binary)
        if launch:
            return [*launch, *args]
        return ["cmd.exe", "/c", str(binary), *args]
    return [str(binary), *args]


def _subprocess_kwargs() -> dict:
    kwargs: dict = {
        "encoding": "utf-8",
        "errors": "replace",
        "env": agent_env(),
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return kwargs


def _tcp_open(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _normalize_proxy(raw: str) -> str | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    if "=" in raw:
        picked = ""
        for item in raw.split(";"):
            if "=" not in item:
                continue
            kind, _, value = item.partition("=")
            kind = kind.strip().lower()
            value = value.strip()
            if kind in ("https", "http") and value:
                picked = value
                break
            if kind.startswith("socks") and value and not picked:
                picked = value if value.lower().startswith("socks") else f"socks5://{value}"
        raw = picked
    if not raw:
        return None
    if raw.lower().startswith(("http://", "https://", "socks5://", "socks://")):
        return raw
    return f"http://{raw}"


def _windows_system_proxy() -> str | None:
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if not enable:
                return None
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except OSError:
        return None
    return _normalize_proxy(str(server))


def _proxy_reachable(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return _tcp_open(host, port)


def resolve_https_proxy() -> str | None:
    for name in (
        "AGENT_HTTPS_PROXY",
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        raw = os.environ.get(name, "").strip()
        if raw:
            proxy = _normalize_proxy(raw)
            if proxy:
                return proxy
    system = _windows_system_proxy()
    if system and _proxy_reachable(system):
        return system
    for port in _LOCAL_PROXY_PORTS:
        if _tcp_open("127.0.0.1", port):
            return f"http://127.0.0.1:{port}"
    return None


def agent_env() -> dict:
    env = os.environ.copy()
    key = load_secrets().get("cursor_api_key") or os.environ.get("CURSOR_API_KEY", "")
    if key:
        env["CURSOR_API_KEY"] = key
    env["NO_OPEN_BROWSER"] = "1"
    env["CI"] = "1"
    proxy = resolve_https_proxy()
    if proxy:
        env["HTTPS_PROXY"] = proxy
        env["HTTP_PROXY"] = proxy
        env["https_proxy"] = proxy
        env["http_proxy"] = proxy
        env["NODE_USE_ENV_PROXY"] = "1"
        no_proxy = env.get("NO_PROXY") or env.get("no_proxy") or "localhost,127.0.0.1"
        env["NO_PROXY"] = no_proxy
        env["no_proxy"] = no_proxy
    return env


def run_agent(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        agent_argv(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        **_subprocess_kwargs(),
    )


def create_chat_id(workspace: str) -> str:
    """create-chat prints the UUID then keeps running; grab the line and stop it."""
    proc = subprocess.Popen(
        agent_argv(["create-chat", "--workspace", workspace]),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **_subprocess_kwargs(),
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        chat_id = line.strip()
        if not CHAT_ID_RE.match(chat_id):
            err = (proc.stderr.read() if proc.stderr else "").strip()
            raise RuntimeError(err or f"create-chat returned unexpected output: {line!r}")
        return chat_id
    finally:
        proc.kill()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def parse_models_output(text: str) -> list[dict]:
    items: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("available"):
            continue
        match = MODEL_LINE_RE.match(line)
        if not match:
            continue
        slug, label = match.group(1), match.group(2).strip()
        current = "(current)" in label
        default = "(default)" in label
        label = re.sub(r"\s*\((current|default)\)\s*$", "", label).strip()
        items.append(
            {"id": slug, "label": label, "current": current, "default": default}
        )
    return items


def list_available_models(force: bool = False) -> list[dict]:
    now = time.time()
    with _models_lock:
        if (
            not force
            and _models_cache["items"]
            and now - _models_cache["ts"] < MODELS_CACHE_TTL
        ):
            return list(_models_cache["items"])
    proc = run_agent(["models"], timeout=30)
    if proc.returncode != 0:
        with _models_lock:
            if _models_cache["items"]:
                return list(_models_cache["items"])
        return [{"id": DEFAULT_MODEL, "label": DEFAULT_MODEL}]
    items = parse_models_output(proc.stdout or "")
    if not items:
        items = [{"id": DEFAULT_MODEL, "label": DEFAULT_MODEL}]
    with _models_lock:
        _models_cache["items"] = items
        _models_cache["ts"] = now
    return list(items)


def resolve_model(model: str, speed: str) -> str:
    model = (model or DEFAULT_MODEL).strip()
    if speed != "fast" or model.endswith("-fast"):
        return model
    fast_id = f"{model}-fast"
    available = {item["id"] for item in list_available_models()}
    return fast_id if fast_id in available else model


def ensure_chat_id(state: dict) -> str:
    if state.get("chat_id"):
        return state["chat_id"]
    chat_id = create_chat_id(state["workspace"])
    state["chat_id"] = chat_id
    save_state(state)
    return chat_id


def build_agent_args(state: dict, chat_id: str, text: str) -> list[str]:
    model = resolve_model(state.get("model", DEFAULT_MODEL), state.get("speed", "default"))
    args = [
        "-p",
        "--trust",
        "--approve-mcps",
        "--model",
        model,
        "--output-format",
        "stream-json",
        "--workspace",
        state["workspace"],
        "--resume",
        chat_id,
    ]
    mode = state.get("mode", "agent")
    if mode == "ask":
        args.extend(["--mode", "ask"])
    elif mode == "plan":
        args.extend(["--mode", "plan"])
    else:
        args.append("--force")
    args.append(text)
    return args


def is_context_summary_text(text: str) -> bool:
    head = (text or "").lstrip()[:800]
    if not head:
        return False
    return any(
        marker in head
        for marker in (
            "<conversation_summary>",
            "<summary_content>",
            "Your conversation was summarized",
            "<system_notification>",
        )
    )


def _thinking_text(obj: dict) -> str:
    text = obj.get("text")
    if isinstance(text, str) and text:
        return text
    message = obj.get("message")
    if isinstance(message, dict):
        content = message.get("content") or []
        parts = []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    parts.append(str(block.get("text") or block.get("thinking") or ""))
        elif isinstance(content, str):
            parts.append(content)
        return "".join(parts)
    return ""


def parse_stream_line(line: str) -> list[dict]:
    line = _ANSI_RE.sub("", line).strip()
    if not line:
        return []
    if line.startswith(("RetriableError:", "Error:", "error:")) or "Failed to reach the Cursor API" in line or "API key is invalid" in line:
        msg = _ANSI_RE.sub("", line).strip()
        if "resource_exhausted" in line:
            msg = "Cursor 服务端繁忙（resource_exhausted），正在自动重试…"
        elif "API key is invalid" in line:
            msg = "CURSOR_API_KEY 无效。到网页设置里贴新的 key，或去 Cursor Dashboard 重新生成。"
        elif "Failed to reach the Cursor API" in line:
            msg = "连不上 Cursor API。先确认 Clash 7897 开着，或检查代理。"
        return [{"type": "error", "text": msg, "retriable": "resource_exhausted" in line or "Failed to reach" in line}]
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        log_agent("raw " + line[:400])
        return []
    events = []
    typ = obj.get("type")
    subtype = obj.get("subtype")
    if typ == "thinking":
        if subtype != "completed":
            text = _thinking_text(obj)
            if text:
                events.append({"type": "thinking", "text": text})
    elif typ == "assistant":
        if obj.get("model_call_id"):
            return []
        content = obj.get("message", {}).get("content") or []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type") or "")
            if btype in ("thinking", "reasoning"):
                t = block.get("text") or block.get("thinking") or ""
                if t:
                    events.append({"type": "thinking", "text": t})
            elif block.get("text") and not is_context_summary_text(block["text"]):
                events.append({"type": "text", "text": block["text"]})
    elif typ == "tool_call":
        tool = obj.get("tool_call") or {}
        name = next(iter(tool.keys()), "tool") if isinstance(tool, dict) else "tool"
        events.append({"type": "tool", "text": f"{subtype or 'call'}: {name}"})
    elif typ == "result":
        if obj.get("is_error"):
            result = obj.get("result") or obj.get("message") or "agent failed"
            events.append({"type": "error", "text": str(result)})
        elif isinstance(obj.get("result"), str) and obj["result"].strip() and not is_context_summary_text(obj["result"]):
            events.append({"type": "text", "text": obj["result"], "from_result": True})
        events.append({"type": "done"})
    elif typ == "error":
        msg = obj.get("message") or obj.get("error") or obj.get("text")
        if isinstance(msg, dict):
            msg = msg.get("message") or msg.get("error") or ""
        if msg:
            events.append({"type": "error", "text": str(msg)})
    elif typ not in _KNOWN_STREAM_TYPES:
        log_agent(f"unhandled stream type={typ!r} subtype={subtype!r}")
    return events


def run_agent_stream(args: list[str], emit, chat_id: str | None = None) -> tuple[bool, bool]:
    """Run agent once. Returns (success, retriable)."""
    if is_stop_requested(chat_id):
        return False, False
    proc = subprocess.Popen(
        agent_argv(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        **_subprocess_kwargs(),
    )
    set_job_proc(chat_id, proc)
    assert proc.stdout is not None
    saw_done = False
    saw_text = False
    saw_error = False
    retriable = False
    tail = deque(maxlen=20)
    emitted_parts: list[str] = []
    prompt = args[-1] if args else ""
    log_agent(
        f"start chat={chat_id or '-'} model={next((args[i + 1] for i, a in enumerate(args) if a == '--model'), '-')} "
        f"prompt={prompt[:80]!r}"
    )
    try:
        for line in proc.stdout:
            if is_stop_requested(chat_id):
                break
            tail.append(line.rstrip()[:500])
            for ev in parse_stream_line(line):
                if ev.get("type") == "done":
                    saw_done = True
                if ev.get("type") == "text":
                    text = ev.get("text") or ""
                    if ev.get("from_result"):
                        joined = "".join(emitted_parts)
                        if saw_text or (joined and text.replace("\n", "") == joined.replace("\n", "")):
                            continue
                    elif text in emitted_parts:
                        continue
                    else:
                        emitted_parts.append(text)
                    saw_text = True
                if ev.get("type") == "error" and not ev.get("retriable"):
                    saw_error = True
                if ev.get("retriable"):
                    retriable = True
                emit(ev)
        if is_stop_requested(chat_id):
            log_agent(f"stop chat={chat_id or '-'}")
            return False, False
        proc.wait(timeout=15)
    except Exception as exc:
        if is_stop_requested(chat_id):
            return False, False
        log_agent(f"exception chat={chat_id or '-'}: {exc}")
        emit({"type": "error", "text": str(exc)})
        try:
            proc.kill()
        except OSError:
            pass
        return False, False
    finally:
        set_job_proc(chat_id, None)
    success = proc.returncode == 0 and saw_done and not saw_error
    log_agent(
        f"end chat={chat_id or '-'} code={proc.returncode} done={saw_done} text={saw_text} "
        f"err={saw_error} retry={retriable} success={success}"
    )
    if not success and tail:
        log_agent("tail " + " | ".join(tail)[-800:])
    if proc.returncode != 0 and not saw_done:
        if not retriable:
            emit({"type": "error", "text": f"agent exit {proc.returncode}"})
        else:
            retriable = True
    elif proc.returncode == 0 and not saw_done and not saw_text:
        emit({"type": "error", "text": "agent 没有返回 result 事件（可能中途断流）"})
        log_agent(f"missing result event chat={chat_id or '-'}")
    return success, retriable


def stream_chat_with_retries(
    args: list[str], emit_all, emit_job, chat_id: str | None = None
) -> bool:
    for attempt in range(1, MAX_AGENT_RETRIES + 1):
        if is_stop_requested(chat_id):
            return False
        if attempt > 1:
            log_agent(f"retry {attempt}/{MAX_AGENT_RETRIES} chat={chat_id or '-'}")
            update_job(chat_id, last_event=f"重试 {attempt}/{MAX_AGENT_RETRIES}")
            emit_all({"type": "tool", "text": f"重试 {attempt}/{MAX_AGENT_RETRIES}…"})
            emit_job()
            for _ in range(int(min(2 * attempt, 8) * 10)):
                if is_stop_requested(chat_id):
                    return False
                time.sleep(0.1)
        success, retriable = run_agent_stream(args, emit_all, chat_id)
        if is_stop_requested(chat_id):
            return False
        if success:
            return True
        if not retriable or attempt == MAX_AGENT_RETRIES:
            if attempt == MAX_AGENT_RETRIES and not is_stop_requested(chat_id):
                emit_all(
                    {
                        "type": "error",
                        "text": "多次重试仍失败。可换 ask 模式试，或稍后再发。",
                    }
                )
            break
    return False
