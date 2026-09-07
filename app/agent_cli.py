from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time

from .config import (
    AGENT_BIN,
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


def agent_argv(args: list[str]) -> list[str]:
    binary = str(AGENT_BIN)
    if os.name == "nt" and binary.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", binary, *args]
    return [binary, *args]


def _subprocess_kwargs() -> dict:
    kwargs: dict = {
        "encoding": "utf-8",
        "errors": "replace",
        "env": agent_env(),
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return kwargs


def agent_env() -> dict:
    env = os.environ.copy()
    key = load_secrets().get("cursor_api_key") or os.environ.get("CURSOR_API_KEY", "")
    if key:
        env["CURSOR_API_KEY"] = key
    env["NO_OPEN_BROWSER"] = "1"
    env["CI"] = "1"
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


def parse_stream_line(line: str) -> list[dict]:
    line = line.strip()
    if not line:
        return []
    if line.startswith(("RetriableError:", "Error:", "error:")):
        msg = line
        if "resource_exhausted" in line:
            msg = "Cursor 服务端繁忙（resource_exhausted），正在自动重试…"
        return [{"type": "error", "text": msg, "retriable": "resource_exhausted" in line}]
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return [{"type": "text", "text": line + "\n"}]
    events = []
    typ = obj.get("type")
    subtype = obj.get("subtype")
    if typ == "assistant":
        content = obj.get("message", {}).get("content") or []
        for block in content:
            if isinstance(block, dict) and block.get("text"):
                events.append({"type": "text", "text": block["text"]})
    elif typ == "tool_call":
        tool = obj.get("tool_call") or {}
        name = next(iter(tool.keys()), "tool") if isinstance(tool, dict) else "tool"
        events.append({"type": "tool", "text": f"{subtype or 'call'}: {name}"})
    elif typ == "result":
        if obj.get("is_error"):
            result = obj.get("result") or obj.get("message") or "agent failed"
            events.append({"type": "error", "text": str(result)})
        elif isinstance(obj.get("result"), str) and obj["result"].strip():
            events.append({"type": "text", "text": obj["result"], "from_result": True})
        events.append({"type": "done"})
    elif typ == "error":
        events.append({"type": "error", "text": str(obj.get("message") or obj)})
    return events


def run_agent_stream(args: list[str], emit) -> tuple[bool, bool]:
    """Run agent once. Returns (success, retriable)."""
    if is_stop_requested():
        return False, False
    proc = subprocess.Popen(
        agent_argv(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        **_subprocess_kwargs(),
    )
    set_job_proc(proc)
    assert proc.stdout is not None
    saw_done = False
    saw_text = False
    retriable = False
    try:
        for line in proc.stdout:
            if is_stop_requested():
                break
            for ev in parse_stream_line(line):
                if ev.get("type") == "done":
                    saw_done = True
                if ev.get("type") == "text":
                    if ev.get("from_result") and saw_text:
                        continue
                    saw_text = True
                if ev.get("retriable"):
                    retriable = True
                emit(ev)
        if is_stop_requested():
            return False, False
        proc.wait(timeout=15)
    except Exception as exc:
        if is_stop_requested():
            return False, False
        emit({"type": "error", "text": str(exc)})
        try:
            proc.kill()
        except OSError:
            pass
        return False, False
    finally:
        set_job_proc(None)
    success = proc.returncode == 0 and saw_done and saw_text
    if proc.returncode != 0 and not saw_done:
        if not retriable:
            emit({"type": "error", "text": f"agent exit {proc.returncode}"})
        else:
            retriable = True
    return success, retriable


def stream_chat_with_retries(args: list[str], emit_all, emit_job) -> bool:
    for attempt in range(1, MAX_AGENT_RETRIES + 1):
        if is_stop_requested():
            return False
        if attempt > 1:
            update_job(last_event=f"重试 {attempt}/{MAX_AGENT_RETRIES}")
            emit_all({"type": "tool", "text": f"重试 {attempt}/{MAX_AGENT_RETRIES}…"})
            emit_job()
            for _ in range(int(min(2 * attempt, 8) * 10)):
                if is_stop_requested():
                    return False
                time.sleep(0.1)
        success, retriable = run_agent_stream(args, emit_all)
        if is_stop_requested():
            return False
        if success:
            return True
        if not retriable or attempt == MAX_AGENT_RETRIES:
            if attempt == MAX_AGENT_RETRIES and not is_stop_requested():
                emit_all(
                    {
                        "type": "error",
                        "text": "多次重试仍失败。可换 ask 模式试，或稍后再发。",
                    }
                )
            break
    return False
