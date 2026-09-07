from __future__ import annotations

import os
import threading

from .config import (
    DEFAULT_MODEL,
    DEFAULT_WORKSPACE,
    SECRETS_PATH,
    STATE_PATH,
    workspace_needs_localize,
)
from .jsonio import load_json, save_json

_state_lock = threading.Lock()


def load_state() -> dict:
    with _state_lock:
        data = load_json(STATE_PATH)
        workspace = data.get("workspace") or str(DEFAULT_WORKSPACE)
        changed = False
        if workspace_needs_localize(workspace):
            workspace = str(DEFAULT_WORKSPACE)
            data["chat_id"] = None
            changed = True
        data["workspace"] = workspace
        data.setdefault("chat_id", None)
        data.setdefault("mode", "agent")
        data.setdefault("model", DEFAULT_MODEL)
        data.setdefault("speed", "default")
        if changed:
            save_json(STATE_PATH, data, 0o644)
        return data


def save_state(data: dict) -> None:
    with _state_lock:
        save_json(STATE_PATH, data, 0o644)


def load_secrets() -> dict:
    with _state_lock:
        data = load_json(SECRETS_PATH)
        data.pop("web_token", None)
        if "cursor_api_key" not in data:
            data["cursor_api_key"] = os.environ.get("CURSOR_API_KEY", "")
        if "password_hash" not in data:
            data["password_hash"] = ""
        if "sessions" not in data or not isinstance(data["sessions"], dict):
            data["sessions"] = {}
        save_json(SECRETS_PATH, data)
        return data


def save_secrets(data: dict) -> None:
    with _state_lock:
        data.pop("web_token", None)
        save_json(SECRETS_PATH, data)
