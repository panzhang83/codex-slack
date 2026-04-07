import threading
import time
from dataclasses import dataclass
from typing import Optional

from codex_threads import (
    CodexAppServerConfig,
    interrupt_turn,
    read_field,
    read_thread_response,
    steer_turn,
)


@dataclass(frozen=True)
class ActiveTurnInfo:
    thread_id: str
    turn_id: str
    status: str


@dataclass(frozen=True)
class ActiveTurnRecord:
    thread_key: str
    session_id: str
    turn_id: str
    updated_at: int


class ActiveTurnRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._by_thread_key = {}

    def set(self, thread_key, session_id, turn_id):
        with self._lock:
            self._by_thread_key[thread_key] = ActiveTurnRecord(
                thread_key=thread_key,
                session_id=session_id,
                turn_id=turn_id,
                updated_at=int(time.time()),
            )

    def get_for_thread(self, thread_key) -> Optional[ActiveTurnRecord]:
        with self._lock:
            return self._by_thread_key.get(thread_key)

    def clear_for_thread(self, thread_key):
        with self._lock:
            self._by_thread_key.pop(thread_key, None)

    def clear_for_session(self, session_id):
        with self._lock:
            stale_keys = [key for key, record in self._by_thread_key.items() if record.session_id == session_id]
            for key in stale_keys:
                self._by_thread_key.pop(key, None)


def _normalize_turn_status(value):
    if value is None:
        return ""
    raw = getattr(value, "value", value)
    return str(raw or "").strip()


def find_active_turn(thread_read_response):
    thread = read_field(thread_read_response, "thread", thread_read_response)
    thread_id = read_field(thread, "id", "") or ""
    thread_status = read_field(read_field(thread, "status", {}) or {}, "type", "")
    if str(thread_status or "").strip() != "active":
        return None

    last_active = None
    for turn in read_field(thread, "turns", []) or []:
        turn_status = _normalize_turn_status(read_field(turn, "status"))
        if turn_status != "inProgress":
            continue
        turn_id = read_field(turn, "id", "") or ""
        if turn_id:
            last_active = ActiveTurnInfo(thread_id=thread_id, turn_id=turn_id, status=turn_status)
    return last_active


def get_active_turn(config: CodexAppServerConfig, session_id):
    return find_active_turn(read_thread_response(config, session_id))


def interrupt_active_turn(config: CodexAppServerConfig, session_id):
    active_turn = get_active_turn(config, session_id)
    if not active_turn:
        raise RuntimeError("当前 session 没有可中断的活跃 turn。")
    interrupt_turn(config, active_turn.thread_id, active_turn.turn_id)
    return active_turn


def steer_active_turn(config: CodexAppServerConfig, session_id, text):
    active_turn = get_active_turn(config, session_id)
    if not active_turn:
        raise RuntimeError("当前 session 没有可追加输入的活跃 turn。")
    steer_turn(
        config,
        thread_id=active_turn.thread_id,
        expected_turn_id=active_turn.turn_id,
        input_items=[{"type": "text", "text": text}],
    )
    return active_turn
