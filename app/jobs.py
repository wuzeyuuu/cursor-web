from __future__ import annotations

import os
import subprocess
import threading
import time

MAX_QUEUE_PER_CHAT = 8

_global = threading.Lock()
_lanes: dict[str, "_Lane"] = {}
_turn_seq = 0


class _Turn:
    __slots__ = ("id", "prompt", "ready", "cancelled", "started")

    def __init__(self, turn_id: int, prompt: str) -> None:
        self.id = turn_id
        self.prompt = prompt
        self.ready = threading.Event()
        self.cancelled = False
        self.started = False


class _Lane:
    def __init__(self, chat_id: str) -> None:
        self.chat_id = chat_id
        self.lock = threading.Lock()
        self.turns: list[_Turn] = []
        self.proc: subprocess.Popen | None = None
        self.job: dict | None = None
        self.stop = threading.Event()


def _clip_prompt(prompt: str) -> str:
    prompt = prompt.replace("\n", " ").strip()
    return prompt[:80] + ("…" if len(prompt) > 80 else "")


def _get_lane(chat_id: str) -> _Lane:
    with _global:
        lane = _lanes.get(chat_id)
        if lane is None:
            lane = _Lane(chat_id)
            _lanes[chat_id] = lane
        return lane


def _elapsed(job: dict) -> float | None:
    started = job.get("started_at")
    if not started:
        return None
    ended = job.get("finished_at") or time.time()
    return round(ended - started, 1)


def _status_locked(lane: _Lane, turn: _Turn | None = None) -> dict:
    waiting = [t for t in lane.turns if not t.started and not t.cancelled]
    running = next((t for t in lane.turns if t.started and not t.cancelled), None)
    job = dict(lane.job) if lane.job else {"state": "idle", "chat_id": lane.chat_id}
    job["chat_id"] = lane.chat_id
    job["queue"] = len(waiting)
    if turn is not None and not turn.started and not turn.cancelled:
        pos = 1
        for item in lane.turns:
            if item is turn:
                break
            if not item.cancelled:
                pos += 1
        job = dict(job)
        job["state"] = "queued"
        job["queue_pos"] = pos
        job["prompt"] = _clip_prompt(turn.prompt)
        return job
    if running:
        job["state"] = "running"
        if running.prompt:
            job.setdefault("prompt", _clip_prompt(running.prompt))
    elif waiting:
        job["state"] = "queued"
        job["prompt"] = _clip_prompt(waiting[0].prompt)
    if job.get("state") == "running":
        job["elapsed_s"] = _elapsed(job) if job.get("started_at") else 0
    elif job.get("started_at") and job.get("finished_at"):
        job["elapsed_s"] = _elapsed(job)
    return job


def get_job_status(chat_id: str | None = None) -> dict:
    if not chat_id:
        active = list_active_jobs()
        return active[0] if active else {"state": "idle"}
    lane = _get_lane(chat_id)
    with lane.lock:
        return _status_locked(lane)


def list_active_jobs() -> list[dict]:
    with _global:
        ids = list(_lanes.keys())
    out: list[dict] = []
    for chat_id in ids:
        job = get_job_status(chat_id)
        if job.get("state") in ("running", "queued") or job.get("queue"):
            out.append(job)
    out.sort(key=lambda item: item.get("started_at") or 0, reverse=True)
    return out


def begin_turn(chat_id: str, prompt: str) -> _Turn | None:
    global _turn_seq
    lane = _get_lane(chat_id)
    with lane.lock:
        live = [t for t in lane.turns if not t.cancelled]
        if len(live) >= MAX_QUEUE_PER_CHAT:
            return None
        _turn_seq += 1
        turn = _Turn(_turn_seq, prompt)
        lane.turns.append(turn)
        head = next((t for t in lane.turns if not t.cancelled), None)
        if head is turn:
            turn.ready.set()
        return turn


def wait_turn(chat_id: str, turn: _Turn, emit) -> bool:
    lane = _get_lane(chat_id)
    while not turn.ready.wait(timeout=0.25):
        if turn.cancelled:
            return False
        with lane.lock:
            status = _status_locked(lane, turn)
        status["turn_id"] = turn.id
        emit({"type": "job", **status})
    return not turn.cancelled


def find_turn(chat_id: str, turn_id: int) -> _Turn | None:
    lane = _get_lane(chat_id)
    with lane.lock:
        for turn in lane.turns:
            if turn.id == int(turn_id):
                return turn
    return None


