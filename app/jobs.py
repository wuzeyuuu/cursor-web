from __future__ import annotations

import os
import subprocess
import threading
import time

_job_lock = threading.Lock()
_current_job: dict | None = None
_current_proc: subprocess.Popen | None = None
_stop_event = threading.Event()


def get_job_status() -> dict:
    with _job_lock:
        if not _current_job:
            return {"state": "idle"}
        job = dict(_current_job)
    if job.get("state") == "running" and job.get("started_at"):
        job["elapsed_s"] = round(time.time() - job["started_at"], 1)
    elif job.get("started_at") and job.get("finished_at"):
        job["elapsed_s"] = round(job["finished_at"] - job["started_at"], 1)
    return job


def start_job(chat_id: str, prompt: str) -> bool:
    global _current_job
    with _job_lock:
        if _current_job and _current_job.get("state") == "running":
            return False
        _stop_event.clear()
        _current_job = {
            "state": "running",
            "chat_id": chat_id,
            "prompt": prompt[:80] + ("…" if len(prompt) > 80 else ""),
            "started_at": time.time(),
            "last_tool": None,
            "last_event": "starting",
        }
        return True


def update_job(**kwargs) -> None:
    with _job_lock:
        if _current_job:
            _current_job.update(kwargs)


def set_job_proc(proc: subprocess.Popen | None) -> None:
    global _current_proc
    with _job_lock:
        _current_proc = proc


def is_stop_requested() -> bool:
    return _stop_event.is_set()


def _kill_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()


def request_stop() -> bool:
    with _job_lock:
        running = bool(_current_job and _current_job.get("state") == "running")
        proc = _current_proc
    if not running:
        return False
    _stop_event.set()
    if proc is not None:
        _kill_process(proc)
    return True


def finish_job(state: str, error: str | None = None) -> None:
    global _current_proc
    with _job_lock:
        _current_proc = None
        if not _current_job:
            return
        _current_job["state"] = state
        _current_job["finished_at"] = time.time()
        _current_job["last_event"] = state
        if error:
            _current_job["error"] = error
        if _current_job.get("started_at"):
            _current_job["elapsed_s"] = round(
                _current_job["finished_at"] - _current_job["started_at"], 1
            )