def cancel_turn(chat_id: str, turn_id: int) -> bool:
    lane = _get_lane(chat_id)
    with lane.lock:
        for turn in lane.turns:
            if turn.id != int(turn_id):
                continue
            if turn.started or turn.cancelled:
                return False
            turn.cancelled = True
            turn.ready.set()
            return True
    return False


def edit_turn(chat_id: str, turn_id: int, prompt: str) -> bool:
    prompt = (prompt or "").strip()
    if not prompt:
        return False
    lane = _get_lane(chat_id)
    with lane.lock:
        for turn in lane.turns:
            if turn.id != int(turn_id):
                continue
            if turn.started or turn.cancelled:
                return False
            turn.prompt = prompt
            return True
    return False


def promote_turn(chat_id: str, turn_id: int) -> bool:
    """Put this queued turn next; interrupt the running job so it starts immediately."""
    lane = _get_lane(chat_id)
    proc = None
    with lane.lock:
        turn = next((t for t in lane.turns if t.id == int(turn_id)), None)
        if turn is None or turn.started or turn.cancelled:
            return False
        lane.turns.remove(turn)
        insert_at = 0
        for i, item in enumerate(lane.turns):
            if item.started and not item.cancelled:
                insert_at = i + 1
                break
        lane.turns.insert(insert_at, turn)
        running = next((t for t in lane.turns if t.started and not t.cancelled), None)
        if running is None:
            turn.ready.set()
        else:
            lane.stop.set()
            proc = lane.proc
    if proc is not None:
        _kill_process(proc)
    return True


def start_job(chat_id: str, prompt: str, turn: _Turn | None = None) -> dict:
    lane = _get_lane(chat_id)
    with lane.lock:
        if turn is not None:
            turn.started = True
        lane.stop.clear()
        waiting = [t for t in lane.turns if not t.started and not t.cancelled]
        lane.job = {
            "state": "running",
            "chat_id": chat_id,
            "prompt": _clip_prompt(prompt),
            "started_at": time.time(),
            "last_tool": None,
            "last_event": "starting",
            "queue": len(waiting),
        }
        return _status_locked(lane, None)


def update_job(chat_id: str | None = None, **kwargs) -> None:
    if not chat_id:
        return
    lane = _get_lane(chat_id)
    with lane.lock:
        if lane.job:
            lane.job.update(kwargs)


def set_job_proc(chat_id: str | None, proc: subprocess.Popen | None) -> None:
    if not chat_id:
        return
    lane = _get_lane(chat_id)
    with lane.lock:
        lane.proc = proc


def is_stop_requested(chat_id: str | None = None) -> bool:
    if not chat_id:
        return False
    return _get_lane(chat_id).stop.is_set()


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


def request_stop(chat_id: str | None = None) -> bool:
    if not chat_id:
        return False
    lane = _get_lane(chat_id)
    with lane.lock:
        lane.stop.set()
        proc = lane.proc
        had = False
        for turn in lane.turns:
            if turn.started and not turn.cancelled:
                had = True
                continue
            if not turn.cancelled:
                turn.cancelled = True
                turn.ready.set()
                had = True
        if lane.job and lane.job.get("state") in ("running", "queued"):
            had = True
    if proc is not None:
        _kill_process(proc)
        return True
    return had


def finish_job(chat_id: str, state: str, error: str | None = None) -> None:
    lane = _get_lane(chat_id)
    with lane.lock:
        lane.proc = None
        if not lane.job:
            lane.job = {"chat_id": chat_id}
        lane.job["state"] = state
        lane.job["finished_at"] = time.time()
        lane.job["last_event"] = state
        if error:
            lane.job["error"] = error
        if lane.job.get("started_at"):
            lane.job["elapsed_s"] = _elapsed(lane.job)
        lane.job["queue"] = len([t for t in lane.turns if not t.started and not t.cancelled])


def end_turn(chat_id: str, turn: _Turn) -> None:
    lane = _get_lane(chat_id)
    with lane.lock:
        if turn in lane.turns:
            lane.turns.remove(turn)
        nxt = next((t for t in lane.turns if not t.cancelled), None)
        if nxt and not nxt.ready.is_set():
            nxt.ready.set()
        elif nxt is None:
            lane.stop.clear()
