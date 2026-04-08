import atexit
import asyncio
import concurrent.futures
import json
import os
import queue
import re
import shlex
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import traceback
import urllib.error
import uuid
import warnings
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app_runtime import (
    AppServerRuntime,
    RuntimeUserInputQuestion,
    RuntimeUserInputRequest,
)
import codex_threads as thread_views
import session_catalog
import slack_document_inputs
import slack_home
import slack_image_inputs
from codex_app_server_sdk.errors import CodexProtocolError, CodexTimeoutError, CodexTransportError
from codex_app_server_sdk.models import ThreadConfig, TurnOverrides
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError
from turn_control import ActiveTurnRegistry

warnings.filterwarnings(
    "ignore",
    message=r'Field ".*" .* protected namespace "model_".*',
    category=UserWarning,
)

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


def load_env():
    env = dict(os.environ)
    dotenv_path = Path(__file__).with_name(".env")
    if dotenv_path.exists():
        for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env.setdefault(key.strip(), value.strip().strip("'").strip('"'))
    return env


ENV = load_env()
SESSION_STORE_PATH = Path(
    ENV.get("CODEX_SLACK_SESSION_STORE", Path(__file__).with_name(".codex-slack-sessions.json"))
).expanduser()
INSTANCE_LOCK_PATH = Path(
    ENV.get("CODEX_SLACK_INSTANCE_LOCK", Path(__file__).with_name(".codex-slack.pid"))
).expanduser()
SESSION_ID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
THREAD_LOCKS = {}
THREAD_LOCKS_GUARD = threading.Lock()
SESSION_LOCKS = {}
SESSION_LOCKS_GUARD = threading.Lock()
WATCHERS = {}
WATCHERS_GUARD = threading.Lock()
INSTANCE_LOCK_HANDLE = None
SESSION_SELECTION_CACHE = session_catalog.SessionSelectionCache()
ACTIVE_TURN_REGISTRY = ActiveTurnRegistry()
APP_RUNTIME = None
APP_RUNTIME_GUARD = threading.Lock()
SESSION_MODE_OBSERVE = "observe"
SESSION_MODE_CONTROL = "control"
SESSION_ORIGIN_ATTACHED = "attached"
SESSION_ORIGIN_SLACK = "slack"
COLLABORATION_MODE_DEFAULT = "default"
COLLABORATION_MODE_PLAN = "plan"
SUPPORTED_COLLABORATION_MODES = (COLLABORATION_MODE_DEFAULT, COLLABORATION_MODE_PLAN)
THREAD_COLLABORATION_MODE_ACTION = "thread_collaboration_mode_set"
THREAD_COLLABORATION_MODE_PLAN_ACTION = f"{THREAD_COLLABORATION_MODE_ACTION}_plan"
THREAD_COLLABORATION_MODE_DEFAULT_ACTION = f"{THREAD_COLLABORATION_MODE_ACTION}_default"
THREAD_PLAN_ACTION = "thread_plan_execute"
THREAD_PLAN_IMPLEMENT_CLEAN_ACTION = f"{THREAD_PLAN_ACTION}_clean"
THREAD_PLAN_IMPLEMENT_HERE_ACTION = f"{THREAD_PLAN_ACTION}_here"
THREAD_PLAN_KEEP_PLANNING_ACTION = f"{THREAD_PLAN_ACTION}_keep_planning"
REQUEST_USER_INPUT_OPEN_ACTION = "request_user_input_open"
REQUEST_USER_INPUT_CANCEL_ACTION = "request_user_input_cancel"
REQUEST_USER_INPUT_SUBMIT_CALLBACK = "request_user_input_submit"
REQUEST_USER_INPUT_OTHER_VALUE = "__other__"
SUBAGENT_SEND_NEXT_ACTION = "subagent_send_next"
SUBAGENT_SEND_CANCEL_ACTION = "subagent_send_cancel"
SUBAGENT_OBSERVE_ACTION = "subagent_observe"
SUBAGENT_ATTACH_ACTION = "subagent_attach"
DEFAULT_REASONING_EFFORT = "xhigh"
SUPPORTED_REASONING_EFFORTS = ("low", "medium", "high", "xhigh")
DEFAULT_WATCH_POLL_SECONDS = 5
DEFAULT_WATCH_METADATA_FALLBACK_SECONDS = 30
DEFAULT_WATCH_FS_DEBOUNCE_SECONDS = 0.2
DEFAULT_PROGRESS_HEARTBEAT_SECONDS = 300
DEFAULT_PROGRESS_POLL_SECONDS = 15
DEFAULT_PROGRESS_BATCH_SECONDS = 5.0
DEFAULT_PENDING_SUBAGENT_TTL_SECONDS = 600
MAX_APP_SERVER_RETRIES = 2
DEFAULT_APP_SERVER_RESUME_MAX_RETRIES = 2
MAX_WATCH_READ_FAILURES = 2
DEFAULT_APP_SERVER_STDIO_LINE_LIMIT_BYTES = 32 * 1024 * 1024
DEFAULT_IMAGE_ONLY_PROMPT = "请查看我附上的图片，并按我的上下文继续处理。"
DEFAULT_DOCUMENT_ONLY_PROMPT = "请先阅读我附上的文档，并按我的上下文继续处理。"
DEFAULT_IMAGE_AND_DOCUMENT_ONLY_PROMPT = "请查看我附上的图片并阅读我附上的文档，然后按我的上下文继续处理。"
SUPPORTED_DOCUMENT_ATTACHMENT_HINT = "txt/md/json/yaml/csv/pdf/docx/jl/ipynb 等文档类附件"
DEFAULT_PROGRESS_UPDATES_ENABLED = True
DEFAULT_SLACK_STARTUP_RETRY_INITIAL_SECONDS = 2.0
DEFAULT_SLACK_STARTUP_RETRY_MAX_SECONDS = 60.0


def normalize_reasoning_effort(value):
    normalized = str(value or "").strip().lower()
    if normalized in SUPPORTED_REASONING_EFFORTS:
        return normalized
    return None


def normalize_collaboration_mode(value):
    normalized = str(value or "").strip().lower()
    if normalized in SUPPORTED_COLLABORATION_MODES:
        return normalized
    return None


def normalize_progress_updates(value):
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"on", "true", "1", "yes"}:
        return True
    if normalized in {"off", "false", "0", "no"}:
        return False
    return None


def format_progress_updates_value(value):
    normalized = normalize_progress_updates(value)
    if normalized is True:
        return "on"
    if normalized is False:
        return "off"
    return "-"


def format_reasoning_effort_values():
    return "|".join(SUPPORTED_REASONING_EFFORTS)


def normalize_session_cwd(value):
    normalized = str(value or "").strip()
    return normalized or None


def normalize_plan_text(value):
    text = str(value or "").strip()
    return text or None


def normalize_plan_execution_mode(value):
    normalized = str(value or "").strip().lower()
    if normalized in {"clean", "here"}:
        return normalized
    return None


def normalize_plan_action_name(value):
    normalized = str(value or "").strip().lower()
    if normalized in {"clean", "here", "keep_planning"}:
        return normalized
    return None


def sanitize_inline_code_text(value, max_length=120):
    text = " ".join(str(value or "").split()).replace("`", "'").strip()
    if len(text) <= max_length:
        return text or "-"
    return text[: max_length - 3].rstrip() + "..."


def normalize_subagent_role(value):
    normalized = str(value or "").strip()
    return normalized or None


def normalize_subagent_nickname(value):
    normalized = str(value or "").strip()
    return normalized or None


class SlackThreadSessionStore:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._sessions = self._load()

    def _load(self):
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}

        normalized = {}
        for key, value in data.items():
            if isinstance(value, str):
                normalized[key] = {"session_id": value, "updated_at": 0}
                continue
            if not isinstance(value, dict):
                continue

            entry = {"updated_at": value.get("updated_at", 0)}
            session_id = str(value.get("session_id") or "").strip()
            if session_id:
                entry["session_id"] = session_id
            mode = value.get("mode")
            if mode in {SESSION_MODE_OBSERVE, SESSION_MODE_CONTROL}:
                entry["mode"] = mode
            owner_user_id = value.get("owner_user_id")
            if isinstance(owner_user_id, str) and owner_user_id:
                entry["owner_user_id"] = owner_user_id
            reasoning_effort = normalize_reasoning_effort(value.get("reasoning_effort"))
            if reasoning_effort:
                entry["reasoning_effort"] = reasoning_effort
            progress_updates = normalize_progress_updates(value.get("progress_updates"))
            if progress_updates is not None:
                entry["progress_updates"] = progress_updates
            watch_enabled = value.get("watch_enabled")
            if isinstance(watch_enabled, bool):
                entry["watch_enabled"] = watch_enabled
            session_origin = value.get("session_origin")
            if session_origin in {SESSION_ORIGIN_ATTACHED, SESSION_ORIGIN_SLACK}:
                entry["session_origin"] = session_origin
            session_cwd = normalize_session_cwd(value.get("session_cwd"))
            if session_cwd:
                entry["session_cwd"] = session_cwd
            collaboration_mode = normalize_collaboration_mode(value.get("collaboration_mode"))
            if collaboration_mode:
                entry["collaboration_mode"] = collaboration_mode
            latest_plan_text = normalize_plan_text(value.get("latest_plan_text"))
            if latest_plan_text:
                entry["latest_plan_text"] = latest_plan_text
            latest_plan_session_id = str(value.get("latest_plan_session_id") or "").strip()
            if latest_plan_session_id:
                entry["latest_plan_session_id"] = latest_plan_session_id
            latest_plan_approved_at = value.get("latest_plan_approved_at")
            if isinstance(latest_plan_approved_at, int) and latest_plan_approved_at > 0:
                entry["latest_plan_approved_at"] = latest_plan_approved_at
            latest_plan_execution_mode = normalize_plan_execution_mode(
                value.get("latest_plan_execution_mode")
            )
            if latest_plan_execution_mode:
                entry["latest_plan_execution_mode"] = latest_plan_execution_mode
            latest_plan_recommended_execution_mode = normalize_plan_execution_mode(
                value.get("latest_plan_recommended_execution_mode")
            )
            if latest_plan_recommended_execution_mode:
                entry["latest_plan_recommended_execution_mode"] = latest_plan_recommended_execution_mode
            latest_plan_selected_action = normalize_plan_action_name(
                value.get("latest_plan_selected_action")
            )
            if latest_plan_selected_action:
                entry["latest_plan_selected_action"] = latest_plan_selected_action
            latest_plan_execution_session_id = str(
                value.get("latest_plan_execution_session_id") or ""
            ).strip()
            if latest_plan_execution_session_id:
                entry["latest_plan_execution_session_id"] = latest_plan_execution_session_id
            pending_subagent_target = self._normalize_pending_subagent_target(
                value.get("pending_subagent_target"),
                current_session_id=session_id or None,
                owner_user_id=entry.get("owner_user_id"),
            )
            if pending_subagent_target:
                entry["pending_subagent_target"] = pending_subagent_target
            watch_last_event_key = self._normalize_watch_last_event_key(
                value.get("watch_last_event_key"),
                current_session_id=session_id or None,
            )
            if watch_last_event_key:
                entry["watch_last_event_key"] = watch_last_event_key
            if not self._has_persisted_state(entry):
                continue
            normalized[key] = entry
        return normalized

    @staticmethod
    def _has_persisted_state(entry):
        if not isinstance(entry, dict):
            return False
        return any(
            [
                bool(entry.get("session_id")),
                bool(entry.get("owner_user_id")),
                bool(entry.get("reasoning_effort")),
                "progress_updates" in entry,
                "watch_enabled" in entry,
                bool(entry.get("collaboration_mode")),
                bool(entry.get("latest_plan_text")),
                bool(entry.get("latest_plan_recommended_execution_mode")),
                bool(entry.get("latest_plan_selected_action")),
                bool(entry.get("pending_subagent_target")),
                bool(entry.get("watch_last_event_key")),
            ]
        )

    @staticmethod
    def _normalize_pending_subagent_target(value, *, current_session_id=None, owner_user_id=None):
        if not isinstance(value, dict):
            return None
        thread_id = str(value.get("thread_id") or "").strip()
        session_id = str(value.get("session_id") or "").strip()
        stored_owner_user_id = str(value.get("owner_user_id") or "").strip()
        armed_at = value.get("armed_at")
        try:
            armed_at = int(armed_at)
        except (TypeError, ValueError):
            return None
        if not thread_id or not session_id or not stored_owner_user_id or armed_at <= 0:
            return None
        if current_session_id and session_id != str(current_session_id).strip():
            return None
        if owner_user_id and stored_owner_user_id != str(owner_user_id).strip():
            return None
        raw_ttl = str(
            ENV.get(
                "CODEX_SLACK_PENDING_SUBAGENT_TTL_SECONDS",
                DEFAULT_PENDING_SUBAGENT_TTL_SECONDS,
            )
        ).strip()
        try:
            ttl_seconds = max(60, int(raw_ttl))
        except ValueError:
            ttl_seconds = DEFAULT_PENDING_SUBAGENT_TTL_SECONDS
        if (int(time.time()) - armed_at) > ttl_seconds:
            return None
        normalized = {
            "thread_id": thread_id,
            "session_id": session_id,
            "owner_user_id": stored_owner_user_id,
            "armed_at": armed_at,
        }
        agent_nickname = normalize_subagent_nickname(value.get("agent_nickname"))
        if agent_nickname:
            normalized["agent_nickname"] = agent_nickname
        agent_role = normalize_subagent_role(value.get("agent_role"))
        if agent_role:
            normalized["agent_role"] = agent_role
        return normalized

    @staticmethod
    def _preserve_pending_subagent_target(existing_entry, session_id):
        pending_target = SlackThreadSessionStore._normalize_pending_subagent_target(
            read_field(existing_entry or {}, "pending_subagent_target"),
            current_session_id=session_id,
            owner_user_id=read_field(existing_entry or {}, "owner_user_id"),
        )
        if pending_target:
            return pending_target
        return None

    @staticmethod
    def _normalize_watch_last_event_key(value, *, current_session_id=None):
        if isinstance(value, (list, tuple)) and len(value) == 2:
            value = {
                "turn_id": value[0],
                "item_id": value[1],
                "session_id": current_session_id,
                "updated_at": int(time.time()),
            }
        if not isinstance(value, dict):
            return None
        turn_id = str(value.get("turn_id") or "").strip()
        item_id = str(value.get("item_id") or "").strip()
        session_id = str(value.get("session_id") or "").strip()
        updated_at = value.get("updated_at")
        try:
            updated_at = int(updated_at)
        except (TypeError, ValueError):
            updated_at = int(time.time())
        if not turn_id or not item_id:
            return None
        if current_session_id and session_id and session_id != str(current_session_id).strip():
            return None
        normalized = {
            "turn_id": turn_id,
            "item_id": item_id,
            "updated_at": max(0, updated_at),
        }
        effective_session_id = str(current_session_id or session_id or "").strip()
        if effective_session_id:
            normalized["session_id"] = effective_session_id
        return normalized

    @staticmethod
    def _preserve_watch_last_event_key(existing_entry, session_id):
        return SlackThreadSessionStore._normalize_watch_last_event_key(
            read_field(existing_entry or {}, "watch_last_event_key"),
            current_session_id=session_id,
        )

    def _save_locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            prefix=f"{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
            encoding="utf-8",
            delete=False,
        ) as tmp:
            json.dump(self._sessions, tmp, ensure_ascii=True, indent=2, sort_keys=True)
            tmp.write("\n")
            tmp_path = Path(tmp.name)
        tmp_path.replace(self.path)

    def get(self, key):
        with self._lock:
            entry = self._sessions.get(key)
            if not entry:
                return None
            return entry.get("session_id")

    def get_mode(self, key):
        with self._lock:
            entry = self._sessions.get(key)
            if not entry:
                return None
            mode = entry.get("mode")
            if mode in {SESSION_MODE_OBSERVE, SESSION_MODE_CONTROL}:
                return mode
            if entry.get("session_id"):
                return SESSION_MODE_CONTROL
            return None

    def get_owner(self, key):
        with self._lock:
            entry = self._sessions.get(key)
            if not entry:
                return None
            return entry.get("owner_user_id")

    def get_reasoning_effort(self, key):
        with self._lock:
            entry = self._sessions.get(key)
            if not entry:
                return None
            return normalize_reasoning_effort(entry.get("reasoning_effort"))

    def get_session_origin(self, key):
        with self._lock:
            entry = self._sessions.get(key)
            if not entry:
                return None
            session_origin = entry.get("session_origin")
            if session_origin in {SESSION_ORIGIN_ATTACHED, SESSION_ORIGIN_SLACK}:
                return session_origin
            if entry.get("session_id"):
                return SESSION_ORIGIN_SLACK
            return None

    def get_session_cwd(self, key):
        with self._lock:
            entry = self._sessions.get(key)
            if not entry:
                return None
            return normalize_session_cwd(entry.get("session_cwd"))

    def get_progress_updates(self, key):
        with self._lock:
            entry = self._sessions.get(key)
            if not entry:
                return None
            return normalize_progress_updates(entry.get("progress_updates"))

    def get_watch_enabled(self, key):
        with self._lock:
            entry = self._sessions.get(key)
            if not entry:
                return None
            value = entry.get("watch_enabled")
            if isinstance(value, bool):
                return value
            return None

    def get_collaboration_mode(self, key):
        with self._lock:
            entry = self._sessions.get(key)
            if not entry:
                return None
            return normalize_collaboration_mode(entry.get("collaboration_mode"))

    def get_watch_last_event_key(self, key, *, current_session_id=None):
        with self._lock:
            entry = self._sessions.get(key)
            if not entry:
                return None
            normalized = self._normalize_watch_last_event_key(
                entry.get("watch_last_event_key"),
                current_session_id=current_session_id or entry.get("session_id"),
            )
            if normalized:
                if normalized != entry.get("watch_last_event_key"):
                    entry["watch_last_event_key"] = normalized
                    entry["updated_at"] = int(time.time())
                    self._save_locked()
                return (normalized.get("turn_id"), normalized.get("item_id"))
            if "watch_last_event_key" in entry:
                entry.pop("watch_last_event_key", None)
                if self._has_persisted_state(entry):
                    entry["updated_at"] = int(time.time())
                else:
                    self._sessions.pop(key, None)
                self._save_locked()
            return None

    def get_latest_plan(self, key):
        with self._lock:
            entry = self._sessions.get(key)
            if not entry:
                return None
            return normalize_plan_text(entry.get("latest_plan_text"))

    def get_latest_plan_session_id(self, key):
        with self._lock:
            entry = self._sessions.get(key)
            if not entry:
                return None
            value = str(entry.get("latest_plan_session_id") or "").strip()
            return value or None

    def get_latest_plan_approved_at(self, key):
        with self._lock:
            entry = self._sessions.get(key)
            if not entry:
                return None
            value = entry.get("latest_plan_approved_at")
            if isinstance(value, int) and value > 0:
                return value
            return None

    def get_latest_plan_execution_mode(self, key):
        with self._lock:
            entry = self._sessions.get(key)
            if not entry:
                return None
            return normalize_plan_execution_mode(entry.get("latest_plan_execution_mode"))

    def get_latest_plan_execution_session_id(self, key):
        with self._lock:
            entry = self._sessions.get(key)
            if not entry:
                return None
            value = str(entry.get("latest_plan_execution_session_id") or "").strip()
            return value or None

    def get_latest_plan_recommended_execution_mode(self, key):
        with self._lock:
            entry = self._sessions.get(key)
            if not entry:
                return None
            return normalize_plan_execution_mode(
                entry.get("latest_plan_recommended_execution_mode")
            )

    def get_latest_plan_selected_action(self, key):
        with self._lock:
            entry = self._sessions.get(key)
            if not entry:
                return None
            return normalize_plan_action_name(entry.get("latest_plan_selected_action"))

    def get_pending_subagent_target(self, key, *, current_session_id=None, owner_user_id=None):
        with self._lock:
            entry = self._sessions.get(key)
            if not entry:
                return None
            pending_target = self._normalize_pending_subagent_target(
                entry.get("pending_subagent_target"),
                current_session_id=current_session_id or entry.get("session_id"),
                owner_user_id=owner_user_id or entry.get("owner_user_id"),
            )
            if pending_target:
                if pending_target != entry.get("pending_subagent_target"):
                    entry["pending_subagent_target"] = pending_target
                    entry["updated_at"] = int(time.time())
                    self._save_locked()
                return dict(pending_target)
            if "pending_subagent_target" in entry:
                entry.pop("pending_subagent_target", None)
                if self._has_persisted_state(entry):
                    entry["updated_at"] = int(time.time())
                else:
                    self._sessions.pop(key, None)
                self._save_locked()
            return None

    def find_owner_for_session(self, session_id):
        with self._lock:
            for entry in self._sessions.values():
                if entry.get("session_id") != session_id:
                    continue
                owner_user_id = entry.get("owner_user_id")
                if owner_user_id:
                    return owner_user_id
            return None

    def set(self, key, session_id, owner_user_id=None, session_origin=None, session_cwd=None):
        with self._lock:
            existing_entry = self._sessions.get(key, {})
            entry = {
                "session_id": session_id,
                "updated_at": int(time.time()),
                "mode": existing_entry.get("mode") or SESSION_MODE_CONTROL,
            }
            effective_owner_user_id = owner_user_id or existing_entry.get("owner_user_id")
            if effective_owner_user_id:
                entry["owner_user_id"] = effective_owner_user_id
            reasoning_effort = normalize_reasoning_effort(existing_entry.get("reasoning_effort"))
            if reasoning_effort:
                entry["reasoning_effort"] = reasoning_effort
            progress_updates = normalize_progress_updates(existing_entry.get("progress_updates"))
            if progress_updates is not None:
                entry["progress_updates"] = progress_updates
            watch_enabled = existing_entry.get("watch_enabled")
            if isinstance(watch_enabled, bool):
                entry["watch_enabled"] = watch_enabled
            collaboration_mode = normalize_collaboration_mode(existing_entry.get("collaboration_mode"))
            if collaboration_mode:
                entry["collaboration_mode"] = collaboration_mode
            latest_plan_text = normalize_plan_text(existing_entry.get("latest_plan_text"))
            if latest_plan_text:
                entry["latest_plan_text"] = latest_plan_text
            latest_plan_session_id = str(existing_entry.get("latest_plan_session_id") or "").strip()
            if latest_plan_session_id:
                entry["latest_plan_session_id"] = latest_plan_session_id
            latest_plan_approved_at = existing_entry.get("latest_plan_approved_at")
            if isinstance(latest_plan_approved_at, int) and latest_plan_approved_at > 0:
                entry["latest_plan_approved_at"] = latest_plan_approved_at
            latest_plan_execution_mode = normalize_plan_execution_mode(
                existing_entry.get("latest_plan_execution_mode")
            )
            if latest_plan_execution_mode:
                entry["latest_plan_execution_mode"] = latest_plan_execution_mode
            latest_plan_recommended_execution_mode = normalize_plan_execution_mode(
                existing_entry.get("latest_plan_recommended_execution_mode")
            )
            if latest_plan_recommended_execution_mode:
                entry["latest_plan_recommended_execution_mode"] = latest_plan_recommended_execution_mode
            latest_plan_selected_action = normalize_plan_action_name(
                existing_entry.get("latest_plan_selected_action")
            )
            if latest_plan_selected_action:
                entry["latest_plan_selected_action"] = latest_plan_selected_action
            latest_plan_execution_session_id = str(
                existing_entry.get("latest_plan_execution_session_id") or ""
            ).strip()
            if latest_plan_execution_session_id:
                entry["latest_plan_execution_session_id"] = latest_plan_execution_session_id
            effective_session_origin = session_origin or existing_entry.get("session_origin") or SESSION_ORIGIN_SLACK
            if effective_session_origin in {SESSION_ORIGIN_ATTACHED, SESSION_ORIGIN_SLACK}:
                entry["session_origin"] = effective_session_origin
            effective_session_cwd = normalize_session_cwd(session_cwd) or normalize_session_cwd(
                existing_entry.get("session_cwd")
            )
            if effective_session_cwd:
                entry["session_cwd"] = effective_session_cwd
            watch_last_event_key = self._preserve_watch_last_event_key(existing_entry, session_id)
            if watch_last_event_key:
                entry["watch_last_event_key"] = watch_last_event_key
            pending_subagent_target = self._preserve_pending_subagent_target(existing_entry, session_id)
            if pending_subagent_target:
                entry["pending_subagent_target"] = pending_subagent_target
            self._sessions[key] = entry
            self._save_locked()

    def attach_session(
        self,
        key,
        session_id,
        owner_user_id,
        allow_unseen=False,
        mode=SESSION_MODE_OBSERVE,
        session_cwd=None,
    ):
        with self._lock:
            existing_entry = self._sessions.get(key, {})
            previous_session_id = existing_entry.get("session_id")
            existing_thread_owner_user_id = existing_entry.get("owner_user_id")
            if existing_thread_owner_user_id and existing_thread_owner_user_id != owner_user_id:
                return (
                    previous_session_id,
                    "当前 Slack thread 已经由另一位 Slack 用户绑定，当前不允许跨用户覆盖。",
                )

            current_owner_user_id = None
            for entry in self._sessions.values():
                if entry.get("session_id") != session_id:
                    continue
                current_owner_user_id = entry.get("owner_user_id")
                if current_owner_user_id:
                    break

            if current_owner_user_id and current_owner_user_id != owner_user_id:
                return previous_session_id, "这个 Codex session 已经被另一位 Slack 用户绑定过，当前不允许跨用户接管。"

            if not current_owner_user_id and not allow_unseen:
                return previous_session_id, get_shared_attach_error()

            entry = {
                "session_id": session_id,
                "updated_at": int(time.time()),
                "owner_user_id": owner_user_id,
                "mode": mode if mode in {SESSION_MODE_OBSERVE, SESSION_MODE_CONTROL} else SESSION_MODE_OBSERVE,
                "session_origin": SESSION_ORIGIN_ATTACHED,
            }
            reasoning_effort = normalize_reasoning_effort(existing_entry.get("reasoning_effort"))
            if reasoning_effort:
                entry["reasoning_effort"] = reasoning_effort
            progress_updates = normalize_progress_updates(existing_entry.get("progress_updates"))
            if progress_updates is not None:
                entry["progress_updates"] = progress_updates
            watch_enabled = existing_entry.get("watch_enabled")
            if isinstance(watch_enabled, bool):
                entry["watch_enabled"] = watch_enabled
            collaboration_mode = normalize_collaboration_mode(existing_entry.get("collaboration_mode"))
            if collaboration_mode:
                entry["collaboration_mode"] = collaboration_mode
            latest_plan_text = normalize_plan_text(existing_entry.get("latest_plan_text"))
            if latest_plan_text:
                entry["latest_plan_text"] = latest_plan_text
            latest_plan_session_id = str(existing_entry.get("latest_plan_session_id") or "").strip()
            if latest_plan_session_id:
                entry["latest_plan_session_id"] = latest_plan_session_id
            latest_plan_approved_at = existing_entry.get("latest_plan_approved_at")
            if isinstance(latest_plan_approved_at, int) and latest_plan_approved_at > 0:
                entry["latest_plan_approved_at"] = latest_plan_approved_at
            latest_plan_execution_mode = normalize_plan_execution_mode(
                existing_entry.get("latest_plan_execution_mode")
            )
            if latest_plan_execution_mode:
                entry["latest_plan_execution_mode"] = latest_plan_execution_mode
            latest_plan_recommended_execution_mode = normalize_plan_execution_mode(
                existing_entry.get("latest_plan_recommended_execution_mode")
            )
            if latest_plan_recommended_execution_mode:
                entry["latest_plan_recommended_execution_mode"] = latest_plan_recommended_execution_mode
            latest_plan_selected_action = normalize_plan_action_name(
                existing_entry.get("latest_plan_selected_action")
            )
            if latest_plan_selected_action:
                entry["latest_plan_selected_action"] = latest_plan_selected_action
            latest_plan_execution_session_id = str(
                existing_entry.get("latest_plan_execution_session_id") or ""
            ).strip()
            if latest_plan_execution_session_id:
                entry["latest_plan_execution_session_id"] = latest_plan_execution_session_id
            effective_session_cwd = normalize_session_cwd(session_cwd) or normalize_session_cwd(
                existing_entry.get("session_cwd")
            )
            if effective_session_cwd:
                entry["session_cwd"] = effective_session_cwd
            watch_last_event_key = self._preserve_watch_last_event_key(existing_entry, session_id)
            if watch_last_event_key:
                entry["watch_last_event_key"] = watch_last_event_key
            pending_subagent_target = self._preserve_pending_subagent_target(existing_entry, session_id)
            if pending_subagent_target:
                entry["pending_subagent_target"] = pending_subagent_target
            self._sessions[key] = entry
            self._save_locked()
            return previous_session_id, None

    def set_pending_subagent_target(
        self,
        key,
        *,
        thread_id,
        agent_nickname,
        agent_role,
        owner_user_id,
        session_id,
        armed_at=None,
    ):
        normalized_pending_target = self._normalize_pending_subagent_target(
            {
                "thread_id": thread_id,
                "agent_nickname": agent_nickname,
                "agent_role": agent_role,
                "owner_user_id": owner_user_id,
                "armed_at": armed_at or int(time.time()),
                "session_id": session_id,
            },
            current_session_id=session_id,
            owner_user_id=owner_user_id,
        )
        if not normalized_pending_target:
            return None
        with self._lock:
            existing_entry = dict(self._sessions.get(key, {}))
            existing_entry["pending_subagent_target"] = normalized_pending_target
            existing_entry["updated_at"] = int(time.time())
            if owner_user_id:
                existing_entry["owner_user_id"] = owner_user_id
            self._sessions[key] = existing_entry
            self._save_locked()
        return dict(normalized_pending_target)

    def clear_pending_subagent_target(self, key):
        with self._lock:
            entry = self._sessions.get(key)
            if not entry or "pending_subagent_target" not in entry:
                return None
            cleared = entry.pop("pending_subagent_target", None)
            if self._has_persisted_state(entry):
                entry["updated_at"] = int(time.time())
                self._save_locked()
            else:
                self._sessions.pop(key, None)
                self._save_locked()
            if isinstance(cleared, dict):
                return dict(cleared)
            return None

    def set_reasoning_effort(self, key, reasoning_effort, owner_user_id=None):
        normalized_effort = normalize_reasoning_effort(reasoning_effort)
        if not normalized_effort:
            return
        with self._lock:
            existing_entry = dict(self._sessions.get(key, {}))
            existing_entry["reasoning_effort"] = normalized_effort
            existing_entry["updated_at"] = int(time.time())
            effective_owner_user_id = owner_user_id or existing_entry.get("owner_user_id")
            if effective_owner_user_id:
                existing_entry["owner_user_id"] = effective_owner_user_id
            self._sessions[key] = existing_entry
            self._save_locked()

    def clear_reasoning_effort(self, key):
        with self._lock:
            entry = self._sessions.get(key)
            if not entry:
                return False
            if "reasoning_effort" not in entry:
                return False
            entry.pop("reasoning_effort", None)
            if self._has_persisted_state(entry):
                entry["updated_at"] = int(time.time())
                self._save_locked()
                return True
            self._sessions.pop(key, None)
            self._save_locked()
            return True

    def set_progress_updates(self, key, progress_updates, owner_user_id=None):
        normalized_progress_updates = normalize_progress_updates(progress_updates)
        if normalized_progress_updates is None:
            return
        with self._lock:
            existing_entry = dict(self._sessions.get(key, {}))
            existing_entry["progress_updates"] = normalized_progress_updates
            existing_entry["updated_at"] = int(time.time())
            effective_owner_user_id = owner_user_id or existing_entry.get("owner_user_id")
            if effective_owner_user_id:
                existing_entry["owner_user_id"] = effective_owner_user_id
            self._sessions[key] = existing_entry
            self._save_locked()

    def set_watch_enabled(self, key, watch_enabled, owner_user_id=None):
        if not isinstance(watch_enabled, bool):
            return
        with self._lock:
            existing_entry = dict(self._sessions.get(key, {}))
            existing_entry["watch_enabled"] = watch_enabled
            existing_entry["updated_at"] = int(time.time())
            effective_owner_user_id = owner_user_id or existing_entry.get("owner_user_id")
            if effective_owner_user_id:
                existing_entry["owner_user_id"] = effective_owner_user_id
            self._sessions[key] = existing_entry
            self._save_locked()

    def set_watch_last_event_key(self, key, session_id, event_key, owner_user_id=None):
        normalized = self._normalize_watch_last_event_key(
            {
                "turn_id": event_key[0] if isinstance(event_key, (list, tuple)) and len(event_key) == 2 else None,
                "item_id": event_key[1] if isinstance(event_key, (list, tuple)) and len(event_key) == 2 else None,
                "session_id": session_id,
                "updated_at": int(time.time()),
            },
            current_session_id=session_id,
        )
        if not normalized:
            return
        with self._lock:
            existing_entry = dict(self._sessions.get(key, {}))
            existing_entry["watch_last_event_key"] = normalized
            existing_entry["updated_at"] = int(time.time())
            effective_owner_user_id = owner_user_id or existing_entry.get("owner_user_id")
            if effective_owner_user_id:
                existing_entry["owner_user_id"] = effective_owner_user_id
            self._sessions[key] = existing_entry
            self._save_locked()

    def clear_watch_last_event_key(self, key):
        with self._lock:
            entry = self._sessions.get(key)
            if not entry or "watch_last_event_key" not in entry:
                return False
            entry.pop("watch_last_event_key", None)
            if self._has_persisted_state(entry):
                entry["updated_at"] = int(time.time())
                self._save_locked()
                return True
            self._sessions.pop(key, None)
            self._save_locked()
            return True

    def clear_progress_updates(self, key):
        with self._lock:
            entry = self._sessions.get(key)
            if not entry:
                return False
            if "progress_updates" not in entry:
                return False
            entry.pop("progress_updates", None)
            if self._has_persisted_state(entry):
                entry["updated_at"] = int(time.time())
                self._save_locked()
                return True
            self._sessions.pop(key, None)
            self._save_locked()
            return True

    def set_collaboration_mode(self, key, collaboration_mode, owner_user_id=None):
        normalized_mode = normalize_collaboration_mode(collaboration_mode)
        if not normalized_mode:
            return
        with self._lock:
            existing_entry = dict(self._sessions.get(key, {}))
            existing_entry["collaboration_mode"] = normalized_mode
            existing_entry["updated_at"] = int(time.time())
            effective_owner_user_id = owner_user_id or existing_entry.get("owner_user_id")
            if effective_owner_user_id:
                existing_entry["owner_user_id"] = effective_owner_user_id
            self._sessions[key] = existing_entry
            self._save_locked()

    def set_session_cwd(self, key, session_cwd, owner_user_id=None):
        normalized_cwd = normalize_session_cwd(session_cwd)
        if not normalized_cwd:
            return
        with self._lock:
            existing_entry = dict(self._sessions.get(key, {}))
            existing_entry["session_cwd"] = normalized_cwd
            existing_entry["updated_at"] = int(time.time())
            effective_owner_user_id = owner_user_id or existing_entry.get("owner_user_id")
            if effective_owner_user_id:
                existing_entry["owner_user_id"] = effective_owner_user_id
            self._sessions[key] = existing_entry
            self._save_locked()

    def set_latest_plan(
        self,
        key,
        plan_text,
        session_id=None,
        owner_user_id=None,
    ):
        normalized_plan_text = normalize_plan_text(plan_text)
        if not normalized_plan_text:
            return
        with self._lock:
            existing_entry = dict(self._sessions.get(key, {}))
            existing_entry["latest_plan_text"] = normalized_plan_text
            normalized_session_id = str(session_id or "").strip()
            if normalized_session_id:
                existing_entry["latest_plan_session_id"] = normalized_session_id
            existing_entry.pop("latest_plan_recommended_execution_mode", None)
            existing_entry.pop("latest_plan_selected_action", None)
            existing_entry["updated_at"] = int(time.time())
            effective_owner_user_id = owner_user_id or existing_entry.get("owner_user_id")
            if effective_owner_user_id:
                existing_entry["owner_user_id"] = effective_owner_user_id
            self._sessions[key] = existing_entry
            self._save_locked()

    def set_latest_plan_selected_action(self, key, action_name, owner_user_id=None):
        normalized_action_name = normalize_plan_action_name(action_name)
        if not normalized_action_name:
            return
        with self._lock:
            existing_entry = dict(self._sessions.get(key, {}))
            existing_entry["latest_plan_selected_action"] = normalized_action_name
            existing_entry["updated_at"] = int(time.time())
            effective_owner_user_id = owner_user_id or existing_entry.get("owner_user_id")
            if effective_owner_user_id:
                existing_entry["owner_user_id"] = effective_owner_user_id
            self._sessions[key] = existing_entry
            self._save_locked()

    def mark_plan_implemented(
        self,
        key,
        *,
        execution_mode,
        execution_session_id,
        owner_user_id=None,
    ):
        normalized_mode = normalize_plan_execution_mode(execution_mode)
        normalized_session_id = str(execution_session_id or "").strip()
        if not normalized_mode or not normalized_session_id:
            return
        with self._lock:
            existing_entry = dict(self._sessions.get(key, {}))
            existing_entry["latest_plan_execution_mode"] = normalized_mode
            existing_entry["latest_plan_execution_session_id"] = normalized_session_id
            existing_entry["latest_plan_selected_action"] = normalized_mode
            existing_entry["latest_plan_approved_at"] = int(time.time())
            existing_entry["updated_at"] = int(time.time())
            effective_owner_user_id = owner_user_id or existing_entry.get("owner_user_id")
            if effective_owner_user_id:
                existing_entry["owner_user_id"] = effective_owner_user_id
            self._sessions[key] = existing_entry
            self._save_locked()

    def set_mode(self, key, mode):
        if mode not in {SESSION_MODE_OBSERVE, SESSION_MODE_CONTROL}:
            return
        with self._lock:
            entry = self._sessions.get(key)
            if not entry:
                return
            entry["mode"] = mode
            entry["updated_at"] = int(time.time())
            self._save_locked()

    def delete(self, key):
        with self._lock:
            if key in self._sessions:
                del self._sessions[key]
                self._save_locked()

    def clear_session_binding(self, key):
        with self._lock:
            entry = self._sessions.get(key)
            if not entry:
                return
            entry.pop("session_id", None)
            entry.pop("mode", None)
            entry.pop("session_origin", None)
            entry.pop("session_cwd", None)
            entry.pop("pending_subagent_target", None)
            entry.pop("watch_last_event_key", None)
            entry["updated_at"] = int(time.time())
            if self._has_persisted_state(entry):
                self._save_locked()
                return
            self._sessions.pop(key, None)
            self._save_locked()

    def touch(self, key):
        with self._lock:
            entry = self._sessions.get(key)
            if not entry:
                return
            entry["updated_at"] = int(time.time())
            self._save_locked()

    def list_for_owner(self, owner_user_id, limit=5):
        with self._lock:
            rows = []
            for thread_key, entry in self._sessions.items():
                if entry.get("owner_user_id") != owner_user_id:
                    continue
                session_id = entry.get("session_id")
                if not session_id:
                    continue
                rows.append(
                    {
                        "thread_key": thread_key,
                        "session_id": session_id,
                        "mode": entry.get("mode") or SESSION_MODE_CONTROL,
                        "cwd": entry.get("session_cwd") or "-",
                        "updated_at": int(entry.get("updated_at") or 0),
                    }
                )

            rows.sort(key=lambda row: row["updated_at"], reverse=True)
            if limit and limit > 0:
                rows = rows[:limit]
            return rows

    def list_bindings(self):
        with self._lock:
            rows = []
            for thread_key, entry in self._sessions.items():
                session_id = str(entry.get("session_id") or "").strip()
                if not session_id:
                    continue
                rows.append(
                    {
                        "thread_key": thread_key,
                        "session_id": session_id,
                        "owner_user_id": entry.get("owner_user_id"),
                        "mode": entry.get("mode") or SESSION_MODE_CONTROL,
                        "watch_enabled": bool(entry.get("watch_enabled")) if "watch_enabled" in entry else False,
                        "watch_last_event_key": self._normalize_watch_last_event_key(
                            entry.get("watch_last_event_key"),
                            current_session_id=session_id,
                        ),
                    }
                )
            return rows


SESSION_STORE = SlackThreadSessionStore(SESSION_STORE_PATH)


@dataclass
class ThreadLockState:
    lock: object
    waiters: int = 0


@dataclass
class WatchHandle:
    thread: threading.Thread
    stop_event: threading.Event
    session_id: str
    channel: str
    thread_ts: str


@dataclass(frozen=True)
class WatchThreadSnapshot:
    path: Optional[str]
    updated_at: Optional[int]
    status_type: str


@dataclass
class PendingSlackUserInputRequest:
    token: str
    thread_key: str
    channel: str
    thread_ts: str
    owner_user_id: str
    session_id: Optional[str]
    request: RuntimeUserInputRequest
    future: concurrent.futures.Future
    prompt_message_ts: Optional[str] = None
    created_at: float = field(default_factory=time.time)


PENDING_USER_INPUT_REQUESTS = {}
PENDING_USER_INPUT_REQUESTS_GUARD = threading.Lock()


class AsyncProgressReporter:
    def __init__(self, client, channel, thread_ts, batch_seconds=None):
        self.client = client
        self.channel = channel
        self.thread_ts = thread_ts
        self.batch_seconds = max(0.5, float(batch_seconds or get_progress_batch_seconds()))
        self._queue = queue.Queue()
        self._closed = threading.Event()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

    def enqueue(self, text):
        normalized = (text or "").strip()
        if not normalized or self._closed.is_set():
            return
        self._queue.put(("message", normalized, None))

    def flush(self, timeout=10):
        if self._closed.is_set():
            return
        marker = threading.Event()
        self._queue.put(("flush", None, marker))
        marker.wait(timeout=timeout)

    def close(self, timeout=10):
        if self._closed.is_set():
            return
        marker = threading.Event()
        self._queue.put(("stop", None, marker))
        marker.wait(timeout=timeout)
        self._closed.set()
        self._worker.join(timeout=timeout)

    def _post_pending(self, pending_messages):
        merged = "\n\n".join(message for message in pending_messages if message)
        if merged:
            post_chunks(self.client, self.channel, self.thread_ts, merged)

    def _worker_loop(self):
        pending_messages = []
        deadline = None
        while True:
            timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
            try:
                command, payload, marker = self._queue.get(timeout=timeout)
            except queue.Empty:
                if pending_messages:
                    try:
                        self._post_pending(pending_messages)
                    except Exception as exc:  # pragma: no cover
                        print(f"[progress_reporter_error] {exc}", flush=True)
                    pending_messages = []
                deadline = None
                continue

            should_stop = False
            try:
                if command == "message":
                    pending_messages.append(payload)
                    if deadline is None:
                        deadline = time.monotonic() + self.batch_seconds
                elif command in {"flush", "stop"}:
                    if pending_messages:
                        self._post_pending(pending_messages)
                        pending_messages = []
                    deadline = None
                    should_stop = command == "stop"
                else:  # pragma: no cover
                    deadline = None
            except Exception as exc:  # pragma: no cover
                print(f"[progress_reporter_error] {exc}", flush=True)
                pending_messages = []
                deadline = None
                should_stop = command == "stop"
            finally:
                if marker is not None:
                    marker.set()

            if should_stop:
                return


ConversationEvent = thread_views.ConversationEvent
ProgressEvent = thread_views.ProgressEvent
WatchAnchorLostError = thread_views.WatchAnchorLostError
read_field = thread_views.read_field
read_root = thread_views.read_root
truncate_text = thread_views.truncate_text


def chunk_text(text, max_length=3500):
    normalized = (text or "").strip()
    if not normalized:
        return ["这一轮没有可发送的文本内容。"]

    chunks = []
    start = 0
    while start < len(normalized):
        chunks.append(normalized[start : start + max_length])
        start += max_length
    return chunks


def strip_app_mentions(text):
    return re.sub(r"<@[A-Z0-9]+>", "", text or "").strip()


def strip_command_payload(text, command_name):
    normalized = (text or "").strip()
    pattern = re.compile(rf"^/?{re.escape(command_name)}(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)
    match = pattern.match(normalized)
    if not match:
        return None
    return (match.group(1) or "").strip()


def is_reset_command(text):
    normalized = (text or "").strip().lower()
    return normalized in {"/reset", "reset", "reset session", "/reset-session"}


def is_fresh_command(text):
    return strip_command_payload(text, "fresh") is not None


def is_session_command(text):
    normalized = (text or "").strip().lower()
    return normalized in {"/session", "session", "session id"}


def is_status_command(text):
    normalized = (text or "").strip().lower()
    return normalized in {"/where", "where", "/whoami", "whoami", "/status", "status"}


def is_mode_command(text):
    normalized = (text or "").strip().lower()
    return normalized in {"/mode", "mode", "collaboration mode", "/collaboration-mode"}


def is_handoff_command(text):
    normalized = (text or "").strip().lower()
    return normalized in {"/handoff", "handoff"}


def is_recap_command(text):
    normalized = (text or "").strip().lower()
    return normalized in {"/recap", "recap"}


def is_recent_command(text):
    normalized = (text or "").strip().lower()
    return normalized in {"/recent", "recent"}


def is_sessions_command(text):
    return strip_command_payload(text, "sessions") is not None


def strip_sessions_command(text):
    return strip_command_payload(text, "sessions") or ""


def is_subagents_command(text):
    normalized = (text or "").strip().lower()
    return normalized in {"/subagents", "subagents", "agents", "/agents"}


def is_watch_command(text):
    return strip_command_payload(text, "watch") == ""


def is_unsupported_watch_command(text):
    payload = strip_command_payload(text, "watch")
    return payload not in (None, "")


def is_unwatch_command(text):
    normalized = (text or "").strip().lower()
    return normalized in {"/unwatch", "unwatch", "stop watch", "/stop-watch"}


def is_attach_command(text):
    return strip_command_payload(text, "attach") is not None


def strip_attach_command(text):
    return strip_command_payload(text, "attach") or ""


def parse_attach_recent_selector(payload):
    normalized = (payload or "").strip()
    match = re.match(r"^recent\s+(\d+)$", normalized, re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1))


def is_effort_command(text):
    return strip_command_payload(text, "effort") is not None


def strip_effort_command(text):
    return strip_command_payload(text, "effort") or ""


def is_name_command(text):
    return strip_command_payload(text, "name") is not None


def strip_name_command(text):
    return strip_command_payload(text, "name") or ""


def is_progress_command(text):
    return strip_command_payload(text, "progress") is not None


def strip_progress_command(text):
    return strip_command_payload(text, "progress") or ""


def is_control_command(text):
    normalized = (text or "").strip().lower()
    return normalized in {"/control", "control", "/takeover", "takeover"}


def is_observe_command(text):
    normalized = (text or "").strip().lower()
    return normalized in {"/observe", "observe", "/release", "release"}


def is_interrupt_command(text):
    normalized = (text or "").strip().lower()
    return normalized in {"/interrupt", "interrupt", "interrupt turn", "/interrupt-turn", "stop turn", "/stop-turn"}


def is_steer_command(text):
    return strip_command_payload(text, "steer") is not None


def strip_steer_command(text):
    return strip_command_payload(text, "steer") or ""


def strip_fresh_command(text):
    return strip_command_payload(text, "fresh") or ""


def parse_fresh_payload(payload):
    normalized = (payload or "").strip()
    if not normalized:
        return None, "", None

    if not normalized.lower().startswith("--effort"):
        return None, normalized, None

    match = re.match(r"^--effort(?:=|\s+)(\S+)(?:\s+(.*))?$", normalized, re.IGNORECASE | re.DOTALL)
    if not match:
        return None, normalized, (
            f"`fresh --effort` 只支持 {format_reasoning_effort_values()}，例如 "
            f"`fresh --effort high 修复测试失败`。"
        )

    reasoning_effort = normalize_reasoning_effort(match.group(1))
    if not reasoning_effort:
        return None, normalized, (
            f"`fresh --effort` 只支持 {format_reasoning_effort_values()}，例如 "
            f"`fresh --effort high 修复测试失败`。"
        )

    prompt = (match.group(2) or "").strip()
    return reasoning_effort, prompt, None


def get_codex_settings():
    codex_bin = ENV.get("CODEX_BIN", "codex")
    model = ENV.get("OPENAI_MODEL", "gpt-5.4")
    workdir = get_default_workdir()
    timeout_raw = ENV.get("CODEX_TIMEOUT_SECONDS", "900")
    try:
        timeout = int(timeout_raw)
    except ValueError as exc:
        raise RuntimeError(f"CODEX_TIMEOUT_SECONDS must be an integer, got: {timeout_raw!r}") from exc
    sandbox = ENV.get("CODEX_SANDBOX", "workspace-write")
    extra_args = ENV.get("CODEX_EXTRA_ARGS", "").strip()
    full_auto = ENV.get("CODEX_FULL_AUTO", "0") == "1"
    return codex_bin, model, workdir, timeout, sandbox, extra_args, full_auto


def get_default_workdir():
    return ENV.get("CODEX_WORKDIR", str(Path.cwd()))


def get_configured_reasoning_effort():
    return normalize_reasoning_effort(ENV.get("CODEX_REASONING_EFFORT", ""))


def get_default_reasoning_effort():
    return get_configured_reasoning_effort() or DEFAULT_REASONING_EFFORT


def get_app_server_stdio_line_limit_bytes():
    raw = str(
        ENV.get(
            "CODEX_SLACK_APP_SERVER_LINE_LIMIT_BYTES",
            DEFAULT_APP_SERVER_STDIO_LINE_LIMIT_BYTES,
        )
    ).strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_APP_SERVER_STDIO_LINE_LIMIT_BYTES
    return max(1024 * 1024, value)


def get_app_server_request_timeout_seconds():
    raw = str(
        ENV.get(
            "CODEX_SLACK_APP_SERVER_REQUEST_TIMEOUT_SECONDS",
            thread_views.DEFAULT_APP_SERVER_REQUEST_TIMEOUT_SECONDS,
        )
    ).strip()
    try:
        value = float(raw)
    except ValueError:
        return thread_views.DEFAULT_APP_SERVER_REQUEST_TIMEOUT_SECONDS
    return max(5.0, value)


def get_app_server_resume_timeout_seconds():
    raw = str(
        ENV.get(
            "CODEX_SLACK_APP_SERVER_RESUME_TIMEOUT_SECONDS",
            get_app_server_request_timeout_seconds(),
        )
    ).strip()
    try:
        value = float(raw)
    except ValueError:
        return get_app_server_request_timeout_seconds()
    return max(5.0, value)


def get_app_server_resume_max_retries():
    raw = str(
        ENV.get(
            "CODEX_SLACK_APP_SERVER_RESUME_MAX_RETRIES",
            DEFAULT_APP_SERVER_RESUME_MAX_RETRIES,
        )
    ).strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_APP_SERVER_RESUME_MAX_RETRIES
    return max(1, value)


def get_slack_startup_retry_initial_seconds():
    raw = str(
        ENV.get(
            "CODEX_SLACK_STARTUP_RETRY_INITIAL_SECONDS",
            DEFAULT_SLACK_STARTUP_RETRY_INITIAL_SECONDS,
        )
    ).strip()
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_SLACK_STARTUP_RETRY_INITIAL_SECONDS
    return max(0.5, value)


def get_slack_startup_retry_max_seconds():
    raw = str(
        ENV.get(
            "CODEX_SLACK_STARTUP_RETRY_MAX_SECONDS",
            DEFAULT_SLACK_STARTUP_RETRY_MAX_SECONDS,
        )
    ).strip()
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_SLACK_STARTUP_RETRY_MAX_SECONDS
    return max(get_slack_startup_retry_initial_seconds(), value)


def get_codex_app_server_config():
    codex_bin, _model, workdir, _timeout, _sandbox, _extra_args, _full_auto = get_codex_settings()
    return thread_views.CodexAppServerConfig(
        codex_bin=codex_bin,
        workdir=workdir,
        env=build_codex_child_env(),
        line_limit_bytes=get_app_server_stdio_line_limit_bytes(),
        request_timeout=get_app_server_request_timeout_seconds(),
        resume_request_timeout=get_app_server_resume_timeout_seconds(),
        max_retries=MAX_APP_SERVER_RETRIES,
        resume_max_retries=get_app_server_resume_max_retries(),
    )


def get_app_runtime():
    global APP_RUNTIME
    with APP_RUNTIME_GUARD:
        if APP_RUNTIME is None:
            APP_RUNTIME = AppServerRuntime(get_codex_app_server_config)
            atexit.register(APP_RUNTIME.close)
        return APP_RUNTIME


def should_reset_runtime_after_exception(exc):
    return isinstance(exc, (CodexTimeoutError, CodexTransportError))


def compact_exception_text(exc, max_length=280):
    text = " ".join(str(exc or "").split()).replace("`", "'").strip()
    if not text:
        text = exc.__class__.__name__
    if len(text) <= max_length:
        return text
    return text[: max_length - 14].rstrip() + "...<truncated>"


def build_process_error_message(user_id, exc, diagnostics=None):
    mention = f"<@{user_id}> " if user_id else ""
    detail = compact_exception_text(exc)
    if isinstance(exc, CodexTimeoutError):
        if "thread/resume" in detail:
            guidance = "我已重置内部 runtime。请直接在这个 Slack thread 再发一次相同消息重试。"
        elif "turn/start" in detail:
            guidance = "我已重置内部 runtime。请直接重试；如果持续失败，再发 `status` 查看当前绑定状态。"
        else:
            guidance = "我已重置内部 runtime。请直接重试这条请求。"
    elif isinstance(exc, CodexTransportError):
        guidance = "我已重置内部 runtime。请直接重试；如果仍失败，再检查 `codex app-server` 是否可正常启动。"
    elif isinstance(exc, CodexProtocolError):
        guidance = "这是 app-server 返回的协议级错误。请按这里的错误详情继续在 Slack 里发修复指令。"
    else:
        guidance = "服务仍在运行。你可以继续直接在这个 Slack thread 里发送下一条指令。"
    diagnostics_text = str(diagnostics or "").strip()
    diagnostics_block = ""
    if diagnostics_text:
        diagnostics_block = f"\n- app-server stderr tail:\n```text\n{diagnostics_text[:1800]}\n```"
    return (
        f"{mention}这次请求失败了，但服务仍在运行。\n\n"
        f"- error: `{exc.__class__.__name__}`\n"
        f"- detail: `{detail}`\n"
        f"{diagnostics_block}\n"
        f"- next: {guidance}"
    )


def build_empty_final_response_text(session_id=None):
    session_hint = f" 当前 session: `{session_id}`。" if session_id else ""
    return (
        "这一轮已经结束，但 Codex 没有产出可直接展示的最终答复文本。"
        f"{session_hint}\n\n"
        "你可以继续在这个 Slack thread 发送下一条消息；如果需要确认当前绑定与模式，可发送 `status` 或 `mode`。"
    )


def response_contains_proposed_plan(text):
    normalized = str(text or "")
    return "<proposed_plan>" in normalized and "</proposed_plan>" in normalized


def extract_latest_proposed_plan(text):
    normalized = str(text or "")
    matches = re.findall(
        r"<proposed_plan>\s*(.*?)\s*</proposed_plan>",
        normalized,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if not matches:
        return None
    return normalize_plan_text(matches[-1])


def extract_latest_implementation_recommendation(text):
    normalized = str(text or "")
    matches = re.findall(
        r"<implementation_recommendation>\s*(.*?)\s*</implementation_recommendation>",
        normalized,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if not matches:
        return None
    return normalize_plan_execution_mode(matches[-1])


def strip_implementation_recommendation_tags(text):
    normalized = str(text or "")
    if not normalized:
        return ""
    stripped = re.sub(
        r"\s*<implementation_recommendation>\s*.*?\s*</implementation_recommendation>\s*",
        "\n",
        normalized,
        flags=re.DOTALL | re.IGNORECASE,
    )
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)
    return stripped.strip()


def sanitize_plan_mode_response_for_slack(text):
    normalized = str(text or "")
    if not normalized:
        return ""
    if extract_latest_implementation_recommendation(normalized) is None:
        return normalized.strip()
    return strip_implementation_recommendation_tags(normalized)


def format_relative_timestamp(timestamp):
    if not isinstance(timestamp, int) or timestamp <= 0:
        return "-"
    delta = max(0, int(time.time()) - timestamp)
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        hours = delta // 3600
        minutes = (delta % 3600) // 60
        return f"{hours}h {minutes}m ago" if minutes else f"{hours}h ago"
    days = delta // 86400
    hours = (delta % 86400) // 3600
    return f"{days}d {hours}h ago" if hours else f"{days}d ago"


def get_pending_subagent_ttl_seconds():
    raw = str(
        ENV.get(
            "CODEX_SLACK_PENDING_SUBAGENT_TTL_SECONDS",
            DEFAULT_PENDING_SUBAGENT_TTL_SECONDS,
        )
    ).strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_PENDING_SUBAGENT_TTL_SECONDS
    return max(60, value)


def format_subagent_source_label(agent_nickname=None, agent_role=None, thread_id=None):
    nickname = normalize_subagent_nickname(agent_nickname) or "Subagent"
    role = normalize_subagent_role(agent_role)
    if role:
        return f"{nickname} · {role}"
    if thread_id:
        return f"{nickname} · {thread_id}"
    return nickname


def format_subagent_short_name(agent_nickname=None, agent_role=None, thread_id=None):
    nickname = normalize_subagent_nickname(agent_nickname) or "Subagent"
    role = normalize_subagent_role(agent_role)
    if role:
        return f"{nickname} ({role})"
    if thread_id:
        return f"{nickname} ({thread_id})"
    return nickname


def prepend_source_header(text, *, agent_nickname=None, agent_role=None, thread_id=None):
    normalized_text = str(text or "").strip()
    label = format_subagent_source_label(
        agent_nickname=agent_nickname,
        agent_role=agent_role,
        thread_id=thread_id,
    )
    if not normalized_text:
        return label
    return f"{label}\n\n{normalized_text}"


def extract_thread_agent_metadata(thread_read_response):
    thread = read_field(thread_read_response, "thread", thread_read_response)
    return {
        "thread_id": str(read_field(thread, "id", "") or "").strip() or None,
        "agent_nickname": normalize_subagent_nickname(read_field(thread, "agentNickname")),
        "agent_role": normalize_subagent_role(read_field(thread, "agentRole")),
        "status": extract_thread_status_type(thread_read_response),
        "updated_at": extract_thread_updated_at(thread_read_response),
    }


def maybe_prefix_thread_output(session_id, text, *, thread_read_response=None):
    if not session_id:
        return str(text or "").strip()
    response = thread_read_response
    if response is None:
        with suppress(Exception):
            response = read_thread_response(session_id, include_turns=False)
    if response is None:
        return str(text or "").strip()
    metadata = extract_thread_agent_metadata(response)
    if not metadata.get("agent_nickname") and not metadata.get("agent_role"):
        return str(text or "").strip()
    return prepend_source_header(
        text,
        agent_nickname=metadata.get("agent_nickname"),
        agent_role=metadata.get("agent_role"),
        thread_id=metadata.get("thread_id"),
    )


def get_pending_subagent_state_lines(thread_key, *, current_session_id=None, session_store=None):
    session_store = session_store or SESSION_STORE
    pending_target = session_store.get_pending_subagent_target(
        thread_key,
        current_session_id=current_session_id,
    )
    if not pending_target:
        return ["- pending_subagent_target: `-`"]
    label = format_subagent_short_name(
        pending_target.get("agent_nickname"),
        pending_target.get("agent_role"),
        pending_target.get("thread_id"),
    )
    return [
        (
            "- pending_subagent_target: "
            f"`{label}` thread=`{pending_target.get('thread_id') or '-'}` "
            f"armed_at=`{format_relative_timestamp(pending_target.get('armed_at'))}`"
        )
    ]


def persist_latest_proposed_plan(thread_key, result_text, session_id=None, owner_user_id=None):
    plan_text = extract_latest_proposed_plan(result_text)
    if not plan_text:
        return None
    SESSION_STORE.set_latest_plan(
        thread_key,
        plan_text,
        session_id=session_id,
        owner_user_id=owner_user_id,
    )
    return plan_text


def get_watch_poll_seconds():
    raw = str(ENV.get("CODEX_SLACK_WATCH_POLL_SECONDS", DEFAULT_WATCH_POLL_SECONDS)).strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_WATCH_POLL_SECONDS
    return max(1, value)


def get_watch_metadata_fallback_seconds():
    raw = str(
        ENV.get(
            "CODEX_SLACK_WATCH_METADATA_FALLBACK_SECONDS",
            DEFAULT_WATCH_METADATA_FALLBACK_SECONDS,
        )
    ).strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_WATCH_METADATA_FALLBACK_SECONDS
    return max(1, value)


def get_watch_fs_debounce_seconds():
    raw = str(
        ENV.get(
            "CODEX_SLACK_WATCH_FS_DEBOUNCE_SECONDS",
            DEFAULT_WATCH_FS_DEBOUNCE_SECONDS,
        )
    ).strip()
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_WATCH_FS_DEBOUNCE_SECONDS
    return max(0.05, value)


def get_progress_heartbeat_seconds():
    raw = str(ENV.get("CODEX_PROGRESS_HEARTBEAT_SECONDS", DEFAULT_PROGRESS_HEARTBEAT_SECONDS)).strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_PROGRESS_HEARTBEAT_SECONDS
    return max(1, value)


def get_progress_poll_seconds():
    raw = str(ENV.get("CODEX_PROGRESS_POLL_SECONDS", DEFAULT_PROGRESS_POLL_SECONDS)).strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_PROGRESS_POLL_SECONDS
    return max(1, value)


def get_progress_batch_seconds():
    raw = str(ENV.get("CODEX_PROGRESS_BATCH_SECONDS", DEFAULT_PROGRESS_BATCH_SECONDS)).strip()
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_PROGRESS_BATCH_SECONDS
    return max(0.5, value)


def get_default_progress_updates_enabled():
    normalized = normalize_progress_updates(ENV.get("CODEX_PROGRESS_UPDATES", DEFAULT_PROGRESS_UPDATES_ENABLED))
    if normalized is None:
        return DEFAULT_PROGRESS_UPDATES_ENABLED
    return normalized


def resolve_progress_updates(thread_key, session_store=None):
    session_store = session_store or SESSION_STORE
    thread_override = session_store.get_progress_updates(thread_key)
    if thread_override is not None:
        return thread_override, "thread"
    return get_default_progress_updates_enabled(), "default"


def get_progress_updates_state_lines(thread_key, session_store=None):
    session_store = session_store or SESSION_STORE
    thread_override = session_store.get_progress_updates(thread_key)
    effective_value, source = resolve_progress_updates(thread_key, session_store=session_store)
    return [
        f"- progress_updates_effective: `{format_progress_updates_value(effective_value)}`",
        f"- progress_updates_source: `{source}`",
        f"- progress_updates_thread_override: `{format_progress_updates_value(thread_override)}`",
        f"- progress_updates_default: `{format_progress_updates_value(get_default_progress_updates_enabled())}`",
    ]


def get_allowed_slack_user_ids():
    raw = ENV.get("ALLOWED_SLACK_USER_IDS", "").strip()
    if not raw:
        return set()
    return {part.strip() for part in re.split(r"[\s,]+", raw) if part.strip()}


def is_allowed_slack_user(user_id):
    allowed_user_ids = get_allowed_slack_user_ids()
    if not allowed_user_ids:
        return True
    return user_id in allowed_user_ids


def is_shared_attach_enabled():
    return ENV.get("ALLOW_SHARED_ATTACH", "0").strip() == "1"


def is_valid_attach_session_id(session_id):
    return bool(SESSION_ID_RE.fullmatch((session_id or "").strip()))


def is_unseen_attach_allowed(user_id):
    allowed_user_ids = get_allowed_slack_user_ids()
    return (len(allowed_user_ids) == 1 and user_id in allowed_user_ids) or is_shared_attach_enabled()


def get_shared_attach_error():
    return (
        "当前默认只允许在“单用户白名单”模式下 attach 一个尚未被 bot 见过的 session。"
        " 如果你确实需要多用户共享 attach，请在 `.env` 里设置 `ALLOW_SHARED_ATTACH=1`。"
    )


def get_thread_owner_error(user_id):
    return f"<@{user_id}> 当前 Slack thread 已经由另一位 Slack 用户绑定，当前不允许跨用户继续使用。"


def get_thread_owner_access_error(thread_key, user_id, session_store=None):
    session_store = session_store or SESSION_STORE
    owner_user_id = session_store.get_owner(thread_key)
    if owner_user_id and owner_user_id != user_id:
        return get_thread_owner_error(user_id)
    return None


def get_attach_error(user_id, session_id, session_store=None):
    session_store = session_store or SESSION_STORE
    normalized_session_id = (session_id or "").strip()

    if not normalized_session_id:
        return "请用 `attach <session_id>` 绑定一个已有的 Codex 会话。"

    if not is_valid_attach_session_id(normalized_session_id):
        return "`attach` 目前只接受 Codex session UUID，例如 `attach 019d5868-71ba-7101-9143-81867f3db5bf`。"

    owner_user_id = session_store.find_owner_for_session(normalized_session_id)
    if owner_user_id and owner_user_id != user_id:
        return "这个 Codex session 已经被另一位 Slack 用户绑定过，当前不允许跨用户接管。"

    if owner_user_id == user_id:
        return None

    if is_unseen_attach_allowed(user_id):
        return None

    return get_shared_attach_error()


def attach_thread_to_session(
    client,
    channel,
    thread_ts,
    thread_key,
    *,
    session_id,
    user_id,
    mode=SESSION_MODE_OBSERVE,
    include_bootstrap=False,
):
    normalized_session_id = str(session_id or "").strip()
    attach_error = get_attach_error(user_id, normalized_session_id)
    if attach_error:
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=attach_error)
        return False

    attached_session_cwd = None
    with suppress(Exception):
        attached_session_cwd = read_thread_cwd(normalized_session_id)

    previous_session_id, attach_error = SESSION_STORE.attach_session(
        thread_key,
        normalized_session_id,
        owner_user_id=user_id,
        allow_unseen=is_unseen_attach_allowed(user_id),
        mode=mode,
        session_cwd=attached_session_cwd,
    )
    if attach_error:
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=attach_error)
        return False

    ACTIVE_TURN_REGISTRY.clear_for_thread(thread_key)
    stop_watcher(thread_key)
    SESSION_STORE.clear_pending_subagent_target(thread_key)
    log_session_event(
        "attach",
        thread_key,
        existing_session_id=previous_session_id,
        next_session_id=normalized_session_id,
    )

    attach_cwd_note = (
        f"\n\n已识别这个 session 的工作目录：`{attached_session_cwd}`。"
        if attached_session_cwd
        else "\n\n暂时还没读到这个 session 的工作目录。"
    )
    mode_text = (
        "默认已进入 `observe` 模式。你可以先用 `watch`、`where`、`session` 查看 thread 对话。"
        " 如果你确认要由 Slack 接管，再发送 `control` 或 `takeover`。"
        if mode == SESSION_MODE_OBSERVE
        else "当前已直接进入 `control` 模式，后续普通消息会继续发给这个 session。"
    )
    message = (
        f"<@{user_id}> 当前 Slack thread 已绑定到 Codex session `{normalized_session_id}`。\n\n"
        f"{mode_text}{attach_cwd_note}"
    )
    if include_bootstrap:
        with suppress(Exception):
            watch_text, _last_event_key = build_watch_bootstrap(normalized_session_id)
            message = f"{message}\n\n{watch_text}"
    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=message)
    return True


def get_session_mode(thread_key, session_store=None):
    session_store = session_store or SESSION_STORE
    return session_store.get_mode(thread_key)


def get_session_origin(thread_key, session_store=None):
    session_store = session_store or SESSION_STORE
    return session_store.get_session_origin(thread_key)


def get_session_cwd(thread_key, session_store=None):
    session_store = session_store or SESSION_STORE
    return session_store.get_session_cwd(thread_key)


def resolve_collaboration_mode(thread_key, session_store=None):
    session_store = session_store or SESSION_STORE
    return session_store.get_collaboration_mode(thread_key) or COLLABORATION_MODE_DEFAULT


def get_collaboration_mode_state_lines(thread_key, session_store=None):
    session_store = session_store or SESSION_STORE
    thread_mode = session_store.get_collaboration_mode(thread_key)
    effective_mode = resolve_collaboration_mode(thread_key, session_store=session_store)
    return [
        f"- collaboration_mode_effective: `{effective_mode}`",
        f"- collaboration_mode_thread_override: `{thread_mode or '-'}`",
    ]


def get_plan_state_lines(thread_key, session_store=None):
    session_store = session_store or SESSION_STORE
    latest_plan_session_id = session_store.get_latest_plan_session_id(thread_key)
    latest_plan_execution_mode = session_store.get_latest_plan_execution_mode(thread_key)
    latest_plan_execution_session_id = session_store.get_latest_plan_execution_session_id(thread_key)
    latest_plan_selected_action = session_store.get_latest_plan_selected_action(thread_key)
    latest_plan_approved_at = session_store.get_latest_plan_approved_at(thread_key)
    latest_plan_text = session_store.get_latest_plan(thread_key)
    return [
        f"- latest_plan_session_id: `{latest_plan_session_id or '-'}`",
        f"- latest_plan_selected_action: `{latest_plan_selected_action or '-'}`",
        f"- latest_plan_execution_mode: `{latest_plan_execution_mode or '-'}`",
        f"- latest_plan_execution_session_id: `{latest_plan_execution_session_id or '-'}`",
        f"- latest_plan_approved_at: `{format_relative_timestamp(latest_plan_approved_at)}`",
        f"- latest_plan_preview: `{sanitize_inline_code_text(latest_plan_text or '-', max_length=100)}`",
    ]


def format_collaboration_mode_label(collaboration_mode):
    normalized = normalize_collaboration_mode(collaboration_mode) or COLLABORATION_MODE_DEFAULT
    if normalized == COLLABORATION_MODE_PLAN:
        return "Plan"
    return "Default"


def extract_thread_cwd(thread_read_response):
    return thread_views.extract_thread_cwd(thread_read_response)


def extract_thread_path(thread_read_response):
    thread = read_field(thread_read_response, "thread", thread_read_response)
    normalized = str(read_field(thread, "path", "") or "").strip()
    return normalized or None


def extract_thread_status_type(thread_read_response):
    thread = read_field(thread_read_response, "thread", thread_read_response)
    status = read_root(read_field(thread, "status", {}) or {})
    normalized = str(read_field(status, "type", "") or "").strip()
    return normalized or "unknown"


def extract_thread_updated_at(thread_read_response):
    thread = read_field(thread_read_response, "thread", thread_read_response)
    updated_at = read_field(thread, "updatedAt")
    if updated_at is None:
        return None
    try:
        return int(updated_at)
    except (TypeError, ValueError):
        return None


def extract_watch_thread_snapshot(thread_read_response):
    return WatchThreadSnapshot(
        path=extract_thread_path(thread_read_response),
        updated_at=extract_thread_updated_at(thread_read_response),
        status_type=extract_thread_status_type(thread_read_response),
    )


def read_thread_cwd(session_id):
    return extract_thread_cwd(read_thread_response(session_id, include_turns=False))


def refresh_session_cwd(thread_key, session_id, owner_user_id=None, session_store=None):
    session_store = session_store or SESSION_STORE
    if not session_id:
        return session_store.get_session_cwd(thread_key)

    try:
        session_cwd = read_thread_cwd(session_id)
    except Exception:
        return session_store.get_session_cwd(thread_key)

    if session_cwd:
        session_store.set_session_cwd(thread_key, session_cwd, owner_user_id=owner_user_id)
    return session_cwd or session_store.get_session_cwd(thread_key)


def resolve_workdir(thread_key, session_id=None, session_cwd=None, session_store=None):
    session_store = session_store or SESSION_STORE
    if session_id:
        return normalize_session_cwd(session_cwd) or session_store.get_session_cwd(thread_key) or get_default_workdir()
    return get_default_workdir()


def resolve_reasoning_effort(thread_key, session_id=None, session_origin=None, session_store=None):
    session_store = session_store or SESSION_STORE
    thread_reasoning_effort = session_store.get_reasoning_effort(thread_key)
    if thread_reasoning_effort:
        return thread_reasoning_effort, "thread"

    effective_session_id = session_id if session_id is not None else session_store.get(thread_key)
    effective_session_origin = session_origin if session_origin is not None else session_store.get_session_origin(thread_key)
    if effective_session_id and effective_session_origin == SESSION_ORIGIN_ATTACHED:
        return None, "inherited"

    env_reasoning_effort = get_configured_reasoning_effort()
    if env_reasoning_effort:
        return env_reasoning_effort, "env"

    return get_default_reasoning_effort(), "default"


def format_effective_reasoning_effort(reasoning_effort, source):
    if source == "inherited":
        return "inherited"
    if not reasoning_effort:
        return "-"
    if source in {"thread", "env", "default"}:
        return f"{reasoning_effort} ({source})"
    return reasoning_effort


def build_reasoning_effort_args(reasoning_effort):
    normalized = normalize_reasoning_effort(reasoning_effort)
    if not normalized:
        return []
    return ["--config", f'model_reasoning_effort="{normalized}"']


def build_image_args(image_paths):
    args = []
    for raw_path in image_paths or []:
        path = str(raw_path or "").strip()
        if not path:
            continue
        args.extend(["--image", path])
    return args


def build_document_attachment_prompt(prompt, downloaded_documents):
    normalized_prompt = str(prompt or "").strip()
    documents = list(downloaded_documents or [])
    if not documents:
        return normalized_prompt

    lines = []
    if normalized_prompt:
        lines.append(normalized_prompt)
        lines.append("")
    lines.append("以下是本次 Slack 消息附带的文档文件，它们已经下载到本地。请先按需读取这些文件的实际内容，再继续处理请求：")
    for item in documents:
        mimetype = str(getattr(item, "mimetype", "") or "").strip() or "-"
        filename = str(getattr(item, "filename", "") or getattr(getattr(item, "path", None), "name", "document")).strip()
        path = str(getattr(item, "path", "") or "").strip()
        lines.append(f"- {filename} | {mimetype} | path=`{path}`")
    return "\n".join(lines).strip()


def get_default_attachment_only_prompt(*, has_images=False, has_documents=False):
    if has_images and has_documents:
        return DEFAULT_IMAGE_AND_DOCUMENT_ONLY_PROMPT
    if has_images:
        return DEFAULT_IMAGE_ONLY_PROMPT
    if has_documents:
        return DEFAULT_DOCUMENT_ONLY_PROMPT
    return ""


def get_observe_mode_error(user_id, session_id):
    return (
        f"<@{user_id}> 当前 Slack thread 绑定的 session `{session_id or '-'}` 处于 `observe` 模式。"
        " 为避免和终端里的交互式 Codex 会话并发写入，普通消息暂不会继续 `resume`。"
        " 如果你确认要由 Slack 接管，请先发送 `control` 或 `takeover`。"
    )


def get_reasoning_effort_state_lines(thread_key, session_id=None, session_origin=None, session_store=None):
    session_store = session_store or SESSION_STORE
    thread_reasoning_effort = session_store.get_reasoning_effort(thread_key)
    env_reasoning_effort = get_configured_reasoning_effort()
    effective_reasoning_effort, effective_source = resolve_reasoning_effort(
        thread_key,
        session_id=session_id,
        session_origin=session_origin,
        session_store=session_store,
    )
    return [
        f"- thread_reasoning_effort: `{thread_reasoning_effort or '-'}`",
        f"- env_reasoning_effort: `{env_reasoning_effort or '-'}`",
        f"- effective_reasoning_effort: `{format_effective_reasoning_effort(effective_reasoning_effort, effective_source)}`",
    ]


def get_reasoning_effort_set_message(thread_key, reasoning_effort, session_id=None, session_origin=None, session_store=None):
    session_store = session_store or SESSION_STORE
    current_session_origin = session_origin if session_origin is not None else session_store.get_session_origin(thread_key)
    suffix = (
        "后续由这个 Slack thread 发起的 turns 会使用该值。"
        if not session_id or current_session_origin != SESSION_ORIGIN_ATTACHED
        else "后续由这个 Slack thread 发起的 turns 会使用该值，并覆盖这个已 attach 会话原本继承的 effort。"
    )
    return (
        f"已将当前 Slack thread 的 reasoning effort 设为 `{reasoning_effort}`。\n\n"
        f"{suffix}"
    )


def get_reasoning_effort_reset_message(thread_key, session_id=None, session_origin=None, session_store=None):
    session_store = session_store or SESSION_STORE
    effective_reasoning_effort, effective_source = resolve_reasoning_effort(
        thread_key,
        session_id=session_id,
        session_origin=session_origin,
        session_store=session_store,
    )
    if effective_source == "inherited":
        fallback_text = "由于这是一个 attach 进来的已有 session，后续由 Slack 发起的 turns 会继续继承原 session 的 effort 设置。"
    elif effective_source == "env":
        fallback_text = f"后续由 Slack 发起的 turns 会回退到 `.env` 中的默认值 `{effective_reasoning_effort}`。"
    else:
        fallback_text = f"后续由 Slack 发起的 turns 会回退到默认值 `{effective_reasoning_effort}`。"
    return (
        "已清除当前 Slack thread 的 reasoning effort override。\n\n"
        f"{fallback_text}"
    )


def build_runtime_collaboration_mode_payload(collaboration_mode, reasoning_effort=None):
    normalized_mode = normalize_collaboration_mode(collaboration_mode)
    if not normalized_mode:
        return None
    _codex_bin, model, _default_workdir, _timeout, _sandbox, _extra_args, _full_auto = get_codex_settings()
    payload = {
        "mode": normalized_mode,
        "settings": {
            "model": model,
            "reasoningEffort": normalize_reasoning_effort(reasoning_effort),
            "developerInstructions": None,
        },
    }
    return payload


def encode_thread_plan_action_value(thread_key, action_name):
    return json.dumps(
        {
            "thread_key": str(thread_key or "").strip(),
            "action": str(action_name or "").strip(),
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )


def decode_thread_plan_action_value(raw_value):
    payload = json.loads(str(raw_value or ""))
    if not isinstance(payload, dict):
        raise RuntimeError("plan action payload invalid")
    thread_key = str(payload.get("thread_key") or "").strip()
    action_name = str(payload.get("action") or "").strip()
    if action_name not in {"clean", "here", "keep_planning"} or not thread_key:
        raise RuntimeError("plan action payload incomplete")
    return thread_key, action_name


def encode_thread_collaboration_mode_value(thread_key, target_mode):
    return json.dumps(
        {
            "thread_key": str(thread_key or "").strip(),
            "target_mode": normalize_collaboration_mode(target_mode),
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )


def decode_thread_collaboration_mode_value(raw_value):
    payload = json.loads(str(raw_value or ""))
    if not isinstance(payload, dict):
        raise RuntimeError("collaboration mode payload invalid")
    thread_key = str(payload.get("thread_key") or "").strip()
    target_mode = normalize_collaboration_mode(payload.get("target_mode"))
    if not thread_key or not target_mode:
        raise RuntimeError("collaboration mode payload incomplete")
    return thread_key, target_mode


def encode_subagent_action_value(thread_key, session_id, subagent_thread_id):
    return json.dumps(
        {
            "thread_key": str(thread_key or "").strip(),
            "session_id": str(session_id or "").strip(),
            "subagent_thread_id": str(subagent_thread_id or "").strip(),
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )


def decode_subagent_action_value(raw_value):
    payload = json.loads(str(raw_value or ""))
    if not isinstance(payload, dict):
        raise RuntimeError("subagent action payload invalid")
    thread_key = str(payload.get("thread_key") or "").strip()
    session_id = str(payload.get("session_id") or "").strip()
    subagent_thread_id = str(payload.get("subagent_thread_id") or "").strip()
    if not thread_key or not session_id or not subagent_thread_id:
        raise RuntimeError("subagent action payload incomplete")
    return thread_key, session_id, subagent_thread_id


def build_subagent_send_cancel_blocks(thread_key, session_id):
    return [
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": SUBAGENT_SEND_CANCEL_ACTION,
                    "text": {"type": "plain_text", "text": "Cancel"},
                    "value": encode_subagent_action_value(thread_key, session_id, "cancel"),
                }
            ],
        }
    ]


def extract_subagent_candidates_from_thread(thread_read_response, main_thread_id):
    candidates = {}
    thread = read_field(thread_read_response, "thread", thread_read_response)
    turns = read_field(thread, "turns", []) or []
    for turn in turns:
        for item in read_field(turn, "items", []) or []:
            root = read_root(item)
            if str(read_field(root, "type", "") or "").strip() != "collabAgentToolCall":
                continue
            tool_name = str(read_field(root, "tool", "") or "").strip()
            if tool_name not in {"spawn_agent", "send_input", "resume_agent", "wait", "wait_agent", "close_agent"}:
                continue
            sender_thread_id = str(read_field(root, "senderThreadId", "") or "").strip()
            if sender_thread_id and sender_thread_id != str(main_thread_id or "").strip():
                continue
            receiver_thread_ids = read_field(root, "receiverThreadIds", []) or []
            agents_states = read_field(root, "agentsStates", {}) or {}
            for receiver_thread_id in receiver_thread_ids:
                normalized_thread_id = str(receiver_thread_id or "").strip()
                if not normalized_thread_id or normalized_thread_id == str(main_thread_id or "").strip():
                    continue
                candidate = candidates.setdefault(
                    normalized_thread_id,
                    {
                        "thread_id": normalized_thread_id,
                        "status": None,
                        "updated_at": None,
                    },
                )
                state = read_field(agents_states, normalized_thread_id, {}) or {}
                state_status = str(read_field(state, "status", "") or "").strip()
                if state_status:
                    candidate["status"] = state_status
    return candidates


def discover_subagents(main_session_id):
    if not main_session_id:
        return []
    main_thread_response = read_thread_response(main_session_id, include_turns=True)
    candidates = extract_subagent_candidates_from_thread(main_thread_response, main_session_id)
    subagents = []
    for thread_id, candidate in candidates.items():
        try:
            thread_response = read_thread_response(thread_id, include_turns=False)
        except Exception:
            continue
        metadata = extract_thread_agent_metadata(thread_response)
        subagents.append(
            {
                "thread_id": thread_id,
                "agent_nickname": metadata.get("agent_nickname") or candidate.get("agent_nickname"),
                "agent_role": metadata.get("agent_role") or candidate.get("agent_role"),
                "status": metadata.get("status") or candidate.get("status") or "unknown",
                "updated_at": metadata.get("updated_at") or candidate.get("updated_at"),
            }
        )
    subagents.sort(key=lambda item: int(item.get("updated_at") or 0), reverse=True)
    return subagents


def find_subagent_for_main_session(main_session_id, subagent_thread_id):
    normalized_thread_id = str(subagent_thread_id or "").strip()
    if not main_session_id or not normalized_thread_id:
        return None
    for item in discover_subagents(main_session_id):
        if item.get("thread_id") == normalized_thread_id:
            return item
    return None


def get_pending_subagent_rebuild_notice(thread_key, *, previous_session_id, next_session_id, owner_user_id):
    if not previous_session_id or not next_session_id or previous_session_id == next_session_id:
        return None
    pending_target = SESSION_STORE.get_pending_subagent_target(
        thread_key,
        current_session_id=previous_session_id,
        owner_user_id=owner_user_id,
    )
    if not pending_target:
        return None
    label = format_subagent_short_name(
        pending_target.get("agent_nickname"),
        pending_target.get("agent_role"),
        pending_target.get("thread_id"),
    )
    return (
        f"<@{owner_user_id}> 由于当前主 session 已切换为 `{next_session_id}`，"
        f"之前挂起的 subagent 单次路由 `{label}` 已自动失效；当前目标仍是 `main`。"
    )


def handle_subagent_send_next_action(client, logger, *, thread_key, session_id, subagent_thread_id, user_id, channel_id, thread_ts):
    current_session_id = SESSION_STORE.get(thread_key)
    current_mode = get_session_mode(thread_key)
    if current_session_id != session_id:
        SESSION_STORE.clear_pending_subagent_target(thread_key)
        client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text="当前主 session 已变化，之前看到的 subagent 列表已过期。请重新发送 `subagents`。",
        )
        return
    if current_mode != SESSION_MODE_CONTROL:
        client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=f"<@{user_id}> 当前 Slack thread 处于 `observe` 模式。请先发送 `control` 或 `takeover`，再选择 `Send next message`。",
        )
        return
    try:
        subagent = find_subagent_for_main_session(session_id, subagent_thread_id)
    except Exception as exc:
        subagent = None
        logger.exception("Failed discovering subagent %s for %s: %r", subagent_thread_id, session_id, exc)
    if not subagent:
        SESSION_STORE.clear_pending_subagent_target(thread_key)
        client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text="这个 subagent 已不可用、不可读，或不再属于当前主 session。请重新发送 `subagents`。",
        )
        return
    previous_target = SESSION_STORE.get_pending_subagent_target(
        thread_key,
        current_session_id=session_id,
        owner_user_id=user_id,
    )
    SESSION_STORE.set_pending_subagent_target(
        thread_key,
        thread_id=subagent_thread_id,
        agent_nickname=subagent.get("agent_nickname"),
        agent_role=subagent.get("agent_role"),
        owner_user_id=user_id,
        session_id=session_id,
    )
    label = format_subagent_short_name(
        subagent.get("agent_nickname"),
        subagent.get("agent_role"),
        subagent_thread_id,
    )
    if previous_target and previous_target.get("thread_id") != subagent_thread_id:
        text = f"下一条普通消息目标已更新为 `{label}`。发送后会自动恢复 `main`。"
    else:
        text = f"下一条普通消息将发给 `{label}`，发送后会自动恢复 `main`。"
    client.chat_postMessage(
        channel=channel_id,
        thread_ts=thread_ts,
        text=text,
        blocks=build_subagent_send_cancel_blocks(thread_key, session_id),
    )


def handle_subagent_send_cancel_action(client, *, thread_key, channel_id, thread_ts):
    SESSION_STORE.clear_pending_subagent_target(thread_key)
    client.chat_postMessage(
        channel=channel_id,
        thread_ts=thread_ts,
        text="已取消这次 subagent 单次路由；当前目标仍是 `main`。",
    )


def handle_subagent_observe_action(client, *, thread_key, session_id, subagent_thread_id, user_id, channel_id, thread_ts):
    if SESSION_STORE.get(thread_key) != session_id:
        client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text="当前主 session 已变化，之前看到的 subagent 列表已过期。请重新发送 `subagents`。",
        )
        return
    attach_thread_to_session(
        client,
        channel_id,
        thread_ts,
        thread_key,
        session_id=subagent_thread_id,
        user_id=user_id,
        mode=SESSION_MODE_OBSERVE,
        include_bootstrap=True,
    )


def handle_subagent_attach_action(client, *, thread_key, session_id, subagent_thread_id, user_id, channel_id, thread_ts):
    if SESSION_STORE.get(thread_key) != session_id:
        client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text="当前主 session 已变化，之前看到的 subagent 列表已过期。请重新发送 `subagents`。",
        )
        return
    attach_thread_to_session(
        client,
        channel_id,
        thread_ts,
        thread_key,
        session_id=subagent_thread_id,
        user_id=user_id,
        mode=SESSION_MODE_OBSERVE,
    )


def build_subagents_message(thread_key, session_id, subagents, *, session_mode):
    intro_lines = [
        "*Subagents*",
        f"Main session: `{session_id}`",
    ]
    if session_mode == SESSION_MODE_OBSERVE:
        intro_lines.append("当前 Slack thread 处于 `observe` 模式。你可以查看和只读进入 subagent，但不能 arm `Send next message`。")
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(intro_lines)},
        }
    ]
    if not subagents:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "_当前主 thread 还没有发现可用的 subagents。_"},
            }
        )
        return "Subagents", blocks

    for item in subagents:
        label = format_subagent_short_name(
            item.get("agent_nickname"),
            item.get("agent_role"),
            item.get("thread_id"),
        )
        updated_at = format_relative_timestamp(item.get("updated_at"))
        action_elements = []
        if session_mode != SESSION_MODE_OBSERVE:
            action_elements.append(
                {
                    "type": "button",
                    "action_id": SUBAGENT_SEND_NEXT_ACTION,
                    "text": {"type": "plain_text", "text": "Send next message"},
                    "value": encode_subagent_action_value(
                        thread_key,
                        session_id,
                        item.get("thread_id"),
                    ),
                }
            )
        action_elements.extend(
            [
                {
                    "type": "button",
                    "action_id": SUBAGENT_OBSERVE_ACTION,
                    "text": {"type": "plain_text", "text": "Observe"},
                    "value": encode_subagent_action_value(
                        thread_key,
                        session_id,
                        item.get("thread_id"),
                    ),
                },
                {
                    "type": "button",
                    "action_id": SUBAGENT_ATTACH_ACTION,
                    "text": {"type": "plain_text", "text": "Attach"},
                    "value": encode_subagent_action_value(
                        thread_key,
                        session_id,
                        item.get("thread_id"),
                    ),
                },
            ]
        )
        blocks.extend(
            [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*{label}*\n"
                            f"thread_id: `{item.get('thread_id')}`\n"
                            f"status: `{item.get('status') or 'unknown'}`\n"
                            f"updated: `{updated_at}`"
                        ),
                    },
                },
                {
                    "type": "actions",
                    "elements": action_elements,
                },
            ]
        )
    return "Subagents", blocks


def build_thread_collaboration_mode_message(thread_key, session_id=None, collaboration_mode=None):
    effective_mode = normalize_collaboration_mode(collaboration_mode) or resolve_collaboration_mode(thread_key)
    intro = (
        f"*Collaboration Mode*\nCurrent: `{format_collaboration_mode_label(effective_mode)}`"
    )
    if session_id:
        intro += f"\nSession: `{session_id}`"
    else:
        intro += "\nThis applies to later Slack-owned turns in this thread."
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": intro},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": THREAD_COLLABORATION_MODE_PLAN_ACTION,
                    "text": {"type": "plain_text", "text": "Plan"},
                    "style": "primary" if effective_mode == COLLABORATION_MODE_PLAN else None,
                    "value": encode_thread_collaboration_mode_value(
                        thread_key,
                        COLLABORATION_MODE_PLAN,
                    ),
                },
                {
                    "type": "button",
                    "action_id": THREAD_COLLABORATION_MODE_DEFAULT_ACTION,
                    "text": {"type": "plain_text", "text": "Default"},
                    "style": "primary" if effective_mode == COLLABORATION_MODE_DEFAULT else None,
                    "value": encode_thread_collaboration_mode_value(
                        thread_key,
                        COLLABORATION_MODE_DEFAULT,
                    ),
                },
            ],
        },
    ]
    for element in blocks[1]["elements"]:
        if element.get("style") is None:
            element.pop("style", None)
    return intro.replace("*", ""), blocks


def post_thread_collaboration_mode_message(client, channel, thread_ts, thread_key, session_id=None):
    text, blocks = build_thread_collaboration_mode_message(
        thread_key,
        session_id=session_id,
        collaboration_mode=resolve_collaboration_mode(thread_key),
    )
    return client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=text,
        blocks=blocks,
    )


def build_thread_plan_actions_message(thread_key, session_id=None, footer_note=None):
    plan_session_id = SESSION_STORE.get_latest_plan_session_id(thread_key) or session_id or "-"
    approved_at = SESSION_STORE.get_latest_plan_approved_at(thread_key)
    execution_mode = SESSION_STORE.get_latest_plan_execution_mode(thread_key)
    execution_session_id = SESSION_STORE.get_latest_plan_execution_session_id(thread_key)
    selected_action = SESSION_STORE.get_latest_plan_selected_action(thread_key)
    current_mode = resolve_collaboration_mode(thread_key)
    lines = [
        "*Approved Plan*",
        "选择下一步：继续细化方案，或开始按这份方案实施。",
        f"Planning session: `{plan_session_id}`",
        f"Current collaboration mode: `{format_collaboration_mode_label(current_mode)}`",
    ]
    if execution_mode and execution_session_id:
        lines.append(
            "Last implementation: "
            f"`{execution_mode}` -> `{execution_session_id}` ({format_relative_timestamp(approved_at)})"
        )
    if footer_note:
        lines.append("")
        lines.append(str(footer_note).strip())
    text = "\n".join(lines)
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": text},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": THREAD_PLAN_IMPLEMENT_CLEAN_ACTION,
                    "text": {"type": "plain_text", "text": "Implement clean"},
                    "value": encode_thread_plan_action_value(thread_key, "clean"),
                    "style": "primary" if selected_action == "clean" else None,
                },
                {
                    "type": "button",
                    "action_id": THREAD_PLAN_IMPLEMENT_HERE_ACTION,
                    "text": {"type": "plain_text", "text": "Implement here"},
                    "value": encode_thread_plan_action_value(thread_key, "here"),
                    "style": "primary" if selected_action == "here" else None,
                },
                {
                    "type": "button",
                    "action_id": THREAD_PLAN_KEEP_PLANNING_ACTION,
                    "text": {"type": "plain_text", "text": "Keep planning"},
                    "value": encode_thread_plan_action_value(thread_key, "keep_planning"),
                    "style": "primary" if selected_action == "keep_planning" else None,
                },
            ],
        },
    ]
    for element in blocks[1]["elements"]:
        if element.get("style") is None:
            element.pop("style", None)
    return text.replace("*", ""), blocks


def post_thread_plan_actions_message(client, channel, thread_ts, thread_key, session_id=None, footer_note=None):
    text, blocks = build_thread_plan_actions_message(
        thread_key,
        session_id=session_id,
        footer_note=footer_note,
    )
    return client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=text,
        blocks=blocks,
    )


def build_plan_runtime_summary(
    thread_key,
    *,
    planning_session_id,
    target_session_id,
    execution_mode,
    workdir,
    reasoning_effort,
    session_origin,
):
    lines = [
        f"- slack_thread_key: `{thread_key}`",
        f"- planning_session_id: `{planning_session_id or '-'}`",
        f"- target_session_id: `{target_session_id or '-'}`",
        f"- execution_mode: `{execution_mode}`",
        f"- workdir: `{workdir}`",
        f"- reasoning_effort: `{reasoning_effort or '-'}`",
        f"- session_origin: `{session_origin or '-'}`",
        f"- collaboration_mode_for_followup_turns: `{COLLABORATION_MODE_DEFAULT}`",
    ]
    return "\n".join(lines)


def build_plan_refinement_prompt(plan_text):
    return (
        "请继续细化这份已批准方案，并输出一版更新后的 `<proposed_plan>`。\n\n"
        "要求：\n"
        "- 继续使用中文\n"
        "- 先不要开始实施\n"
        "- 结合当前 thread 里已经出现的补充、约束或新信息继续细化\n"
        "- 即使总体方向不变，也请输出一版完整更新后的 `<proposed_plan>`，不要只给口头说明\n"
        "- 如果是在正式给方案，就直接输出完整 `<proposed_plan>...</proposed_plan>`\n\n"
        "Current approved plan:\n"
        f"{plan_text}"
    )


def build_plan_mode_prompt(prompt):
    normalized_prompt = str(prompt or "").strip()
    if not normalized_prompt:
        return normalized_prompt
    return (
        f"{normalized_prompt}\n\n"
        "如果这次回复是在正式给方案，请直接输出完整 `<proposed_plan>...</proposed_plan>`。"
    )


def build_plan_implementation_prompt(
    plan_text,
    *,
    thread_key,
    planning_session_id,
    target_session_id,
    execution_mode,
    workdir,
    reasoning_effort,
    session_origin,
):
    clean_note = (
        "这是一个新的实现 session。不要假设你自动继承了之前 planning session 的完整上下文；"
        "只依据下面提供的 plan 和运行摘要开始执行。"
        if execution_mode == "clean"
        else "继续在当前 session 中执行，但从现在开始应把下面的 plan 视为已批准的实施合同。"
    )
    return (
        "请开始实施这份已经批准的方案。\n\n"
        "要求：\n"
        "- 默认直接开始执行，不要再重复规划\n"
        "- 如遇到真正阻塞，再提出最小必要澄清\n"
        "- 继续使用中文\n\n"
        f"{clean_note}\n\n"
        "Approved Plan:\n"
        f"{plan_text}\n\n"
        "Runtime Summary:\n"
        f"{build_plan_runtime_summary(thread_key, planning_session_id=planning_session_id, target_session_id=target_session_id, execution_mode=execution_mode, workdir=workdir, reasoning_effort=reasoning_effort, session_origin=session_origin)}"
    )


def build_request_user_input_action_value(token):
    return json.dumps(
        {"token": str(token or "").strip()},
        ensure_ascii=True,
        separators=(",", ":"),
    )


def decode_request_user_input_action_value(raw_value):
    payload = json.loads(str(raw_value or ""))
    if not isinstance(payload, dict):
        raise RuntimeError("request_user_input payload invalid")
    token = str(payload.get("token") or "").strip()
    if not token:
        raise RuntimeError("request_user_input payload incomplete")
    return token


def register_pending_user_input_request(pending_request):
    with PENDING_USER_INPUT_REQUESTS_GUARD:
        PENDING_USER_INPUT_REQUESTS[pending_request.token] = pending_request


def get_pending_user_input_request(token):
    with PENDING_USER_INPUT_REQUESTS_GUARD:
        return PENDING_USER_INPUT_REQUESTS.get(token)


def pop_pending_user_input_request(token):
    with PENDING_USER_INPUT_REQUESTS_GUARD:
        return PENDING_USER_INPUT_REQUESTS.pop(token, None)


def set_pending_user_input_prompt_message_ts(token, message_ts):
    with PENDING_USER_INPUT_REQUESTS_GUARD:
        pending = PENDING_USER_INPUT_REQUESTS.get(token)
        if not pending:
            return
        pending.prompt_message_ts = str(message_ts or "").strip() or None


def resolve_pending_user_input_request(token, response_payload):
    pending = get_pending_user_input_request(token)
    if not pending or pending.future.done():
        return False
    pending.future.set_result(response_payload)
    return True


def build_request_user_input_prompt_summary(question):
    lines = [f"*{question.header}*", question.question]
    if question.options:
        option_labels = [option.label for option in question.options]
        if question.is_other:
            option_labels.append("Other")
        lines.append("Options: " + " | ".join(option_labels))
    return "\n".join(lines)


def build_request_user_input_prompt_text(pending_request):
    count = len(pending_request.request.questions)
    header = f"Codex 需要你补充 {count} 个输入。"
    lines = [header, ""]
    for question in pending_request.request.questions:
        lines.append(build_request_user_input_prompt_summary(question))
        lines.append("")
    lines.append("点击 `Respond` 打开填写面板；如果这轮不继续，也可以点 `Cancel`。")
    return "\n".join(lines).strip()


def build_request_user_input_prompt_blocks(pending_request):
    summary = "\n\n".join(
        build_request_user_input_prompt_summary(question)
        for question in pending_request.request.questions
    ).strip()
    summary_text = summary or "Codex 正在等待你的补充输入。"
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": summary_text},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": REQUEST_USER_INPUT_OPEN_ACTION,
                    "text": {"type": "plain_text", "text": "Respond"},
                    "style": "primary",
                    "value": build_request_user_input_action_value(pending_request.token),
                },
                {
                    "type": "button",
                    "action_id": REQUEST_USER_INPUT_CANCEL_ACTION,
                    "text": {"type": "plain_text", "text": "Cancel"},
                    "value": build_request_user_input_action_value(pending_request.token),
                },
            ],
        },
    ]


def get_request_user_input_choice_block_id(index):
    return f"rui_choice_{index}"


def get_request_user_input_other_block_id(index):
    return f"rui_other_{index}"


def get_request_user_input_text_block_id(index):
    return f"rui_text_{index}"


def build_request_user_input_modal(pending_request):
    blocks = []
    for index, question in enumerate(pending_request.request.questions):
        if question.options:
            option_entries = [
                {
                    "text": {"type": "plain_text", "text": option.label[:75]},
                    "description": {"type": "plain_text", "text": option.description[:75]},
                    "value": str(option_index),
                }
                for option_index, option in enumerate(question.options)
            ]
            if question.is_other:
                option_entries.append(
                    {
                        "text": {"type": "plain_text", "text": "Other"},
                        "description": {
                            "type": "plain_text",
                            "text": "Provide a custom response.",
                        },
                        "value": REQUEST_USER_INPUT_OTHER_VALUE,
                    }
                )
            blocks.append(
                {
                    "type": "input",
                    "block_id": get_request_user_input_choice_block_id(index),
                    "label": {"type": "plain_text", "text": question.header[:2000]},
                    "element": {
                        "type": "radio_buttons",
                        "action_id": "choice",
                        "options": option_entries,
                    },
                    "hint": {"type": "plain_text", "text": question.question[:2000]},
                }
            )
            if question.is_other:
                blocks.append(
                    {
                        "type": "input",
                        "block_id": get_request_user_input_other_block_id(index),
                        "optional": True,
                        "label": {"type": "plain_text", "text": f"{question.header[:120]} (Other)"},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "other_text",
                            "multiline": False,
                        },
                    }
                )
            continue

        blocks.append(
            {
                "type": "input",
                "block_id": get_request_user_input_text_block_id(index),
                "label": {"type": "plain_text", "text": question.header[:2000]},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "answer",
                    "multiline": False,
                },
                "hint": {"type": "plain_text", "text": question.question[:2000]},
            }
        )

    return {
        "type": "modal",
        "callback_id": REQUEST_USER_INPUT_SUBMIT_CALLBACK,
        "private_metadata": build_request_user_input_action_value(pending_request.token),
        "title": {"type": "plain_text", "text": "Codex Input"},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": blocks,
    }


def extract_view_selected_option_value(view_state, block_id, action_id):
    values = ((view_state or {}).get("values") or {})
    block = values.get(block_id) or {}
    action = block.get(action_id) or {}
    selected_option = action.get("selected_option") or {}
    return str(selected_option.get("value") or "").strip()


def extract_request_user_input_submission(view_state, pending_request):
    errors = {}
    answers = {}

    for index, question in enumerate(pending_request.request.questions):
        if question.options:
            selected_value = extract_view_selected_option_value(
                view_state,
                get_request_user_input_choice_block_id(index),
                "choice",
            )
            if not selected_value:
                errors[get_request_user_input_choice_block_id(index)] = "请选择一个选项。"
                continue
            if selected_value == REQUEST_USER_INPUT_OTHER_VALUE:
                other_text = extract_view_state_value(
                    view_state,
                    get_request_user_input_other_block_id(index),
                    "other_text",
                )
                if not other_text:
                    errors[get_request_user_input_other_block_id(index)] = "请填写 Other 的内容。"
                    continue
                answers[question.id] = {"answers": [other_text]}
                continue
            try:
                selected_index = int(selected_value)
                selected_option = question.options[selected_index]
            except (IndexError, TypeError, ValueError):
                errors[get_request_user_input_choice_block_id(index)] = "选项已失效，请重新选择。"
                continue
            answers[question.id] = {"answers": [selected_option.label]}
            continue

        text_value = extract_view_state_value(
            view_state,
            get_request_user_input_text_block_id(index),
            "answer",
        )
        if not text_value:
            errors[get_request_user_input_text_block_id(index)] = "请填写回答内容。"
            continue
        answers[question.id] = {"answers": [text_value]}

    return {"answers": answers}, errors


def update_request_user_input_prompt_message(client, pending_request, status_text):
    if not pending_request.prompt_message_ts:
        return
    with suppress(Exception):
        client.chat_update(
            channel=pending_request.channel,
            ts=pending_request.prompt_message_ts,
            text=status_text,
            blocks=[
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": status_text},
                }
            ],
        )


async def prompt_slack_user_input_async(
    client,
    channel,
    thread_ts,
    thread_key,
    user_id,
    session_id,
    request,
):
    token = uuid.uuid4().hex
    pending_request = PendingSlackUserInputRequest(
        token=token,
        thread_key=thread_key,
        channel=channel,
        thread_ts=thread_ts,
        owner_user_id=user_id,
        session_id=session_id,
        request=request,
        future=concurrent.futures.Future(),
    )
    register_pending_user_input_request(pending_request)

    try:
        prompt_text = build_request_user_input_prompt_text(pending_request)
        response = client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=prompt_text,
            blocks=build_request_user_input_prompt_blocks(pending_request),
        )
        set_pending_user_input_prompt_message_ts(token, response.get("ts"))
    except Exception:
        pop_pending_user_input_request(token)
        return {"answers": {}}

    try:
        return await asyncio.wrap_future(pending_request.future)
    except Exception:
        return {"answers": {}}
    finally:
        pop_pending_user_input_request(token)


def parse_extra_arg_value(tokens, flag_name):
    normalized_flag = f"--{flag_name}"
    prefix = f"{normalized_flag}="
    for index, token in enumerate(tokens):
        if token == normalized_flag:
            if index + 1 < len(tokens):
                return tokens[index + 1]
            return None
        if token.startswith(prefix):
            return token[len(prefix) :]
    return None


def has_extra_arg_flag(tokens, flag_name):
    normalized_flag = f"--{flag_name}"
    return any(token == normalized_flag for token in tokens)


def resolve_runtime_policy_settings():
    _codex_bin, _model, _default_workdir, _timeout, configured_sandbox, extra_args, full_auto = get_codex_settings()
    tokens = shlex.split(extra_args) if extra_args else []
    effective_full_auto = full_auto or has_extra_arg_flag(tokens, "full-auto")

    parsed_sandbox = parse_extra_arg_value(tokens, "sandbox")
    parsed_approval_policy = parse_extra_arg_value(tokens, "approval-policy")
    sandbox = parsed_sandbox or configured_sandbox or None
    approval_policy = parsed_approval_policy

    if "--dangerously-bypass-approvals-and-sandbox" in tokens:
        return "danger-full-access", "never"
    if effective_full_auto:
        return "workspace-write", approval_policy or "on-request"
    return sandbox, approval_policy


def build_runtime_thread_config(workdir_override=None):
    _codex_bin, model, default_workdir, _timeout, _sandbox, _extra_args, _full_auto = get_codex_settings()
    sandbox, approval_policy = resolve_runtime_policy_settings()
    workdir = normalize_session_cwd(workdir_override) or default_workdir
    kwargs = {"cwd": workdir}
    if model:
        kwargs["model"] = model
    if sandbox:
        kwargs["sandbox"] = sandbox
    if approval_policy:
        kwargs["approval_policy"] = approval_policy
    return ThreadConfig(**kwargs)


def build_runtime_turn_overrides(reasoning_effort=None, workdir_override=None):
    kwargs = {}
    workdir = normalize_session_cwd(workdir_override)
    if workdir:
        kwargs["cwd"] = workdir
    if reasoning_effort:
        kwargs["effort"] = reasoning_effort
    if not kwargs:
        return None
    return TurnOverrides(**kwargs)


def build_runtime_input_items(prompt, image_paths=None):
    items = [{"type": "text", "text": prompt}]
    for image_path in image_paths or []:
        items.append({"type": "localImage", "path": str(image_path)})
    return items


def build_codex_exec_args(
    prompt,
    output_file,
    extra_cli_args=None,
    reasoning_effort=None,
    workdir_override=None,
    image_paths=None,
):
    codex_bin, model, default_workdir, timeout, sandbox, extra_args, full_auto = get_codex_settings()
    workdir = normalize_session_cwd(workdir_override) or default_workdir
    args = [
        "exec",
        "--model",
        model,
        "--color",
        "never",
        "--skip-git-repo-check",
        "--output-last-message",
        output_file,
        "--json",
    ]

    if sandbox:
        args.extend(["--sandbox", sandbox])

    if full_auto:
        args.append("--full-auto")

    if extra_args:
        args.extend(shlex.split(extra_args))

    if extra_cli_args:
        args.extend(extra_cli_args)

    args.extend(build_image_args(image_paths))
    args.extend(build_reasoning_effort_args(reasoning_effort))
    args.append(prompt)
    return codex_bin, args, timeout, workdir


def build_codex_resume_args(
    session_id,
    prompt,
    output_file,
    extra_cli_args=None,
    reasoning_effort=None,
    workdir_override=None,
    image_paths=None,
):
    codex_bin, model, default_workdir, timeout, _sandbox, extra_args, full_auto = get_codex_settings()
    workdir = normalize_session_cwd(workdir_override) or default_workdir
    args = [
        "exec",
        "resume",
        "--model",
        model,
        "--skip-git-repo-check",
        "--output-last-message",
        output_file,
        "--json",
    ]
    if full_auto:
        args.append("--full-auto")
    if extra_args:
        args.extend(shlex.split(extra_args))
    if extra_cli_args:
        args.extend(extra_cli_args)
    args.extend(build_image_args(image_paths))
    args.extend(build_reasoning_effort_args(reasoning_effort))
    args.extend([session_id, prompt])
    return codex_bin, args, timeout, workdir


def clean_codex_output(text):
    lines = (text or "").splitlines()
    filtered = []
    for line in lines:
        stripped = line.strip()
        lower = stripped.lower()

        if not stripped:
            filtered.append(line)
            continue

        if stripped.startswith("WARNING: proceeding, even though we could not update PATH:"):
            continue
        if lower.startswith(("thinking", "working", "running", "checking", "searching", "reading")):
            continue
        if lower.startswith(("tool call", "exec_command", "apply_patch", "function call", "response_item", "commentary")):
            continue
        filtered.append(line)

    cleaned = "\n".join(filtered).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


def format_elapsed_seconds(total_seconds):
    seconds = max(0, int(total_seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def read_output_file(path):
    output_path = Path(path)
    if not output_path.exists():
        return ""
    return output_path.read_text(encoding="utf-8").strip()


def build_handoff_prompt():
    return (
        "请基于当前这个 session 已有的对话上下文，生成一份简短的 handoff note，供用户在终端和 Slack 之间切换控制时直接接力。\n\n"
        "硬性要求:\n"
        "- 不要运行工具\n"
        "- 不要读取文件\n"
        "- 不要改代码\n"
        "- 只根据当前 session 里已经存在的上下文作答\n"
        "- 用中文回答\n"
        "- 尽量简短但信息完整\n\n"
        "输出格式严格使用下面这几个小节:\n"
        "Current Goal:\n"
        "Constraints:\n"
        "Working Dir:\n"
        "Session State:\n"
        "Suggested Next Message:\n\n"
        "如果某一项不明确，就明确写“不明确”，不要编造。"
    )


def build_recap_prompt():
    return (
        "请基于当前这个 session 已有的对话上下文，生成一份简短的进展 recap，方便用户在手机 Slack 上快速回顾最近状态。\n\n"
        "硬性要求:\n"
        "- 不要运行工具\n"
        "- 不要读取文件\n"
        "- 不要改代码\n"
        "- 只根据当前 session 里已经存在的上下文作答\n"
        "- 用中文回答\n"
        "- 尽量简短但信息完整\n\n"
        "输出格式严格使用下面这几个小节:\n"
        "Recent Progress:\n"
        "Current Constraints:\n"
        "Open Questions:\n"
        "Suggested Next Message:\n\n"
        "如果某一项不明确，就明确写“不明确”，不要编造。"
    )


def append_handoff_footer(text, session_id, workdir):
    base = (text or "").strip()
    footer = (
        "\n\nIn-Session Verify Command:\n"
        "如果你已经在目标 Codex 会话内部，可运行：\n"
        "`printenv CODEX_THREAD_ID && pwd`\n"
        f"Expected Session ID: `{session_id or '-'}`\n"
        f"Expected Workdir: `{workdir}`"
    )
    return (base + footer).strip()


def append_recap_footer(text, session_id):
    base = (text or "").strip()
    footer = f"\n\nCurrent Session ID: `{session_id or '-'}`"
    return (base + footer).strip()


def parse_sessions_payload(payload):
    normalized = (payload or "").strip()
    if not normalized:
        return False, None
    if normalized.lower() == "--all":
        return True, None

    match = re.match(r"^--cwd\s+(.+)$", normalized, re.IGNORECASE | re.DOTALL)
    if match:
        cwd = normalize_session_cwd(match.group(1))
        if not cwd:
            raise RuntimeError("`sessions --cwd` 后面需要一个目录路径。")
        return False, cwd

    raise RuntimeError("`sessions` 只支持空参数、`--all` 或 `--cwd <path>`。")


def get_recent_sessions_text(thread_key, current_session_id, *, cwd, include_all, heading):
    summaries = session_catalog.fetch_recent_thread_summaries(
        get_codex_app_server_config(),
        cwd=cwd,
        include_all=include_all,
    )
    session_catalog.cache_thread_summaries(SESSION_SELECTION_CACHE, thread_key, summaries)
    return session_catalog.format_thread_summaries(
        summaries,
        heading=heading,
        current_session_id=current_session_id,
    )


@dataclass
class CodexRunResult:
    session_id: Optional[str]
    text: str
    exit_code: Optional[int]
    raw_output: str
    final_output: str
    json_output: str
    cleaned_output: str
    timed_out: bool


class SessionIdTracker:
    def __init__(self, session_id=None):
        self._lock = threading.Lock()
        self._session_id = session_id

    def set(self, session_id):
        if not session_id:
            return
        with self._lock:
            self._session_id = session_id

    def get(self):
        with self._lock:
            return self._session_id


def parse_codex_json_event_line(line):
    stripped = (line or "").strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def process_codex_json_event(event, parsed_session_id, messages, session_id_tracker=None):
    if not isinstance(event, dict):
        return parsed_session_id

    event_type = event.get("type")
    if event_type == "thread.started":
        next_session_id = event.get("thread_id") or parsed_session_id
        if session_id_tracker and next_session_id:
            session_id_tracker.set(next_session_id)
        return next_session_id

    if event_type != "item.completed":
        return parsed_session_id

    item = event.get("item") or {}
    if item.get("type") != "agent_message":
        return parsed_session_id

    message_text = item.get("text")
    if message_text:
        messages.append(message_text)
    return parsed_session_id


def parse_codex_json_events(text):
    session_id = None
    messages = []

    for line in (text or "").splitlines():
        session_id = process_codex_json_event(parse_codex_json_event_line(line), session_id, messages)

    return session_id, "\n\n".join(messages).strip()


def build_codex_child_env():
    child_env = os.environ.copy()
    child_env.update(ENV)
    for key in list(child_env):
        if key.startswith("SLACK_"):
            child_env.pop(key, None)
    return child_env


def stream_codex_json_output(process, timeout, session_id_tracker=None):
    start_monotonic = time.monotonic()
    line_queue = queue.Queue()
    raw_lines = []
    parsed_session_id = session_id_tracker.get() if session_id_tracker else None
    agent_messages = []

    def reader():
        try:
            stdout = process.stdout
            if stdout is None:
                return
            for line in stdout:
                line_queue.put(line)
        finally:
            line_queue.put(None)

    def ingest_queue_line(line):
        nonlocal parsed_session_id
        raw_lines.append(line)
        parsed_session_id = process_codex_json_event(
            parse_codex_json_event_line(line),
            parsed_session_id,
            agent_messages,
            session_id_tracker=session_id_tracker,
        )

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()

    timed_out = False
    try:
        while True:
            try:
                line = line_queue.get(timeout=1)
            except queue.Empty:
                if timeout and timeout > 0 and (time.monotonic() - start_monotonic) >= timeout:
                    timed_out = True
                    with suppress(Exception):
                        process.kill()
                    break
                if process.poll() is not None and not reader_thread.is_alive():
                    break
                continue

            if line is None:
                break
            ingest_queue_line(line)
    finally:
        if timed_out:
            with suppress(Exception):
                process.wait(timeout=5)
        reader_thread.join(timeout=5)
        with suppress(Exception):
            if process.stdout is not None:
                process.stdout.close()

    while True:
        try:
            line = line_queue.get_nowait()
        except queue.Empty:
            break
        if line is None:
            continue
        ingest_queue_line(line)

    json_output = "\n\n".join(agent_messages).strip()
    return "".join(raw_lines), parsed_session_id, json_output, timed_out


def run_codex(
    prompt,
    session_id=None,
    session_id_tracker=None,
    reasoning_effort=None,
    workdir_override=None,
    image_paths=None,
):
    with tempfile.NamedTemporaryFile(prefix="codex-last-message-", suffix=".txt", delete=False) as tmp:
        output_file = tmp.name

    try:
        mode = "resume" if session_id else "new"
        if session_id:
            codex_bin, args, timeout, workdir = build_codex_resume_args(
                session_id,
                prompt,
                output_file,
                reasoning_effort=reasoning_effort,
                workdir_override=workdir_override,
                image_paths=image_paths,
            )
            log_codex_command(mode, workdir, [codex_bin, *args])
        else:
            codex_bin, args, timeout, workdir = build_codex_exec_args(
                prompt,
                output_file,
                reasoning_effort=reasoning_effort,
                workdir_override=workdir_override,
                image_paths=image_paths,
            )
            log_codex_command(mode, workdir, [codex_bin, *args])

        if session_id_tracker and session_id:
            session_id_tracker.set(session_id)

        process = subprocess.Popen(
            [codex_bin, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=build_codex_child_env(),
            cwd=workdir,
        )

        raw_output, parsed_session_id, json_output, timed_out = stream_codex_json_output(
            process,
            timeout,
            session_id_tracker=session_id_tracker,
        )
        if timed_out:
            return CodexRunResult(
                session_id=(session_id_tracker.get() if session_id_tracker else None) or session_id,
                text=f"Codex 运行超时：已超过 {timeout} 秒。",
                exit_code=None,
                raw_output=raw_output,
                final_output="",
                json_output=json_output,
                cleaned_output=clean_codex_output(raw_output),
                timed_out=True,
            )

        final_output = read_output_file(output_file)
        exit_code = process.wait()
        effective_session_id = parsed_session_id or session_id
        if session_id:
            if parsed_session_id and parsed_session_id != session_id:
                print(
                    "[session_drift]"
                    f" requested_session_id={session_id}"
                    f" parsed_session_id={parsed_session_id}"
                    " action=preserve_requested",
                    flush=True,
                )
            effective_session_id = session_id
        elif session_id_tracker:
            effective_session_id = session_id_tracker.get() or effective_session_id
        cleaned_output = clean_codex_output(raw_output)
        log_codex_result(mode, exit_code, raw_output, final_output)

        if exit_code not in (0, None):
            if final_output:
                result_text = final_output
            else:
                fallback_output = json_output or cleaned_output
                result_text = f"Codex 进程异常退出，exit code={exit_code}。\n\n{fallback_output}".strip()
        else:
            result_text = (
                final_output
                or json_output
                or cleaned_output
                or build_empty_final_response_text(effective_session_id)
            )

        return CodexRunResult(
            session_id=effective_session_id,
            text=result_text,
            exit_code=exit_code,
            raw_output=raw_output,
            final_output=final_output,
            json_output=json_output,
            cleaned_output=cleaned_output,
            timed_out=False,
        )
    finally:
        Path(output_file).unlink(missing_ok=True)


def is_invalid_session_result(text):
    normalized = (text or "").lower()
    patterns = [
        "session not found",
        "thread not found",
        "invalid thread",
        "invalid session",
        "could not find thread",
        "no conversation found",
        "failed to resume",
        "resume failed",
    ]
    return any(pattern in normalized for pattern in patterns)


def should_rebuild_invalid_session(result):
    if result.exit_code in (0, None):
        return False

    candidates = [
        result.text,
        result.raw_output,
        result.final_output,
        result.json_output,
        result.cleaned_output,
    ]
    return any(is_invalid_session_result(candidate) for candidate in candidates if candidate)


def should_update_session_activity(result):
    return bool(result.session_id) and not result.timed_out


def make_thread_key(channel, thread_ts):
    return f"{channel}:{thread_ts}"


def parse_thread_key(thread_key):
    normalized = str(thread_key or "").strip()
    if not normalized or ":" not in normalized:
        return None, None
    channel, thread_ts = normalized.split(":", 1)
    channel = str(channel or "").strip()
    thread_ts = str(thread_ts or "").strip()
    if not channel or not thread_ts:
        return None, None
    return channel, thread_ts


def claim_thread_lock(thread_key):
    with THREAD_LOCKS_GUARD:
        state = THREAD_LOCKS.get(thread_key)
        if state is None:
            state = ThreadLockState(lock=threading.Lock())
            THREAD_LOCKS[thread_key] = state
        state.waiters += 1
        return state.lock


def release_thread_lock(thread_key):
    with THREAD_LOCKS_GUARD:
        state = THREAD_LOCKS.get(thread_key)
        if state is None:
            return
        state.waiters = max(0, state.waiters - 1)
        if state.waiters == 0:
            THREAD_LOCKS.pop(thread_key, None)


def claim_session_lock(session_id):
    with SESSION_LOCKS_GUARD:
        state = SESSION_LOCKS.get(session_id)
        if state is None:
            state = ThreadLockState(lock=threading.Lock())
            SESSION_LOCKS[session_id] = state
        state.waiters += 1
        return state.lock


def release_session_lock(session_id):
    with SESSION_LOCKS_GUARD:
        state = SESSION_LOCKS.get(session_id)
        if state is None:
            return
        state.waiters = max(0, state.waiters - 1)
        if state.waiters == 0:
            SESSION_LOCKS.pop(session_id, None)


@contextmanager
def session_execution_guard(session_id):
    if not session_id:
        yield
        return

    lock = claim_session_lock(session_id)
    try:
        with lock:
            yield
    finally:
        release_session_lock(session_id)


def create_app_server_client():
    return thread_views.create_app_server_client(get_codex_app_server_config())


async def read_thread_response_async(session_id):
    return await thread_views.read_thread_response_async(
        get_codex_app_server_config(),
        session_id,
    )


def read_thread_response(session_id, include_turns=True):
    return thread_views.read_thread_response(
        get_codex_app_server_config(),
        session_id,
        include_turns=include_turns,
    )


def format_user_input(user_input):
    return thread_views.format_user_input(user_input)


def format_user_message_content(content_items):
    return thread_views.format_user_message_content(content_items)


def is_final_answer_phase(phase):
    return thread_views.is_final_answer_phase(phase)


def is_progress_phase(phase):
    return thread_views.is_progress_phase(phase)


def extract_conversation_events(thread_read_response):
    return thread_views.extract_conversation_events(thread_read_response)


def extract_progress_events(thread_read_response):
    return thread_views.extract_progress_events(thread_read_response)


def read_conversation_events(session_id):
    return extract_conversation_events(read_thread_response(session_id))


def get_event_key(event):
    return thread_views.get_event_key(event)


def get_recent_turn_events(events):
    return thread_views.get_recent_turn_events(events)


def get_latest_completed_turn_events(events):
    return thread_views.get_latest_completed_turn_events(events)


def get_events_after_key(events, last_key):
    return thread_views.get_events_after_key(events, last_key)


def format_conversation_events(events, heading=None):
    return thread_views.format_conversation_events(events, heading=heading)


def build_watch_bootstrap(session_id):
    events = read_conversation_events(session_id)
    bootstrap_events = get_latest_completed_turn_events(events) or get_recent_turn_events(events)
    last_event_key = get_event_key(bootstrap_events[-1]) if bootstrap_events else None
    text = format_conversation_events(bootstrap_events, heading="最近一轮对话:")
    return maybe_prefix_thread_output(session_id, text), last_event_key


def advance_watch_cursor(events, last_event_key, session_id=None):
    try:
        new_events = get_events_after_key(events, last_event_key)
    except WatchAnchorLostError:
        rebased_events = get_latest_completed_turn_events(events) or get_recent_turn_events(events)
        new_last_event_key = get_event_key(rebased_events[-1]) if rebased_events else None
        return None, new_last_event_key, True

    if not new_events:
        return None, last_event_key, False

    new_last_event_key = get_event_key(new_events[-1])
    return maybe_prefix_thread_output(session_id, format_conversation_events(new_events)), new_last_event_key, False


def capture_progress_baseline(session_id):
    try:
        progress_events = extract_progress_events(read_thread_response(session_id))
    except Exception:
        return {}
    return {event.item_id: event.text for event in progress_events}


def format_progress_message(text):
    return thread_views.format_progress_message(text)


def build_progress_messages(progress_events, previous_text_by_item_id):
    return thread_views.build_progress_messages(progress_events, previous_text_by_item_id)


def maybe_post_progress_updates(client, channel, thread_ts, session_id, previous_text_by_item_id):
    try:
        progress_events = extract_progress_events(read_thread_response(session_id))
    except Exception:
        return

    for message in build_progress_messages(progress_events, previous_text_by_item_id):
        post_chunks(client, channel, thread_ts, message)


def create_progress_reporter(client, channel, thread_ts, batch_seconds=None):
    return AsyncProgressReporter(
        client,
        channel,
        thread_ts,
        batch_seconds=batch_seconds,
    )


def get_runtime_active_turn(session_id):
    if not session_id:
        return None
    return get_app_runtime().get_active_turn(session_id)


def get_effective_session_mode(thread_key, session_id=None, session_mode=None, active_record=None):
    normalized_mode = session_mode if session_mode in {SESSION_MODE_OBSERVE, SESSION_MODE_CONTROL} else None
    if normalized_mode:
        return normalized_mode
    effective_active_record = active_record if active_record is not None else ACTIVE_TURN_REGISTRY.get_for_thread(thread_key)
    effective_session_id = session_id or (effective_active_record.session_id if effective_active_record else None)
    if effective_session_id and effective_active_record and effective_active_record.session_id == effective_session_id:
        return SESSION_MODE_CONTROL
    return None


def build_runtime_turn_unavailable_message(current_session_id):
    session_hint = f"session `{current_session_id}`" if current_session_id else "当前 session"
    return (
        f"{session_hint} 当前没有由 codex-slack runtime 持有的活跃 turn。"
        " 这通常表示上一轮已经结束。"
        " 如果你想继续，请直接发送普通消息开始下一轮；只有 turn 仍在运行时，`steer` / `interrupt` 才会生效。"
        " 终端里已经在运行的 turn 目前只能 `watch`。"
    )


def run_runtime_turn_with_updates(
    client,
    channel,
    thread_ts,
    thread_key,
    prompt,
    session_id=None,
    enable_progress=False,
    reasoning_effort=None,
    workdir_override=None,
    image_paths=None,
    owner_user_id=None,
    session_origin=SESSION_ORIGIN_SLACK,
    collaboration_mode=None,
    track_active_turn=True,
    persist_session_binding=True,
):
    runtime = get_app_runtime()
    session_id_tracker = SessionIdTracker(session_id=session_id)
    heartbeat_seconds = get_progress_heartbeat_seconds() if enable_progress else None
    progress_state = {"baseline": {}}
    progress_reporter = create_progress_reporter(client, channel, thread_ts) if enable_progress else None
    runtime_collaboration_mode = build_runtime_collaboration_mode_payload(
        collaboration_mode,
        reasoning_effort=reasoning_effort,
    )

    def on_turn_started(started_session_id, turn_id):
        session_id_tracker.set(started_session_id)
        if track_active_turn:
            ACTIVE_TURN_REGISTRY.set(thread_key, started_session_id, turn_id)
        if persist_session_binding:
            SESSION_STORE.set(
                thread_key,
                started_session_id,
                owner_user_id=owner_user_id,
                session_origin=session_origin,
                session_cwd=workdir_override,
            )
        with suppress(Exception):
            latest_event_key = get_latest_event_key_for_session(started_session_id)
            if latest_event_key:
                SESSION_STORE.set_watch_last_event_key(
                    thread_key,
                    started_session_id,
                    latest_event_key,
                    owner_user_id=owner_user_id,
                )
    def on_step(step):
        current_session_id = session_id_tracker.get() or session_id
        if current_session_id and step.turn_id and step.item_id:
            with suppress(Exception):
                SESSION_STORE.set_watch_last_event_key(
                    thread_key,
                    current_session_id,
                    (step.turn_id, step.item_id),
                    owner_user_id=owner_user_id,
                )
        if not enable_progress or not step.text or step.item_type != "agentMessage":
            return
        item = step.data.get("item") if isinstance(step.data, dict) else None
        phase = item.get("phase") if isinstance(item, dict) else None
        if not is_progress_phase(phase):
            return
        event = ProgressEvent(
            turn_id=step.turn_id,
            item_id=step.item_id or f"{step.turn_id}:progress",
            phase=str(phase),
            text=step.text,
        )
        for message in build_progress_messages([event], progress_state["baseline"]):
            progress_reporter.enqueue(message)

    def on_heartbeat(current_session_id, _turn_id, elapsed_seconds):
        if not progress_reporter:
            return
        progress_reporter.enqueue(
            f"仍在运行，已持续 {format_elapsed_seconds(elapsed_seconds)}。"
            f" session `{current_session_id}`"
        )

    async def on_user_input_request(request):
        return await prompt_slack_user_input_async(
            client,
            channel,
            thread_ts,
            thread_key,
            owner_user_id or "",
            session_id_tracker.get() or session_id,
            request,
        )

    try:
        runtime_result = runtime.run_turn(
            session_id=session_id,
            input_items=build_runtime_input_items(prompt, image_paths=image_paths),
            thread_config=build_runtime_thread_config(workdir_override=workdir_override),
            turn_overrides=build_runtime_turn_overrides(
                reasoning_effort=reasoning_effort,
                workdir_override=workdir_override,
            ),
            collaboration_mode=runtime_collaboration_mode,
            heartbeat_seconds=heartbeat_seconds,
            on_turn_started=on_turn_started,
            on_step=on_step,
            on_heartbeat=on_heartbeat,
            on_user_input_request=on_user_input_request,
        )
    except Exception as exc:
        if session_id and is_invalid_session_result(str(exc)):
            message = str(exc)
            return CodexRunResult(
                session_id=session_id_tracker.get() or session_id,
                text=message,
                exit_code=1,
                raw_output=message,
                final_output="",
                json_output="",
                cleaned_output=message,
                timed_out=False,
            )
        raise
    finally:
        if track_active_turn:
            ACTIVE_TURN_REGISTRY.clear_for_thread(thread_key)
        if progress_reporter:
            progress_reporter.flush()
            progress_reporter.close()

    effective_session_id = session_id_tracker.get() or runtime_result.session_id
    final_text = (runtime_result.final_text or "").strip() or build_empty_final_response_text(
        effective_session_id
    )
    raw_output = "\n\n".join(step.text for step in runtime_result.steps if step.text).strip()
    return CodexRunResult(
        session_id=effective_session_id,
        text=final_text,
        exit_code=0,
        raw_output=raw_output,
        final_output=final_text,
        json_output="",
        cleaned_output=final_text,
        timed_out=False,
    )


def execute_plan_implementation_action(
    client,
    channel,
    thread_ts,
    thread_key,
    *,
    user_id,
    execution_mode,
):
    current_session_id = SESSION_STORE.get(thread_key)
    current_session_origin = get_session_origin(thread_key)
    current_session_cwd = get_session_cwd(thread_key)
    planning_session_id = SESSION_STORE.get_latest_plan_session_id(thread_key) or current_session_id
    latest_plan_text = SESSION_STORE.get_latest_plan(thread_key)
    if not latest_plan_text:
        raise RuntimeError("当前 thread 还没有可实施的 `<proposed_plan>`。请先让 Codex 产出一份方案。")

    run_workdir = resolve_workdir(
        thread_key,
        session_id=current_session_id,
        session_cwd=current_session_cwd,
    )
    reasoning_effort, _effort_source = resolve_reasoning_effort(
        thread_key,
        session_id=current_session_id,
        session_origin=current_session_origin,
    )
    SESSION_STORE.set_collaboration_mode(
        thread_key,
        COLLABORATION_MODE_DEFAULT,
        owner_user_id=user_id,
    )
    SESSION_STORE.set_mode(thread_key, SESSION_MODE_CONTROL)
    stop_watcher(thread_key)

    target_session_id = current_session_id if execution_mode == "here" else None
    prompt = build_plan_implementation_prompt(
        latest_plan_text,
        thread_key=thread_key,
        planning_session_id=planning_session_id,
        target_session_id=target_session_id,
        execution_mode=execution_mode,
        workdir=run_workdir,
        reasoning_effort=reasoning_effort,
        session_origin=current_session_origin or SESSION_ORIGIN_SLACK,
    )
    with session_execution_guard(target_session_id or planning_session_id):
        codex_result = run_runtime_turn_with_updates(
            client,
            channel,
            thread_ts,
            thread_key,
            prompt,
            session_id=target_session_id,
            enable_progress=resolve_progress_updates(thread_key)[0],
            reasoning_effort=reasoning_effort,
            workdir_override=run_workdir,
            owner_user_id=user_id,
            session_origin=SESSION_ORIGIN_SLACK,
            collaboration_mode=COLLABORATION_MODE_DEFAULT,
        )

    next_session_id = codex_result.session_id
    SESSION_STORE.set(
        thread_key,
        next_session_id,
        owner_user_id=user_id,
        session_origin=SESSION_ORIGIN_SLACK,
        session_cwd=run_workdir,
    )
    SESSION_STORE.mark_plan_implemented(
        thread_key,
        execution_mode=execution_mode,
        execution_session_id=next_session_id,
        owner_user_id=user_id,
    )
    return codex_result, {
        "planning_session_id": planning_session_id,
        "next_session_id": next_session_id,
        "workdir": run_workdir,
        "reasoning_effort": reasoning_effort,
    }


def execute_plan_continue_planning_action(
    client,
    channel,
    thread_ts,
    thread_key,
    *,
    user_id,
):
    current_session_id = SESSION_STORE.get(thread_key)
    current_session_origin = get_session_origin(thread_key)
    current_session_cwd = get_session_cwd(thread_key)
    latest_plan_text = SESSION_STORE.get_latest_plan(thread_key)
    if not current_session_id:
        raise RuntimeError("当前 thread 没有可继续规划的 session。请先发送一条普通消息重新建立 planning session。")
    if not latest_plan_text:
        raise RuntimeError("当前 thread 还没有可继续细化的 `<proposed_plan>`。")

    run_workdir = resolve_workdir(
        thread_key,
        session_id=current_session_id,
        session_cwd=current_session_cwd,
    )
    reasoning_effort, _effort_source = resolve_reasoning_effort(
        thread_key,
        session_id=current_session_id,
        session_origin=current_session_origin,
    )
    SESSION_STORE.set_collaboration_mode(
        thread_key,
        COLLABORATION_MODE_PLAN,
        owner_user_id=user_id,
    )
    SESSION_STORE.set_mode(thread_key, SESSION_MODE_CONTROL)
    prompt = build_plan_refinement_prompt(latest_plan_text)
    with session_execution_guard(current_session_id):
        codex_result = run_runtime_turn_with_updates(
            client,
            channel,
            thread_ts,
            thread_key,
            prompt,
            session_id=current_session_id,
            enable_progress=resolve_progress_updates(thread_key)[0],
            reasoning_effort=reasoning_effort,
            workdir_override=run_workdir,
            owner_user_id=user_id,
            session_origin=current_session_origin or SESSION_ORIGIN_SLACK,
            collaboration_mode=COLLABORATION_MODE_PLAN,
        )
    next_session_id = codex_result.session_id
    SESSION_STORE.set(
        thread_key,
        next_session_id,
        owner_user_id=user_id,
        session_origin=current_session_origin or SESSION_ORIGIN_SLACK,
        session_cwd=run_workdir,
    )
    return codex_result, {
        "next_session_id": next_session_id,
        "workdir": run_workdir,
    }


def handle_keep_planning_action(client, channel_id, thread_ts, thread_key, *, user_id, logger):
    client.chat_postMessage(
        channel=channel_id,
        thread_ts=thread_ts,
        text=f"<@{user_id}> 正在继续细化这份方案，并要求输出一版新的 `<proposed_plan>`，请稍等。",
    )
    try:
        codex_result, details = execute_plan_continue_planning_action(
            client,
            channel_id,
            thread_ts,
            thread_key,
            user_id=user_id,
        )
    except Exception as exc:
        runtime_diagnostics = ""
        if should_reset_runtime_after_exception(exc):
            with suppress(Exception):
                runtime = get_app_runtime()
                runtime.reset()
                runtime_diagnostics = runtime.last_client_diagnostics()
        client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=build_process_error_message(user_id, exc, diagnostics=runtime_diagnostics),
        )
        logger.exception(
            "Continue-planning action failed for %s thread %s: %r",
            user_id,
            thread_key,
            exc,
        )
        return
    result = (
        "已继续在当前 planning session 中细化方案。\n\n"
        f"- planning_session_id: `{details['next_session_id']}`\n"
        f"- workdir: `{details['workdir']}`\n\n"
        f"{codex_result.text}"
    )
    result = sanitize_plan_mode_response_for_slack(result)
    post_chunks(client, channel_id, thread_ts, result)
    if response_contains_proposed_plan(codex_result.text):
        persist_latest_proposed_plan(
            thread_key,
            codex_result.text,
            session_id=details["next_session_id"],
            owner_user_id=user_id,
        )
        post_thread_plan_actions_message(
            client,
            channel_id,
            thread_ts,
            thread_key,
            session_id=details["next_session_id"],
            footer_note="已基于当前讨论继续细化方案。",
        )


def run_codex_with_updates(
    client,
    channel,
    thread_ts,
    prompt,
    session_id=None,
    enable_progress=False,
    reasoning_effort=None,
    workdir_override=None,
    image_paths=None,
):
    result_box = {}
    error_box = {}
    session_id_tracker = SessionIdTracker(session_id=session_id)

    def worker():
        try:
            result_box["result"] = run_codex(
                prompt,
                session_id=session_id,
                session_id_tracker=session_id_tracker,
                reasoning_effort=reasoning_effort,
                workdir_override=workdir_override,
                image_paths=image_paths,
            )
        except Exception as exc:  # pragma: no cover
            error_box["error"] = exc

    worker_thread = threading.Thread(target=worker, daemon=True)
    worker_thread.start()

    start_monotonic = time.monotonic()
    last_heartbeat_at = start_monotonic
    last_progress_poll_at = start_monotonic
    heartbeat_seconds = get_progress_heartbeat_seconds()
    progress_poll_seconds = get_progress_poll_seconds()
    progress_session_id = session_id if (enable_progress and session_id) else None
    progress_baseline = capture_progress_baseline(progress_session_id) if progress_session_id else None

    while worker_thread.is_alive():
        worker_thread.join(timeout=1)
        if not worker_thread.is_alive():
            break

        now = time.monotonic()
        current_session_id = session_id_tracker.get()
        if enable_progress and current_session_id and progress_session_id != current_session_id:
            progress_session_id = current_session_id
            # For brand-new sessions there is no earlier turn state to suppress,
            # so start from an empty baseline and let the first poll emit current progress.
            progress_baseline = (
                capture_progress_baseline(current_session_id) if session_id else {}
            )
            last_progress_poll_at = 0

        if enable_progress and progress_session_id and progress_baseline is not None:
            if (now - last_progress_poll_at) >= progress_poll_seconds:
                maybe_post_progress_updates(client, channel, thread_ts, progress_session_id, progress_baseline)
                last_progress_poll_at = now

        if (now - last_heartbeat_at) >= heartbeat_seconds:
            session_hint = f" session `{current_session_id}`" if current_session_id else ""
            post_chunks(
                client,
                channel,
                thread_ts,
                f"仍在运行，已持续 {format_elapsed_seconds(now - start_monotonic)}。{session_hint}".strip(),
            )
            last_heartbeat_at = now

    if error_box:
        raise error_box["error"]
    return result_box["result"]


def post_chunks(client, channel, thread_ts, text):
    for chunk in chunk_text(text):
        print(
            "[slack_post]"
            f" channel={channel}"
            f" thread_ts={thread_ts}"
            f" length={len(chunk)}",
            flush=True,
        )
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=chunk)


def get_watcher(thread_key):
    with WATCHERS_GUARD:
        return WATCHERS.get(thread_key)


def clear_watcher(thread_key, watcher):
    with WATCHERS_GUARD:
        if WATCHERS.get(thread_key) is watcher:
            WATCHERS.pop(thread_key, None)


def stop_watcher(thread_key, *, clear_watch_enabled=True):
    with WATCHERS_GUARD:
        watcher = WATCHERS.pop(thread_key, None)
    if watcher is None:
        if clear_watch_enabled:
            SESSION_STORE.set_watch_enabled(thread_key, False)
        return False
    watcher.stop_event.set()
    if clear_watch_enabled:
        SESSION_STORE.set_watch_enabled(thread_key, False)
    return True


async def _read_thread_response_with_client(client, session_id, *, include_turns):
    return await client.read_thread(session_id, include_turns=include_turns)


def _watch_transport_error_message(notification):
    if not isinstance(notification, dict):
        return None
    if notification.get("method") != "__transport_error__":
        return None
    params = notification.get("params")
    if not isinstance(params, dict):
        return "receiver loop failed"
    message = str(params.get("message") or "").strip()
    return message or "receiver loop failed"


def _parse_fs_watch_response(response):
    if not isinstance(response, dict):
        return None, None
    watch_id = str(response.get("watchId") or "").strip()
    path = str(response.get("path") or "").strip()
    return watch_id or None, path or None


async def _start_fs_watch(client, path):
    response = await client.request("fs/watch", {"path": path})
    return _parse_fs_watch_response(response)


async def _stop_fs_watch(client, watch_id):
    if not watch_id:
        return
    with suppress(Exception):
        await client.request("fs/unwatch", {"watchId": watch_id})


async def _drain_fs_changed_notifications(client, watch_id, debounce_seconds):
    deadline = time.monotonic() + max(0.05, debounce_seconds)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            notification = await asyncio.wait_for(client._notifications.get(), timeout=remaining)
        except asyncio.TimeoutError:
            return

        error_message = _watch_transport_error_message(notification)
        if error_message:
            raise RuntimeError(f"watch transport failed: {error_message}")

        if not isinstance(notification, dict):
            continue
        if notification.get("method") != "fs/changed":
            continue
        params = notification.get("params")
        if not isinstance(params, dict):
            continue
        if str(params.get("watchId") or "").strip() != watch_id:
            continue
        deadline = time.monotonic() + max(0.05, debounce_seconds)


async def _wait_for_watch_signal(client, watch_id, stop_event, timeout_seconds, debounce_seconds):
    wait_timeout = max(0.1, float(timeout_seconds))
    while not stop_event.is_set():
        if not watch_id:
            await asyncio.to_thread(stop_event.wait, wait_timeout)
            return "poll"

        try:
            notification = await asyncio.wait_for(client._notifications.get(), timeout=wait_timeout)
        except asyncio.TimeoutError:
            return "poll"

        error_message = _watch_transport_error_message(notification)
        if error_message:
            raise RuntimeError(f"watch transport failed: {error_message}")

        if not isinstance(notification, dict):
            continue
        if notification.get("method") != "fs/changed":
            continue
        params = notification.get("params")
        if not isinstance(params, dict):
            continue
        if str(params.get("watchId") or "").strip() != watch_id:
            continue

        await _drain_fs_changed_notifications(client, watch_id, debounce_seconds)
        return "fs_changed"

    return "stopped"


async def watch_loop_async(
    client,
    channel,
    thread_ts,
    thread_key,
    session_id,
    stop_event,
    last_event_key=None,
    stop_when_idle=False,
):
    failure_count = 0
    current_last_event_key = last_event_key
    poll_seconds = get_watch_poll_seconds()
    metadata_fallback_seconds = get_watch_metadata_fallback_seconds()
    debounce_seconds = get_watch_fs_debounce_seconds()
    watch_client = create_app_server_client()
    watch_id = None
    last_snapshot = None

    try:
        await watch_client.start()
        await watch_client.initialize()

        metadata_response = await _read_thread_response_with_client(
            watch_client,
            session_id,
            include_turns=False,
        )
        last_snapshot = extract_watch_thread_snapshot(metadata_response)
        if last_snapshot.path:
            try:
                watch_id, watched_path = await _start_fs_watch(watch_client, last_snapshot.path)
                if watched_path:
                    last_snapshot = WatchThreadSnapshot(
                        path=watched_path,
                        updated_at=last_snapshot.updated_at,
                        status_type=last_snapshot.status_type,
                    )
            except Exception:
                watch_id = None

        while not stop_event.is_set():
            if SESSION_STORE.get(thread_key) != session_id:
                break

            timeout_seconds = metadata_fallback_seconds if watch_id else poll_seconds
            signal = await _wait_for_watch_signal(
                watch_client,
                watch_id,
                stop_event,
                timeout_seconds,
                debounce_seconds,
            )
            if signal == "stopped":
                break
            if SESSION_STORE.get(thread_key) != session_id:
                break

            try:
                metadata_response = await _read_thread_response_with_client(
                    watch_client,
                    session_id,
                    include_turns=False,
                )
                failure_count = 0
            except Exception as exc:
                failure_count += 1
                if failure_count >= MAX_WATCH_READ_FAILURES:
                    post_chunks(
                        client,
                        channel,
                        thread_ts,
                        f"持续 watch 已停止：读取当前 thread 对话失败。\n\n{exc}",
                    )
                    break
                continue

            snapshot = extract_watch_thread_snapshot(metadata_response)
            path_changed = snapshot.path != (last_snapshot.path if last_snapshot else None)
            updated = snapshot.updated_at != (last_snapshot.updated_at if last_snapshot else None)
            status_changed = snapshot.status_type != (last_snapshot.status_type if last_snapshot else None)

            if path_changed:
                await _stop_fs_watch(watch_client, watch_id)
                watch_id = None
                if snapshot.path:
                    try:
                        watch_id, watched_path = await _start_fs_watch(watch_client, snapshot.path)
                        if watched_path:
                            snapshot = WatchThreadSnapshot(
                                path=watched_path,
                                updated_at=snapshot.updated_at,
                                status_type=snapshot.status_type,
                            )
                    except Exception:
                        watch_id = None

            if not (updated or status_changed or path_changed or signal == "fs_changed"):
                last_snapshot = snapshot
                continue

            try:
                thread_response = await _read_thread_response_with_client(
                    watch_client,
                    session_id,
                    include_turns=True,
                )
                failure_count = 0
            except Exception as exc:
                failure_count += 1
                if failure_count >= MAX_WATCH_READ_FAILURES:
                    post_chunks(
                        client,
                        channel,
                        thread_ts,
                        f"持续 watch 已停止：读取当前 thread 对话失败。\n\n{exc}",
                    )
                    break
                continue

            events = extract_conversation_events(thread_response)
            previous_last_event_key = current_last_event_key
            message, current_last_event_key, _rebased = advance_watch_cursor(
                events,
                current_last_event_key,
                session_id,
            )
            if current_last_event_key and current_last_event_key != previous_last_event_key:
                SESSION_STORE.set_watch_last_event_key(thread_key, session_id, current_last_event_key)
            last_snapshot = snapshot
            if not message:
                if stop_when_idle and snapshot.status_type != "active":
                    break
                continue
            post_chunks(client, channel, thread_ts, message)
            if stop_when_idle and snapshot.status_type != "active":
                break
    finally:
        await _stop_fs_watch(watch_client, watch_id)
        with suppress(Exception):
            await watch_client.close()


def watch_loop(
    client,
    channel,
    thread_ts,
    thread_key,
    session_id,
    stop_event,
    last_event_key=None,
    stop_when_idle=False,
):
    try:
        asyncio.run(
            watch_loop_async(
                client,
                channel,
                thread_ts,
                thread_key,
                session_id,
                stop_event,
                last_event_key=last_event_key,
                stop_when_idle=stop_when_idle,
            )
        )
    finally:
        watcher = get_watcher(thread_key)
        if watcher and watcher.stop_event is stop_event:
            clear_watcher(thread_key, watcher)


def start_watcher(
    client,
    channel,
    thread_ts,
    thread_key,
    session_id,
    last_event_key=None,
    *,
    persist_watch=True,
    stop_when_idle=False,
):
    stop_watcher(thread_key, clear_watch_enabled=False)
    stop_event = threading.Event()
    thread = threading.Thread(
        target=watch_loop,
        args=(client, channel, thread_ts, thread_key, session_id, stop_event, last_event_key, stop_when_idle),
        daemon=True,
    )
    watcher = WatchHandle(
        thread=thread,
        stop_event=stop_event,
        session_id=session_id,
        channel=channel,
        thread_ts=thread_ts,
    )
    with WATCHERS_GUARD:
        WATCHERS[thread_key] = watcher
        thread.start()
    if persist_watch:
        SESSION_STORE.set_watch_enabled(thread_key, True)
    return watcher


def maybe_handle_live_turn_control_command(client, channel, thread_ts, thread_key, prompt, user_id):
    if not (is_interrupt_command(prompt) or is_steer_command(prompt)):
        return False

    owner_error = get_thread_owner_access_error(thread_key, user_id)
    if owner_error:
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=owner_error)
        return True

    active_record = ACTIVE_TURN_REGISTRY.get_for_thread(thread_key)
    current_session_id = SESSION_STORE.get(thread_key) or (active_record.session_id if active_record else None)
    current_session_mode = get_effective_session_mode(
        thread_key,
        session_id=current_session_id,
        session_mode=get_session_mode(thread_key),
        active_record=active_record,
    )

    if is_interrupt_command(prompt):
        if not current_session_id:
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f"<@{user_id}> 当前 Slack thread 还没有 Codex session，暂时无法中断 turn。",
            )
            return True

        runtime = get_app_runtime()
        active_turn = runtime.get_active_turn(current_session_id)
        if not active_turn:
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f"<@{user_id}> {build_runtime_turn_unavailable_message(current_session_id)}",
            )
            return True

        try:
            active_turn = runtime.interrupt_active_turn(active_turn)
        except Exception as exc:
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f"<@{user_id}> 中断当前 turn 失败。\n\n{exc}",
            )
            return True

        ACTIVE_TURN_REGISTRY.set(thread_key, current_session_id, active_turn.turn_id)
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=(
                f"<@{user_id}> 已发送中断请求：session `{current_session_id}` 的活跃 turn `{active_turn.turn_id}`。"
                " 请等几秒后再用 `status` 确认状态。"
            ),
        )
        return True

    steer_payload = strip_steer_command(prompt)
    if not current_session_id:
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"<@{user_id}> 当前 Slack thread 还没有 Codex session，暂时无法 steer。",
        )
        return True
    if current_session_mode != SESSION_MODE_CONTROL:
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=(
                get_observe_mode_error(user_id, current_session_id)
                + "\n\n`steer` 只在 `control` 模式下可用。"
            ),
        )
        return True
    if not steer_payload:
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"<@{user_id}> 用法：`steer <你要追加给正在运行 turn 的指令>`。",
        )
        return True

    runtime = get_app_runtime()
    active_turn = runtime.get_active_turn(current_session_id)
    if not active_turn:
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"<@{user_id}> {build_runtime_turn_unavailable_message(current_session_id)}",
        )
        return True

    try:
        active_turn = runtime.steer_active_turn(active_turn, steer_payload)
    except Exception as exc:
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"<@{user_id}> steer 失败。\n\n{exc}",
        )
        return True

    ACTIVE_TURN_REGISTRY.set(thread_key, current_session_id, active_turn.turn_id)
    client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=(
            f"<@{user_id}> 已向 session `{current_session_id}` 的活跃 turn `{active_turn.turn_id}` "
            f"追加输入：`{truncate_text(steer_payload, max_length=120)}`"
        ),
    )
    return True


def process_prompt(client, channel, thread_ts, prompt, user_id, slack_event_payload=None):
    thread_key = make_thread_key(channel, thread_ts)
    try:
        attachment_candidates = (
            slack_image_inputs.extract_candidate_files(slack_event_payload)
            if slack_event_payload
            else []
        )
        image_download_specs = (
            slack_image_inputs.build_image_downloads_from_event(slack_event_payload)
            if slack_event_payload
            else []
        )
        document_download_specs = (
            slack_document_inputs.build_document_downloads_from_event(slack_event_payload)
            if slack_event_payload
            else []
        )
        if not prompt and not image_download_specs and not document_download_specs:
            if attachment_candidates:
                client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=(
                        "当前这条消息只包含暂不支持的附件类型。"
                        f" 目前支持图片附件，以及 {SUPPORTED_DOCUMENT_ATTACHMENT_HINT}。"
                    ),
                )
                return
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="给我一个具体任务，再让我调用 Codex。",
            )
            return

        if maybe_handle_live_turn_control_command(
            client,
            channel,
            thread_ts,
            thread_key,
            prompt,
            user_id,
        ):
            return

        lock = claim_thread_lock(thread_key)
        try:
            with lock:
                owner_error = get_thread_owner_access_error(thread_key, user_id)
                if owner_error:
                    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=owner_error)
                    return

                current_session_id = SESSION_STORE.get(thread_key)
                current_session_mode = get_effective_session_mode(
                    thread_key,
                    session_id=current_session_id,
                    session_mode=get_session_mode(thread_key),
                )
                current_session_origin = get_session_origin(thread_key)
                current_session_cwd = get_session_cwd(thread_key)
                thread_collaboration_mode = resolve_collaboration_mode(thread_key)

                if is_effort_command(prompt):
                    effort_payload = strip_effort_command(prompt)
                    current_thread_reasoning_effort = SESSION_STORE.get_reasoning_effort(thread_key)

                    if not effort_payload:
                        lines = [f"<@{user_id}> 当前 Slack thread 的 reasoning effort 状态:\n"]
                        lines.append(f"- session_id: `{current_session_id or '-'}`")
                        lines.append(f"- session_origin: `{current_session_origin or '-'}`")
                        lines.append(f"- session_cwd: `{current_session_cwd or '-'}`")
                        lines.extend(get_reasoning_effort_state_lines(
                            thread_key,
                            session_id=current_session_id,
                            session_origin=current_session_origin,
                        ))
                        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="\n".join(lines))
                        return

                    if effort_payload.lower() == "reset":
                        if not current_thread_reasoning_effort:
                            client.chat_postMessage(
                                channel=channel,
                                thread_ts=thread_ts,
                                text=f"<@{user_id}> 当前 Slack thread 还没有显式的 reasoning effort override。",
                            )
                            return
                        SESSION_STORE.clear_reasoning_effort(thread_key)
                        client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts,
                            text=(
                                f"<@{user_id}> "
                                + get_reasoning_effort_reset_message(
                                    thread_key,
                                    session_id=current_session_id,
                                    session_origin=current_session_origin,
                                )
                            ),
                        )
                        return

                    reasoning_effort = normalize_reasoning_effort(effort_payload)
                    if not reasoning_effort:
                        client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts,
                            text=(
                                f"<@{user_id}> `effort` 只支持 {format_reasoning_effort_values()}。"
                                " 例如 `effort high`。"
                            ),
                        )
                        return

                    SESSION_STORE.set_reasoning_effort(thread_key, reasoning_effort, owner_user_id=user_id)
                    client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=(
                            f"<@{user_id}> "
                            + get_reasoning_effort_set_message(
                                thread_key,
                                reasoning_effort,
                                session_id=current_session_id,
                                session_origin=current_session_origin,
                            )
                        ),
                    )
                    return

                if is_progress_command(prompt):
                    progress_payload = strip_progress_command(prompt).lower()

                    if not progress_payload or progress_payload == "status":
                        lines = [f"<@{user_id}> 当前 Slack thread 的 progress 推送状态:\n"]
                        lines.extend(get_progress_updates_state_lines(thread_key))
                        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="\n".join(lines))
                        return

                    if progress_payload == "reset":
                        if not SESSION_STORE.clear_progress_updates(thread_key):
                            client.chat_postMessage(
                                channel=channel,
                                thread_ts=thread_ts,
                                text=f"<@{user_id}> 当前 Slack thread 还没有显式的 progress 设置覆盖。",
                            )
                            return
                        effective_value, source = resolve_progress_updates(thread_key)
                        client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts,
                            text=(
                                f"<@{user_id}> 已清除当前 Slack thread 的 progress 设置覆盖。"
                                f" 现在生效的是 `{format_progress_updates_value(effective_value)}`（来源：`{source}`）。"
                            ),
                        )
                        return

                    desired_progress_updates = normalize_progress_updates(progress_payload)
                    if desired_progress_updates is None:
                        client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts,
                            text=(
                                f"<@{user_id}> `progress` 只支持 `on`、`off`、`reset`、`status`。"
                                " 例如 `progress off`。"
                            ),
                        )
                        return

                    SESSION_STORE.set_progress_updates(
                        thread_key,
                        desired_progress_updates,
                        owner_user_id=user_id,
                    )
                    client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=(
                            f"<@{user_id}> 已将当前 Slack thread 的 progress 推送设置为 "
                            f"`{format_progress_updates_value(desired_progress_updates)}`。"
                        ),
                    )
                    return

                if is_handoff_command(prompt):
                    if not current_session_id:
                        client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts,
                            text=f"<@{user_id}> 当前 Slack thread 还没有 Codex session，暂时无法生成 handoff note。",
                        )
                        return
                    if current_session_mode != SESSION_MODE_CONTROL:
                        client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts,
                            text=get_observe_mode_error(user_id, current_session_id),
                        )
                        return

                    client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=f"<@{user_id}> 正在基于当前 session 整理 handoff note，请稍等。",
                    )
                    workdir = (
                        refresh_session_cwd(
                            thread_key,
                            current_session_id,
                            owner_user_id=user_id,
                        )
                        if current_session_id
                        else None
                    )
                    workdir = resolve_workdir(
                        thread_key,
                        session_id=current_session_id,
                        session_cwd=workdir,
                    )
                    reasoning_effort, _effort_source = resolve_reasoning_effort(
                        thread_key,
                        session_id=current_session_id,
                        session_origin=current_session_origin,
                    )
                    with session_execution_guard(current_session_id):
                        codex_result = run_runtime_turn_with_updates(
                            client,
                            channel,
                            thread_ts,
                            thread_key,
                            build_handoff_prompt(),
                            session_id=current_session_id,
                            enable_progress=False,
                            reasoning_effort=reasoning_effort,
                            workdir_override=workdir,
                            owner_user_id=user_id,
                            session_origin=current_session_origin or SESSION_ORIGIN_SLACK,
                        )
                    next_session_id = codex_result.session_id
                    result = append_handoff_footer(codex_result.text, next_session_id or current_session_id, workdir)
                    print(
                        "[codex_result]"
                        f" thread_key={thread_key}"
                        f" handoff=1"
                        f" result_length={len(result or '')}",
                        flush=True,
                    )
                    pending_target_rebuild_notice = get_pending_subagent_rebuild_notice(
                        thread_key,
                        previous_session_id=current_session_id,
                        next_session_id=next_session_id,
                        owner_user_id=user_id,
                    )
                    if should_update_session_activity(codex_result) and next_session_id != current_session_id:
                        SESSION_STORE.set(
                            thread_key,
                            next_session_id,
                            owner_user_id=user_id,
                            session_origin=current_session_origin or SESSION_ORIGIN_SLACK,
                            session_cwd=workdir,
                        )
                    elif should_update_session_activity(codex_result):
                        SESSION_STORE.touch(thread_key)
                    log_session_event(
                        "handoff",
                        thread_key,
                        existing_session_id=current_session_id,
                        next_session_id=next_session_id,
                    )
                    visible_result = maybe_prefix_thread_output(next_session_id or current_session_id, result)
                    if thread_collaboration_mode == COLLABORATION_MODE_PLAN:
                        visible_result = sanitize_plan_mode_response_for_slack(visible_result)
                    post_chunks(client, channel, thread_ts, visible_result)
                    if pending_target_rebuild_notice:
                        client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts,
                            text=pending_target_rebuild_notice,
                        )
                    if response_contains_proposed_plan(result):
                        persist_latest_proposed_plan(
                            thread_key,
                            result,
                            session_id=next_session_id or current_session_id,
                            owner_user_id=user_id,
                        )
                        post_thread_plan_actions_message(
                            client,
                            channel,
                            thread_ts,
                            thread_key,
                            session_id=next_session_id or current_session_id,
                        )
                    return

                if is_recap_command(prompt):
                    if not current_session_id:
                        client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts,
                            text=f"<@{user_id}> 当前 Slack thread 还没有 Codex session，暂时无法生成 recap。",
                        )
                        return
                    if current_session_mode != SESSION_MODE_CONTROL:
                        client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts,
                            text=get_observe_mode_error(user_id, current_session_id),
                        )
                        return

                    client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=f"<@{user_id}> 正在整理当前 session 的 recap，请稍等。",
                    )
                    reasoning_effort, _effort_source = resolve_reasoning_effort(
                        thread_key,
                        session_id=current_session_id,
                        session_origin=current_session_origin,
                    )
                    workdir = (
                        refresh_session_cwd(
                            thread_key,
                            current_session_id,
                            owner_user_id=user_id,
                        )
                        if current_session_id
                        else None
                    )
                    workdir = resolve_workdir(
                        thread_key,
                        session_id=current_session_id,
                        session_cwd=workdir,
                    )
                    with session_execution_guard(current_session_id):
                        codex_result = run_runtime_turn_with_updates(
                            client,
                            channel,
                            thread_ts,
                            thread_key,
                            build_recap_prompt(),
                            session_id=current_session_id,
                            enable_progress=False,
                            reasoning_effort=reasoning_effort,
                            workdir_override=workdir,
                            owner_user_id=user_id,
                            session_origin=current_session_origin or SESSION_ORIGIN_SLACK,
                        )
                    next_session_id = codex_result.session_id
                    result = append_recap_footer(codex_result.text, next_session_id or current_session_id)
                    print(
                        "[codex_result]"
                        f" thread_key={thread_key}"
                        f" recap=1"
                        f" result_length={len(result or '')}",
                        flush=True,
                    )
                    pending_target_rebuild_notice = get_pending_subagent_rebuild_notice(
                        thread_key,
                        previous_session_id=current_session_id,
                        next_session_id=next_session_id,
                        owner_user_id=user_id,
                    )
                    if should_update_session_activity(codex_result) and next_session_id != current_session_id:
                        SESSION_STORE.set(
                            thread_key,
                            next_session_id,
                            owner_user_id=user_id,
                            session_origin=current_session_origin or SESSION_ORIGIN_SLACK,
                            session_cwd=workdir,
                        )
                    elif should_update_session_activity(codex_result):
                        SESSION_STORE.touch(thread_key)
                    log_session_event(
                        "recap",
                        thread_key,
                        existing_session_id=current_session_id,
                        next_session_id=next_session_id,
                    )
                    visible_result = maybe_prefix_thread_output(next_session_id or current_session_id, result)
                    if thread_collaboration_mode == COLLABORATION_MODE_PLAN:
                        visible_result = sanitize_plan_mode_response_for_slack(visible_result)
                    post_chunks(client, channel, thread_ts, visible_result)
                    if pending_target_rebuild_notice:
                        client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts,
                            text=pending_target_rebuild_notice,
                        )
                    if response_contains_proposed_plan(result):
                        persist_latest_proposed_plan(
                            thread_key,
                            result,
                            session_id=next_session_id or current_session_id,
                            owner_user_id=user_id,
                        )
                        post_thread_plan_actions_message(
                            client,
                            channel,
                            thread_ts,
                            thread_key,
                            session_id=next_session_id or current_session_id,
                        )
                    return

                if is_recent_command(prompt):
                    effective_workdir = resolve_workdir(
                        thread_key,
                        session_id=current_session_id,
                        session_cwd=current_session_cwd,
                    )
                    try:
                        text = get_recent_sessions_text(
                            thread_key,
                            current_session_id,
                            cwd=effective_workdir,
                            include_all=False,
                            heading=f"<@{user_id}> 当前工作目录下最近的 Codex sessions:",
                        )
                    except Exception as exc:
                        client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts,
                            text=f"<@{user_id}> 读取 recent sessions 失败。\n\n{exc}",
                        )
                        return

                    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)
                    return

                if is_sessions_command(prompt):
                    try:
                        include_all, explicit_cwd = parse_sessions_payload(strip_sessions_command(prompt))
                    except RuntimeError as exc:
                        client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts,
                            text=f"<@{user_id}> {exc}",
                        )
                        return

                    effective_workdir = explicit_cwd or resolve_workdir(
                        thread_key,
                        session_id=current_session_id,
                        session_cwd=current_session_cwd,
                    )
                    heading = (
                        f"<@{user_id}> 全局最近的 Codex sessions:"
                        if include_all
                        else f"<@{user_id}> 当前范围下最近的 Codex sessions:"
                    )
                    try:
                        text = get_recent_sessions_text(
                            thread_key,
                            current_session_id,
                            cwd=effective_workdir,
                            include_all=include_all,
                            heading=heading,
                        )
                    except Exception as exc:
                        client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts,
                            text=f"<@{user_id}> 读取 sessions 列表失败。\n\n{exc}",
                        )
                        return

                    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)
                    return

                if is_name_command(prompt):
                    if not current_session_id:
                        client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts,
                            text=f"<@{user_id}> 当前 Slack thread 还没有可重命名的 Codex session。",
                        )
                        return
                    try:
                        normalized_title = thread_views.rename_thread(
                            get_codex_app_server_config(),
                            current_session_id,
                            strip_name_command(prompt),
                        )
                    except Exception as exc:
                        client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts,
                            text=f"<@{user_id}> {exc}",
                        )
                        return

                    client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=f"<@{user_id}> 已将当前 session 重命名为 `{normalized_title}`。",
                    )
                    return

                if is_subagents_command(prompt):
                    if not current_session_id:
                        client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts,
                            text=f"<@{user_id}> 当前 Slack thread 还没有绑定 session，暂时无法查看 subagents。",
                        )
                        return
                    try:
                        subagents = discover_subagents(current_session_id)
                    except Exception as exc:
                        client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts,
                            text=f"<@{user_id}> 读取当前主 thread 的 subagents 失败。\n\n{exc}",
                        )
                        return
                    text, blocks = build_subagents_message(
                        thread_key,
                        current_session_id,
                        subagents,
                        session_mode=current_session_mode,
                    )
                    client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=text,
                        blocks=blocks,
                    )
                    return

                if is_unsupported_watch_command(prompt):
                    client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text="`watch` 现在只支持 thread 对话视图，不再接受参数。请直接发送 `watch`。",
                    )
                    return

                if is_watch_command(prompt):
                    if not current_session_id:
                        client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts,
                            text=f"<@{user_id}> 当前 Slack thread 还没有 Codex session，暂时无法查看 thread 对话。",
                        )
                        return
                    if current_session_mode == SESSION_MODE_CONTROL:
                        client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts,
                            text=(
                                f"<@{user_id}> 当前 Slack thread 已处于 `control` 模式，后续 Codex 回复会直接发到这个 Slack thread。"
                                " 为避免重复消息，当前不再启动 `watch`。"
                                " 如果你想改成只读镜像，请先发送 `observe`，再发送 `watch`。"
                            ),
                        )
                        return

                    try:
                        watch_text, last_event_key = build_watch_bootstrap(current_session_id)
                    except Exception as exc:
                        client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts,
                            text=f"<@{user_id}> 读取当前 thread 对话失败。\n\n{exc}",
                        )
                        return

                    start_watcher(client, channel, thread_ts, thread_key, current_session_id, last_event_key=last_event_key)
                    client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=(
                            f"{watch_text}\n\n"
                            "已开始持续 watch。后续我只会把新的 thread 对话推送到这个 Slack thread。"
                            " 如果你想停止持续推送，可以发送 `stop watch` 或 `unwatch`。"
                        ),
                    )
                    return

                if is_unwatch_command(prompt):
                    if stop_watcher(thread_key):
                        client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts,
                            text=f"<@{user_id}> 已停止当前 Slack thread 的持续 watch。",
                        )
                    else:
                        client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts,
                            text=f"<@{user_id}> 当前 Slack thread 没有正在运行的持续 watch。",
                        )
                    return

                if is_control_command(prompt):
                    if not current_session_id:
                        client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts,
                            text=f"<@{user_id}> 当前 Slack thread 还没有 Codex session，暂时无法切到 control 模式。",
                        )
                        return
                    watch_was_stopped = stop_watcher(thread_key)
                    SESSION_STORE.set_mode(thread_key, SESSION_MODE_CONTROL)
                    SESSION_STORE.clear_pending_subagent_target(thread_key)
                    watch_note = ""
                    if watch_was_stopped:
                        watch_note = "\n\n已自动停止当前 Slack thread 的 `watch`，避免你在 Slack 主控时收到重复镜像消息。"
                    client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=(
                            f"<@{user_id}> 当前 Slack thread 已切到 `control` 模式，后续普通消息会继续 session `{current_session_id}`。\n\n"
                            "如果终端里的交互式 Codex 还在活跃，请不要并发操作同一个 session。"
                            f"{watch_note}"
                        ),
                    )
                    return

                if is_observe_command(prompt):
                    if not current_session_id:
                        client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts,
                            text=f"<@{user_id}> 当前 Slack thread 还没有 Codex session，暂时无法切到 observe 模式。",
                        )
                        return
                    SESSION_STORE.set_mode(thread_key, SESSION_MODE_OBSERVE)
                    SESSION_STORE.clear_pending_subagent_target(thread_key)
                    watch_hint = ""
                    if get_watcher(thread_key):
                        watch_hint = (
                            "\n\n当前这个 Slack thread 的 `watch` 仍在运行，所以新的对话更新还会继续推送。"
                            " 如果你也想停止推送，再发送 `unwatch` 或 `stop watch`。"
                        )
                    client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=(
                            f"<@{user_id}> 当前 Slack thread 已切到 `observe` 模式。\n\n"
                            "后续你可以继续用 `watch` / `where` / `session` 查看状态，但普通消息不会继续 `resume` 这个 session。"
                            f"{watch_hint}"
                        ),
                    )
                    return

                if is_status_command(prompt):
                    codex_bin, model, default_workdir, timeout, sandbox, _extra_args, _full_auto = get_codex_settings()
                    watch_active = "yes" if get_watcher(thread_key) else "no"
                    runtime_active_turn = get_runtime_active_turn(current_session_id)
                    runtime_turn_id = runtime_active_turn.turn_id if runtime_active_turn else "-"
                    effective_workdir = resolve_workdir(
                        thread_key,
                        session_id=current_session_id,
                        session_cwd=current_session_cwd,
                    )
                    reasoning_lines = get_reasoning_effort_state_lines(
                        thread_key,
                        session_id=current_session_id,
                        session_origin=current_session_origin,
                    )
                    collaboration_lines = get_collaboration_mode_state_lines(thread_key)
                    progress_lines = get_progress_updates_state_lines(thread_key)
                    plan_lines = get_plan_state_lines(thread_key)
                    pending_subagent_lines = get_pending_subagent_state_lines(
                        thread_key,
                        current_session_id=current_session_id,
                    )
                    client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=(
                            f"<@{user_id}> 当前 Slack thread 的运行状态:\n\n"
                            f"- thread_key: `{thread_key}`\n"
                            f"- session_id: `{current_session_id or '-'}`\n"
                            f"- session_mode: `{current_session_mode if current_session_id else '-'}`\n"
                            f"- session_origin: `{current_session_origin or '-'}`\n"
                            f"- runtime_active_turn: `{runtime_turn_id}`\n"
                            f"- model: `{model}`\n"
                            f"- workdir: `{effective_workdir}`\n"
                            f"- default_workdir: `{default_workdir}`\n"
                            f"- session_cwd: `{current_session_cwd or '-'}`\n"
                            f"- sandbox: `{sandbox or '-'}`\n"
                            f"- timeout_seconds: `{timeout}`\n"
                            f"- codex_bin: `{codex_bin}`\n"
                            f"- watch_active: `{watch_active}`\n"
                            + "\n".join(reasoning_lines)
                            + "\n"
                            + "\n".join(collaboration_lines)
                            + "\n"
                            + "\n".join(progress_lines)
                            + "\n"
                            + "\n".join(plan_lines)
                            + "\n"
                            + "\n".join(pending_subagent_lines)
                            + "\n"
                            "如果你想让终端继续同一个会话，可以在终端里使用这个 `session_id` 执行 `codex exec resume ...`。"
                        ),
                    )
                    return

                if is_mode_command(prompt):
                    post_thread_collaboration_mode_message(
                        client,
                        channel,
                        thread_ts,
                        thread_key,
                        session_id=current_session_id,
                    )
                    return

                if is_session_command(prompt):
                    client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=(
                            f"<@{user_id}> 当前 Slack thread 的 Codex session id: `{current_session_id}`"
                            if current_session_id
                            else f"<@{user_id}> 当前 Slack thread 还没有 Codex session。"
                        ),
                    )
                    return

                if is_attach_command(prompt):
                    attach_session_id = strip_attach_command(prompt)
                    normalized_session_id = (attach_session_id or "").strip()
                    attach_error = None
                    recent_index = parse_attach_recent_selector(normalized_session_id)
                    if recent_index is not None:
                        try:
                            normalized_session_id = session_catalog.resolve_recent_selector(
                                SESSION_SELECTION_CACHE.get(thread_key),
                                recent_index,
                            )
                        except Exception as exc:
                            attach_error = str(exc)
                    elif not normalized_session_id:
                        attach_error = "请用 `attach <session_id>` 绑定一个已有的 Codex 会话。"
                    elif not is_valid_attach_session_id(normalized_session_id):
                        attach_error = (
                            "`attach` 目前只接受 Codex session UUID，例如 "
                            "`attach 019d5868-71ba-7101-9143-81867f3db5bf`。"
                        )
                    if attach_error:
                        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=attach_error)
                        return
                    attach_thread_to_session(
                        client,
                        channel,
                        thread_ts,
                        thread_key,
                        session_id=normalized_session_id,
                        user_id=user_id,
                        mode=SESSION_MODE_OBSERVE,
                    )
                    return

                if is_reset_command(prompt):
                    previous_session_id = SESSION_STORE.get(thread_key)
                    ACTIVE_TURN_REGISTRY.clear_for_thread(thread_key)
                    stop_watcher(thread_key)
                    SESSION_STORE.clear_pending_subagent_target(thread_key)
                    SESSION_STORE.delete(thread_key)
                    log_session_event("reset", thread_key, existing_session_id=previous_session_id)
                    client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=f"<@{user_id}> 当前 Slack thread 的 Codex 会话已重置。",
                    )
                    return

                force_fresh = is_fresh_command(prompt)
                fresh_reasoning_effort = None
                effective_prompt = prompt
                if force_fresh:
                    fresh_reasoning_effort, effective_prompt, fresh_error = parse_fresh_payload(strip_fresh_command(prompt))
                    if fresh_error:
                        client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts,
                            text=f"<@{user_id}> {fresh_error}",
                        )
                        return
                if not effective_prompt and (image_download_specs or document_download_specs):
                    effective_prompt = get_default_attachment_only_prompt(
                        has_images=bool(image_download_specs),
                        has_documents=bool(document_download_specs),
                    )

                if not effective_prompt:
                    client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text="`/fresh` 后面要跟具体任务。",
                    )
                    return

                thread_collaboration_mode = resolve_collaboration_mode(thread_key)
                if thread_collaboration_mode == COLLABORATION_MODE_PLAN:
                    effective_prompt = build_plan_mode_prompt(effective_prompt)

                if fresh_reasoning_effort:
                    SESSION_STORE.set_reasoning_effort(thread_key, fresh_reasoning_effort, owner_user_id=user_id)

                if force_fresh:
                    SESSION_STORE.clear_pending_subagent_target(thread_key)

                existing_session_id = None if force_fresh else current_session_id
                use_runtime_control = existing_session_id is None or current_session_mode == SESSION_MODE_CONTROL
                if existing_session_id and current_session_mode != SESSION_MODE_CONTROL:
                    client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=get_observe_mode_error(user_id, existing_session_id),
                    )
                    return

                pending_subagent_target = None
                if not force_fresh:
                    pending_subagent_target = SESSION_STORE.get_pending_subagent_target(
                        thread_key,
                        current_session_id=current_session_id,
                        owner_user_id=user_id,
                    )

                if pending_subagent_target:
                    subagent_thread_id = pending_subagent_target.get("thread_id")
                    subagent_info = None
                    subagent_error = None
                    try:
                        subagent_info = find_subagent_for_main_session(current_session_id, subagent_thread_id)
                    except Exception as exc:
                        subagent_error = str(exc)
                    if not subagent_info:
                        SESSION_STORE.clear_pending_subagent_target(thread_key)
                        error_text = (
                            f"<@{user_id}> 已取消这次 subagent 单次路由：目标 `{subagent_thread_id}`"
                            " 已不可用、不可读，或不再属于当前主 session。"
                        )
                        if subagent_error:
                            error_text = f"{error_text}\n\n{subagent_error}"
                        client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts,
                            text=error_text,
                        )
                    else:
                        client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts,
                            text=(
                                f"<@{user_id}> 正在把这条消息发给 "
                                f"`{format_subagent_short_name(subagent_info.get('agent_nickname'), subagent_info.get('agent_role'), subagent_thread_id)}`，"
                                "发送后会自动恢复 `main`。"
                            ),
                        )
                        subagent_session_cwd = None
                        with suppress(Exception):
                            subagent_session_cwd = read_thread_cwd(subagent_thread_id)
                        subagent_workdir = subagent_session_cwd or resolve_workdir(thread_key)
                        subagent_reasoning_effort, _subagent_effort_source = resolve_reasoning_effort(
                            thread_key,
                            session_id=subagent_thread_id,
                            session_origin=SESSION_ORIGIN_ATTACHED,
                        )
                        progress_updates_enabled, _progress_updates_source = resolve_progress_updates(thread_key)
                        image_paths = []
                        image_download_dir = None
                        downloaded_documents = []
                        document_download_dir = None
                        if image_download_specs:
                            image_download_dir = tempfile.mkdtemp(prefix="codex-slack-images-")
                            try:
                                image_paths = slack_image_inputs.download_slack_image_files(
                                    image_download_specs,
                                    ENV.get("SLACK_BOT_TOKEN", ""),
                                    download_dir=image_download_dir,
                                )
                            except Exception as exc:
                                slack_image_inputs.cleanup_download_directory(image_download_dir)
                                SESSION_STORE.clear_pending_subagent_target(thread_key)
                                client.chat_postMessage(
                                    channel=channel,
                                    thread_ts=thread_ts,
                                    text=(
                                        f"<@{user_id}> 下载 Slack 图片失败，暂时无法把这条图片消息发给 "
                                        f"`{format_subagent_short_name(subagent_info.get('agent_nickname'), subagent_info.get('agent_role'), subagent_thread_id)}`。\n\n{exc}"
                                    ),
                                )
                                return
                        if document_download_specs:
                            document_download_dir = tempfile.mkdtemp(prefix="codex-slack-documents-")
                            try:
                                downloaded_documents = slack_document_inputs.download_slack_document_files(
                                    document_download_specs,
                                    ENV.get("SLACK_BOT_TOKEN", ""),
                                    download_dir=document_download_dir,
                                )
                            except Exception as exc:
                                slack_document_inputs.cleanup_download_directory(document_download_dir)
                                slack_image_inputs.cleanup_downloaded_files(image_paths)
                                slack_image_inputs.cleanup_download_directory(image_download_dir)
                                SESSION_STORE.clear_pending_subagent_target(thread_key)
                                client.chat_postMessage(
                                    channel=channel,
                                    thread_ts=thread_ts,
                                    text=(
                                        f"<@{user_id}> 下载 Slack 文档失败，暂时无法把这条文档消息发给 "
                                        f"`{format_subagent_short_name(subagent_info.get('agent_nickname'), subagent_info.get('agent_role'), subagent_thread_id)}`。\n\n{exc}"
                                    ),
                                )
                                return
                        routed_prompt = build_document_attachment_prompt(effective_prompt, downloaded_documents)
                        routed_result = None
                        try:
                            with session_execution_guard(subagent_thread_id):
                                routed_result = run_runtime_turn_with_updates(
                                    client,
                                    channel,
                                    thread_ts,
                                    thread_key,
                                    routed_prompt,
                                    session_id=subagent_thread_id,
                                    enable_progress=progress_updates_enabled,
                                    reasoning_effort=subagent_reasoning_effort,
                                    workdir_override=subagent_workdir,
                                    image_paths=image_paths,
                                    owner_user_id=user_id,
                                    session_origin=SESSION_ORIGIN_ATTACHED,
                                    collaboration_mode=thread_collaboration_mode,
                                    track_active_turn=False,
                                    persist_session_binding=False,
                                )
                        finally:
                            SESSION_STORE.clear_pending_subagent_target(thread_key)
                            slack_document_inputs.cleanup_downloaded_documents(downloaded_documents)
                            slack_document_inputs.cleanup_download_directory(document_download_dir)
                            slack_image_inputs.cleanup_downloaded_files(image_paths)
                            slack_image_inputs.cleanup_download_directory(image_download_dir)
                        routed_text = maybe_prefix_thread_output(subagent_thread_id, routed_result.text)
                        routed_text = (
                            f"{routed_text}\n\n"
                            f"已发送给 `{format_subagent_short_name(subagent_info.get('agent_nickname'), subagent_info.get('agent_role'), subagent_thread_id)}`；"
                            "当前目标已恢复为 `main`。"
                        )
                        if thread_collaboration_mode == COLLABORATION_MODE_PLAN:
                            routed_text = sanitize_plan_mode_response_for_slack(routed_text)
                        log_session_event(
                            "subagent_send_next",
                            thread_key,
                            existing_session_id=current_session_id,
                            next_session_id=current_session_id,
                        )
                        post_chunks(client, channel, thread_ts, routed_text)
                        if response_contains_proposed_plan(routed_result.text):
                            persist_latest_proposed_plan(
                                thread_key,
                                routed_result.text,
                                session_id=current_session_id,
                                owner_user_id=user_id,
                            )
                            post_thread_plan_actions_message(
                                client,
                                channel,
                                thread_ts,
                                thread_key,
                                session_id=current_session_id,
                            )
                        return

                log_session_event(
                    "fresh_attempt" if force_fresh else ("resume_attempt" if existing_session_id else "new_attempt"),
                    thread_key,
                    existing_session_id=existing_session_id,
                )
                client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=(
                        f"<@{user_id}> 正在强制启动一个新的 Codex 会话，请稍等。"
                        if force_fresh
                        else f"<@{user_id}> {'正在继续当前 Slack thread 的 Codex 会话，请稍等。' if existing_session_id else '正在启动当前 Slack thread 的 Codex 会话，请稍等。'}"
                    ),
                )

                run_session_origin = current_session_origin if existing_session_id else SESSION_ORIGIN_SLACK
                runtime_session_cwd = (
                    refresh_session_cwd(
                        thread_key,
                        existing_session_id,
                        owner_user_id=user_id,
                    )
                    if existing_session_id
                    else None
                )
                run_workdir = resolve_workdir(
                    thread_key,
                    session_id=existing_session_id,
                    session_cwd=runtime_session_cwd,
                )
                reasoning_effort, _effort_source = resolve_reasoning_effort(
                    thread_key,
                    session_id=existing_session_id,
                    session_origin=run_session_origin,
                )
                progress_updates_enabled, _progress_updates_source = resolve_progress_updates(thread_key)
                image_paths = []
                image_download_dir = None
                downloaded_documents = []
                document_download_dir = None
                if image_download_specs:
                    image_download_dir = tempfile.mkdtemp(prefix="codex-slack-images-")
                    try:
                        image_paths = slack_image_inputs.download_slack_image_files(
                            image_download_specs,
                            ENV.get("SLACK_BOT_TOKEN", ""),
                            download_dir=image_download_dir,
                        )
                    except Exception as exc:
                        slack_image_inputs.cleanup_download_directory(image_download_dir)
                        client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts,
                            text=f"<@{user_id}> 下载 Slack 图片失败，暂时无法把这条图片消息传给 Codex。\n\n{exc}",
                        )
                        return
                if document_download_specs:
                    document_download_dir = tempfile.mkdtemp(prefix="codex-slack-documents-")
                    try:
                        downloaded_documents = slack_document_inputs.download_slack_document_files(
                            document_download_specs,
                            ENV.get("SLACK_BOT_TOKEN", ""),
                            download_dir=document_download_dir,
                        )
                    except Exception as exc:
                        slack_document_inputs.cleanup_download_directory(document_download_dir)
                        slack_image_inputs.cleanup_downloaded_files(image_paths)
                        slack_image_inputs.cleanup_download_directory(image_download_dir)
                        client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts,
                            text=f"<@{user_id}> 下载 Slack 文档失败，暂时无法把这条文档消息传给 Codex。\n\n{exc}",
                        )
                        return

                effective_prompt = build_document_attachment_prompt(effective_prompt, downloaded_documents)

                try:
                    with session_execution_guard(existing_session_id):
                        if use_runtime_control:
                            codex_result = run_runtime_turn_with_updates(
                                client,
                                channel,
                                thread_ts,
                                thread_key,
                                effective_prompt,
                                session_id=existing_session_id,
                                enable_progress=progress_updates_enabled,
                                reasoning_effort=reasoning_effort,
                                workdir_override=run_workdir,
                                image_paths=image_paths,
                                owner_user_id=user_id,
                                session_origin=run_session_origin,
                                collaboration_mode=thread_collaboration_mode,
                            )
                        else:
                            codex_result = run_codex_with_updates(
                                client,
                                channel,
                                thread_ts,
                                effective_prompt,
                                session_id=existing_session_id,
                                enable_progress=progress_updates_enabled,
                                reasoning_effort=reasoning_effort,
                                workdir_override=run_workdir,
                                image_paths=image_paths,
                            )
                        next_session_id = codex_result.session_id
                        result = codex_result.text
                        print(
                            "[codex_result]"
                            f" thread_key={thread_key}"
                            f" result_length={len(result or '')}",
                            flush=True,
                        )
                        if existing_session_id and should_rebuild_invalid_session(codex_result):
                            log_session_event(
                                "resume_failed_rebuild",
                                thread_key,
                                existing_session_id=existing_session_id,
                            )
                            SESSION_STORE.clear_session_binding(thread_key)
                            client.chat_postMessage(
                                channel=channel,
                                thread_ts=thread_ts,
                                text=f"<@{user_id}> 当前 Slack thread 的 Codex 会话不可恢复，正在自动重建新会话。",
                            )
                            rebuilt_reasoning_effort, _rebuilt_effort_source = resolve_reasoning_effort(
                                thread_key,
                                session_id=None,
                                session_origin=SESSION_ORIGIN_SLACK,
                            )
                            if use_runtime_control:
                                codex_result = run_runtime_turn_with_updates(
                                    client,
                                    channel,
                                    thread_ts,
                                    thread_key,
                                    effective_prompt,
                                    enable_progress=progress_updates_enabled,
                                    reasoning_effort=rebuilt_reasoning_effort,
                                    workdir_override=run_workdir,
                                    image_paths=image_paths,
                                    owner_user_id=user_id,
                                    session_origin=SESSION_ORIGIN_SLACK,
                                    collaboration_mode=thread_collaboration_mode,
                                )
                            else:
                                codex_result = run_codex_with_updates(
                                    client,
                                    channel,
                                    thread_ts,
                                    effective_prompt,
                                    enable_progress=progress_updates_enabled,
                                    reasoning_effort=rebuilt_reasoning_effort,
                                    workdir_override=run_workdir,
                                    image_paths=image_paths,
                                )
                            next_session_id = codex_result.session_id
                            result = codex_result.text
                            run_session_origin = SESSION_ORIGIN_SLACK
                            print(
                                "[codex_result]"
                                f" thread_key={thread_key}"
                                f" rebuilt=1"
                                f" result_length={len(result or '')}",
                                flush=True,
                            )
                finally:
                    slack_document_inputs.cleanup_downloaded_documents(downloaded_documents)
                    slack_document_inputs.cleanup_download_directory(document_download_dir)
                    slack_image_inputs.cleanup_downloaded_files(image_paths)
                    slack_image_inputs.cleanup_download_directory(image_download_dir)

                if should_update_session_activity(codex_result) and next_session_id != existing_session_id:
                    SESSION_STORE.set(
                        thread_key,
                        next_session_id,
                        owner_user_id=user_id,
                        session_origin=run_session_origin,
                        session_cwd=run_workdir,
                    )
                elif should_update_session_activity(codex_result):
                    SESSION_STORE.touch(thread_key)

                log_session_event(
                    "completed",
                    thread_key,
                    existing_session_id=existing_session_id,
                    next_session_id=next_session_id,
                )
                visible_result = maybe_prefix_thread_output(next_session_id, result)
                if thread_collaboration_mode == COLLABORATION_MODE_PLAN:
                    visible_result = sanitize_plan_mode_response_for_slack(visible_result)
                post_chunks(client, channel, thread_ts, visible_result)
                if response_contains_proposed_plan(result):
                    persist_latest_proposed_plan(
                        thread_key,
                        result,
                        session_id=next_session_id or existing_session_id,
                        owner_user_id=user_id,
                    )
                    post_thread_plan_actions_message(
                        client,
                        channel,
                        thread_ts,
                        thread_key,
                        session_id=next_session_id or existing_session_id,
                    )
        finally:
            release_thread_lock(thread_key)
    except Exception as exc:
        runtime_diagnostics = ""
        if should_reset_runtime_after_exception(exc):
            with suppress(Exception):
                runtime = get_app_runtime()
                runtime.reset()
                runtime_diagnostics = runtime.last_client_diagnostics()
        try:
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=build_process_error_message(user_id, exc, diagnostics=runtime_diagnostics),
            )
        except Exception:
            pass
        print(
            "[process_error]"
            f" thread_key={thread_key}"
            f" error={exc!r}",
            flush=True,
        )
        traceback.print_exc()
        return


def start_background_job(client, channel, thread_ts, prompt, user_id, slack_event_payload=None):
    thread = threading.Thread(
        target=process_prompt,
        args=(client, channel, thread_ts, prompt, user_id, slack_event_payload),
        daemon=True,
    )
    thread.start()


def validate_env():
    required = [
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
    ]
    missing = [name for name in required if not ENV.get(name)]
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(f"Missing required environment variables: {joined}")


def format_home_timestamp(unix_ts):
    if not unix_ts:
        return "-"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(unix_ts)))
    except Exception:
        return "-"


def get_home_binding_label(thread_key):
    channel_id = str(thread_key or "").strip().partition(":")[0].upper()
    if not channel_id:
        return thread_key or "Slack Thread"
    if channel_id.startswith("D"):
        return "Direct Message"
    if channel_id.startswith("C"):
        return "Channel Thread"
    if channel_id.startswith("G"):
        return "Private Channel Thread"
    return f"Thread {channel_id}"


def encode_home_binding_value(thread_key, session_id):
    return json.dumps(
        {
            "thread_key": str(thread_key or "").strip(),
            "session_id": str(session_id or "").strip(),
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )


def decode_home_binding_value(raw_value):
    payload = json.loads(str(raw_value or ""))
    if not isinstance(payload, dict):
        raise RuntimeError("binding payload invalid")
    thread_key = str(payload.get("thread_key") or "").strip()
    session_id = str(payload.get("session_id") or "").strip()
    if not thread_key or not session_id:
        raise RuntimeError("binding payload incomplete")
    return thread_key, session_id


def build_home_rename_modal(*, thread_key, session_id, initial_title=""):
    initial = str(initial_title or "").strip()
    return {
        "type": "modal",
        "callback_id": "binding_rename_submit",
        "private_metadata": encode_home_binding_value(thread_key, session_id),
        "title": {"type": "plain_text", "text": "Rename Binding"},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "binding_rename_input",
                "label": {"type": "plain_text", "text": "Title"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "title",
                    "initial_value": initial,
                    "placeholder": {"type": "plain_text", "text": "e.g. runner resume test"},
                },
            }
        ],
    }


def extract_view_state_value(view_state, block_id, action_id):
    values = ((view_state or {}).get("values") or {})
    block = values.get(block_id) or {}
    action = block.get(action_id) or {}
    value = str(action.get("value") or "").strip()
    return value


def extract_action_channel_thread(body):
    channel = body.get("channel", {}) or {}
    container = body.get("container", {}) or {}
    message = body.get("message", {}) or {}
    channel_id = (
        channel.get("id")
        or container.get("channel_id")
        or message.get("channel")
        or ""
    )
    message_ts = container.get("message_ts") or message.get("ts") or ""
    thread_ts = container.get("thread_ts") or message.get("thread_ts") or message_ts
    return str(channel_id or ""), str(thread_ts or ""), str(message_ts or "")


def get_thread_display_title(session_id):
    if not session_id:
        return None
    with suppress(Exception):
        thread_response = read_thread_response(session_id, include_turns=False)
        thread = read_field(thread_response, "thread", thread_response)
        title = str(read_field(thread, "name", "") or "").strip()
        if title:
            return title
        preview = str(read_field(thread, "preview", "") or "").strip()
        if preview:
            return truncate_text(preview, max_length=80)
    return None


def get_home_bindings_rows(user_id, limit=5):
    raw_rows = SESSION_STORE.list_for_owner(user_id, limit=limit)
    rows = []
    for row in raw_rows:
        thread_key = row.get("thread_key", "")
        stored_session_id = row.get("session_id", "")
        active_record = ACTIVE_TURN_REGISTRY.get_for_thread(thread_key)
        effective_session_id = (
            active_record.session_id
            if active_record and getattr(active_record, "session_id", None)
            else stored_session_id
        )
        fallback_label = get_home_binding_label(row.get("thread_key", ""))
        title = get_thread_display_title(effective_session_id)
        pending_target = SESSION_STORE.get_pending_subagent_target(
            thread_key,
            current_session_id=effective_session_id,
            owner_user_id=user_id,
        )
        status_text = fallback_label if title and title != fallback_label else None
        if pending_target:
            pending_label = format_subagent_short_name(
                pending_target.get("agent_nickname"),
                pending_target.get("agent_role"),
                pending_target.get("thread_id"),
            )
            pending_text = f"pending next target: {pending_label}"
            status_text = f"{status_text} | {pending_text}" if status_text else pending_text
        rows.append(
            {
                "label": title or fallback_label,
                "session_id": effective_session_id or "-",
                "mode": row.get("mode", "-"),
                "cwd": row.get("cwd", "-"),
                "updated_at": format_home_timestamp(row.get("updated_at")),
                "status_text": status_text,
                "action_id": "binding_rename_open",
                "action_text": "Rename",
                "action_value": encode_home_binding_value(
                    thread_key,
                    effective_session_id,
                ),
            }
        )
    return rows


def get_home_recent_sessions_rows(limit=5, exclude_thread_ids=None):
    excluded_thread_ids = {
        str(thread_id or "").strip()
        for thread_id in (exclude_thread_ids or [])
        if str(thread_id or "").strip()
    }
    try:
        response = thread_views.list_threads(
            get_codex_app_server_config(),
            archived=False,
            limit=limit,
            sort_key="updated_at",
            sort_direction="desc",
        )
        summaries = thread_views.extract_thread_summaries(response)
    except Exception as exc:
        return [
            {
                "thread_id": "-",
                "title": f"[unavailable] {truncate_text(str(exc), max_length=120)}",
                "cwd": "-",
                "status": "-",
            }
        ]

    rows = []
    for summary in summaries:
        thread_id = str(summary.thread_id or "").strip()
        if not thread_id or thread_id in excluded_thread_ids:
            continue
        rows.append(
            {
                "label": summary.name or summary.preview or "(untitled)",
                "thread_id": thread_id or "-",
                "title": summary.name or summary.preview or "(untitled)",
                "cwd": summary.cwd or "-",
                "status": summary.status_type or "-",
            }
        )
        if len(rows) >= limit:
            break
    return rows


def get_latest_event_key_for_session(session_id):
    events = read_conversation_events(session_id)
    if not events:
        return None
    return get_event_key(events[-1])


def should_restore_control_recovery_watch(session_id):
    if not session_id:
        return False
    try:
        thread_response = read_thread_response(session_id, include_turns=False)
    except Exception:
        return False
    return extract_thread_status_type(thread_response) == "active"


def restore_background_watchers(client):
    restored = []
    skipped = []
    for binding in SESSION_STORE.list_bindings():
        thread_key = binding.get("thread_key")
        session_id = binding.get("session_id")
        channel, thread_ts = parse_thread_key(thread_key)
        if not thread_key or not session_id or not channel or not thread_ts:
            skipped.append(
                {
                    "thread_key": thread_key or "-",
                    "session_id": session_id or "-",
                    "reason": "invalid_binding",
                }
            )
            continue
        if get_watcher(thread_key):
            skipped.append(
                {
                    "thread_key": thread_key,
                    "session_id": session_id,
                    "reason": "watcher_already_running",
                }
            )
            continue

        mode = binding.get("mode") or SESSION_MODE_CONTROL
        persist_watch = bool(binding.get("watch_enabled"))
        stop_when_idle = False
        persisted_last_event_key = SESSION_STORE.get_watch_last_event_key(
            thread_key,
            current_session_id=session_id,
        )
        latest_event_key = None

        if not persist_watch and mode == SESSION_MODE_CONTROL:
            is_active = should_restore_control_recovery_watch(session_id)
            stop_when_idle = True
            if not is_active:
                if not persisted_last_event_key:
                    skipped.append(
                        {
                            "thread_key": thread_key,
                            "session_id": session_id,
                            "reason": "idle_control_session",
                        }
                    )
                    continue
                try:
                    latest_event_key = get_latest_event_key_for_session(session_id)
                except Exception as exc:
                    skipped.append(
                        {
                            "thread_key": thread_key,
                            "session_id": session_id,
                            "reason": f"read_failed: {truncate_text(str(exc), max_length=120)}",
                        }
                    )
                    continue
                if latest_event_key == persisted_last_event_key:
                    skipped.append(
                        {
                            "thread_key": thread_key,
                            "session_id": session_id,
                            "reason": "idle_control_session_no_backlog",
                        }
                    )
                    continue
        elif not persist_watch:
            skipped.append(
                {
                    "thread_key": thread_key,
                    "session_id": session_id,
                    "reason": "watch_not_enabled",
                }
            )
            continue

        cursor_source = "persisted" if persisted_last_event_key else "latest"
        if persisted_last_event_key:
            last_event_key = persisted_last_event_key
        else:
            try:
                latest_event_key = latest_event_key or get_latest_event_key_for_session(session_id)
            except Exception as exc:
                skipped.append(
                    {
                        "thread_key": thread_key,
                        "session_id": session_id,
                        "reason": f"read_failed: {truncate_text(str(exc), max_length=120)}",
                    }
                )
                continue
            last_event_key = latest_event_key

        start_watcher(
            client,
            channel,
            thread_ts,
            thread_key,
            session_id,
            last_event_key=last_event_key,
            persist_watch=persist_watch,
            stop_when_idle=stop_when_idle,
        )
        restored.append(
            {
                "thread_key": thread_key,
                "session_id": session_id,
                "mode": mode,
                "persist_watch": persist_watch,
                "stop_when_idle": stop_when_idle,
                "cursor_source": cursor_source,
                "last_event_key": last_event_key,
            }
        )
    return {
        "restored_count": len(restored),
        "restored": restored,
        "skipped": skipped,
    }


def publish_home_view(client, user_id):
    codex_bin, model, default_workdir, timeout, sandbox, _extra_args, full_auto = get_codex_settings()
    binding_rows = get_home_bindings_rows(user_id, limit=5)
    recent_rows = get_home_recent_sessions_rows(
        limit=5,
        exclude_thread_ids=[row.get("session_id") for row in binding_rows],
    )
    bindings_summary = slack_home.format_binding_summary_rows(binding_rows)
    recent_sessions_summary = slack_home.format_recent_sessions_rows(recent_rows)
    help_text = (
        "Use `refresh` in Home, or continue controlling sessions in thread with "
        "`attach`, `watch`, `control/takeover`, `observe`, `interrupt`, `steer`, and `status`."
    )
    view = slack_home.build_home_view(
        default_workdir=default_workdir,
        default_model=model,
        default_effort=get_default_reasoning_effort(),
        bindings_summary=bindings_summary,
        recent_sessions_summary=recent_sessions_summary,
        bindings_rows=binding_rows,
        recent_sessions_rows=recent_rows,
        quick_hints=[
            "Use `watch` for passive mirroring from terminal to phone.",
            "Use `takeover` when Slack should become the writing side.",
            "Use `Rename` in Home to give long-lived bindings a human title.",
            "Image uploads can be passed through to Codex in DM or mention threads.",
        ],
        help_text=(
            f"{help_text}\nConfig: sandbox=`{sandbox}` timeout=`{timeout}` "
            f"full_auto=`{'1' if full_auto else '0'}` codex_bin=`{codex_bin}`"
        ),
    )
    client.views_publish(user_id=user_id, view=view)


def build_app():
    app = App(
        token=ENV["SLACK_BOT_TOKEN"],
        signing_secret=ENV.get("SLACK_SIGNING_SECRET", ""),
        token_verification_enabled=False,
    )

    @app.use
    def log_incoming_events(logger, body, next):
        event = (body or {}).get("event", {})
        print(
            "[slack_event]"
            f" type={event.get('type', '-')}"
            f" channel_type={event.get('channel_type', '-')}"
            f" subtype={event.get('subtype', '-')}"
            f" user={event.get('user', '-')}"
            f" channel={event.get('channel', '-')}"
            f" text={json.dumps(summarize_text_for_log(event.get('text')), ensure_ascii=True)}",
            flush=True,
        )
        next()

    @app.event("app_home_opened")
    def handle_app_home_opened(body, client, logger):
        event = body.get("event", {})
        user_id = event.get("user", "")
        if not user_id:
            return
        if not is_allowed_slack_user(user_id):
            logger.warning("Rejected app_home_opened from unauthorized user %s", user_id)
            return
        try:
            publish_home_view(client, user_id)
        except Exception as exc:  # pragma: no cover
            logger.exception("Failed publishing App Home for %s: %r", user_id, exc)

    @app.action("home_refresh")
    def handle_home_refresh(ack, body, client, logger):
        ack()
        user = body.get("user", {}) or {}
        user_id = user.get("id", "")
        if not user_id:
            return
        if not is_allowed_slack_user(user_id):
            logger.warning("Rejected home_refresh from unauthorized user %s", user_id)
            return
        try:
            publish_home_view(client, user_id)
        except Exception as exc:  # pragma: no cover
            logger.exception("Failed refreshing App Home for %s: %r", user_id, exc)

    @app.action("binding_rename_open")
    def handle_binding_rename_open(ack, body, client, logger):
        ack()
        user = body.get("user", {}) or {}
        user_id = user.get("id", "")
        if not user_id:
            return
        if not is_allowed_slack_user(user_id):
            logger.warning("Rejected binding_rename_open from unauthorized user %s", user_id)
            return
        actions = body.get("actions", []) or []
        action = actions[0] if actions else {}
        try:
            thread_key, session_id = decode_home_binding_value(action.get("value"))
        except Exception as exc:
            logger.exception("Invalid binding rename payload from %s: %r", user_id, exc)
            return
        if get_thread_owner_access_error(thread_key, user_id):
            logger.warning("Rejected binding rename for non-owner user %s thread %s", user_id, thread_key)
            return
        initial_title = ""
        with suppress(Exception):
            thread_response = read_thread_response(session_id, include_turns=False)
            thread = read_field(thread_response, "thread", thread_response)
            initial_title = read_field(thread, "name", "") or ""
        trigger_id = body.get("trigger_id", "")
        if not trigger_id:
            return
        try:
            client.views_open(
                trigger_id=trigger_id,
                view=build_home_rename_modal(
                    thread_key=thread_key,
                    session_id=session_id,
                    initial_title=initial_title,
                ),
            )
        except Exception as exc:  # pragma: no cover
            logger.exception("Failed opening binding rename modal for %s: %r", user_id, exc)

    @app.view("binding_rename_submit")
    def handle_binding_rename_submit(ack, body, view, client, logger):
        ack()
        user = body.get("user", {}) or {}
        user_id = user.get("id", "")
        if not user_id:
            return
        if not is_allowed_slack_user(user_id):
            logger.warning("Rejected binding_rename_submit from unauthorized user %s", user_id)
            return
        try:
            thread_key, session_id = decode_home_binding_value(view.get("private_metadata"))
            if get_thread_owner_access_error(thread_key, user_id):
                logger.warning("Rejected binding rename submit for non-owner user %s thread %s", user_id, thread_key)
                return
            title = extract_view_state_value(view.get("state"), "binding_rename_input", "title")
            normalized_title = thread_views.rename_thread(
                get_codex_app_server_config(),
                session_id,
                title,
            )
            publish_home_view(client, user_id)
            print(
                "[home_rename]"
                f" user={user_id}"
                f" thread_key={thread_key}"
                f" session_id={session_id}"
                f" title={json.dumps(normalized_title, ensure_ascii=True)}",
                flush=True,
            )
        except Exception as exc:  # pragma: no cover
            logger.exception("Failed submitting binding rename for %s: %r", user_id, exc)

    @app.action(SUBAGENT_SEND_NEXT_ACTION)
    def handle_subagent_send_next(ack, body, client, logger):
        ack()
        user = body.get("user", {}) or {}
        user_id = user.get("id", "")
        if not user_id:
            return
        if not is_allowed_slack_user(user_id):
            logger.warning("Rejected subagent_send_next from unauthorized user %s", user_id)
            return
        actions = body.get("actions", []) or []
        action = actions[0] if actions else {}
        try:
            thread_key, session_id, subagent_thread_id = decode_subagent_action_value(action.get("value"))
        except Exception as exc:
            logger.exception("Invalid subagent_send_next payload from %s: %r", user_id, exc)
            return
        if get_thread_owner_access_error(thread_key, user_id):
            logger.warning("Rejected subagent_send_next for non-owner user %s thread %s", user_id, thread_key)
            return
        channel_id, thread_ts, _message_ts = extract_action_channel_thread(body)
        if not channel_id or not thread_ts:
            return
        handle_subagent_send_next_action(
            client,
            logger,
            thread_key=thread_key,
            session_id=session_id,
            subagent_thread_id=subagent_thread_id,
            user_id=user_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
        )

    @app.action(SUBAGENT_SEND_CANCEL_ACTION)
    def handle_subagent_send_cancel(ack, body, client, logger):
        ack()
        user = body.get("user", {}) or {}
        user_id = user.get("id", "")
        if not user_id:
            return
        if not is_allowed_slack_user(user_id):
            logger.warning("Rejected subagent_send_cancel from unauthorized user %s", user_id)
            return
        actions = body.get("actions", []) or []
        action = actions[0] if actions else {}
        try:
            thread_key, _session_id, _subagent_thread_id = decode_subagent_action_value(action.get("value"))
        except Exception as exc:
            logger.exception("Invalid subagent_send_cancel payload from %s: %r", user_id, exc)
            return
        if get_thread_owner_access_error(thread_key, user_id):
            logger.warning("Rejected subagent_send_cancel for non-owner user %s thread %s", user_id, thread_key)
            return
        channel_id, thread_ts, _message_ts = extract_action_channel_thread(body)
        if not channel_id or not thread_ts:
            return
        handle_subagent_send_cancel_action(
            client,
            thread_key=thread_key,
            channel_id=channel_id,
            thread_ts=thread_ts,
        )

    @app.action(SUBAGENT_OBSERVE_ACTION)
    def handle_subagent_observe(ack, body, client, logger):
        ack()
        user = body.get("user", {}) or {}
        user_id = user.get("id", "")
        if not user_id:
            return
        if not is_allowed_slack_user(user_id):
            logger.warning("Rejected subagent_observe from unauthorized user %s", user_id)
            return
        actions = body.get("actions", []) or []
        action = actions[0] if actions else {}
        try:
            thread_key, session_id, subagent_thread_id = decode_subagent_action_value(action.get("value"))
        except Exception as exc:
            logger.exception("Invalid subagent_observe payload from %s: %r", user_id, exc)
            return
        if get_thread_owner_access_error(thread_key, user_id):
            logger.warning("Rejected subagent_observe for non-owner user %s thread %s", user_id, thread_key)
            return
        channel_id, thread_ts, _message_ts = extract_action_channel_thread(body)
        if not channel_id or not thread_ts:
            return
        handle_subagent_observe_action(
            client,
            thread_key=thread_key,
            session_id=session_id,
            subagent_thread_id=subagent_thread_id,
            user_id=user_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
        )

    @app.action(SUBAGENT_ATTACH_ACTION)
    def handle_subagent_attach(ack, body, client, logger):
        ack()
        user = body.get("user", {}) or {}
        user_id = user.get("id", "")
        if not user_id:
            return
        if not is_allowed_slack_user(user_id):
            logger.warning("Rejected subagent_attach from unauthorized user %s", user_id)
            return
        actions = body.get("actions", []) or []
        action = actions[0] if actions else {}
        try:
            thread_key, session_id, subagent_thread_id = decode_subagent_action_value(action.get("value"))
        except Exception as exc:
            logger.exception("Invalid subagent_attach payload from %s: %r", user_id, exc)
            return
        if get_thread_owner_access_error(thread_key, user_id):
            logger.warning("Rejected subagent_attach for non-owner user %s thread %s", user_id, thread_key)
            return
        channel_id, thread_ts, _message_ts = extract_action_channel_thread(body)
        if not channel_id or not thread_ts:
            return
        handle_subagent_attach_action(
            client,
            thread_key=thread_key,
            session_id=session_id,
            subagent_thread_id=subagent_thread_id,
            user_id=user_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
        )

    @app.action(THREAD_COLLABORATION_MODE_PLAN_ACTION)
    @app.action(THREAD_COLLABORATION_MODE_DEFAULT_ACTION)
    def handle_thread_collaboration_mode_set(ack, body, client, logger):
        ack()
        user = body.get("user", {}) or {}
        user_id = user.get("id", "")
        if not user_id:
            return
        if not is_allowed_slack_user(user_id):
            logger.warning("Rejected collaboration mode action from unauthorized user %s", user_id)
            return
        actions = body.get("actions", []) or []
        action = actions[0] if actions else {}
        try:
            thread_key, target_mode = decode_thread_collaboration_mode_value(action.get("value"))
        except Exception as exc:
            logger.exception("Invalid collaboration mode payload from %s: %r", user_id, exc)
            return
        if get_thread_owner_access_error(thread_key, user_id):
            logger.warning(
                "Rejected collaboration mode action for non-owner user %s thread %s",
                user_id,
                thread_key,
            )
            return
        SESSION_STORE.set_collaboration_mode(thread_key, target_mode, owner_user_id=user_id)
        session_id = SESSION_STORE.get(thread_key)
        channel_id, thread_ts, message_ts = extract_action_channel_thread(body)
        text, blocks = build_thread_collaboration_mode_message(
            thread_key,
            session_id=session_id,
            collaboration_mode=target_mode,
        )
        try:
            if channel_id and message_ts:
                client.chat_update(
                    channel=channel_id,
                    ts=message_ts,
                    text=text,
                    blocks=blocks,
                )
            elif channel_id and thread_ts:
                client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text=text,
                    blocks=blocks,
                )
        except Exception as exc:  # pragma: no cover
            logger.exception("Failed updating collaboration mode card for %s: %r", user_id, exc)

    @app.action(THREAD_PLAN_IMPLEMENT_CLEAN_ACTION)
    @app.action(THREAD_PLAN_IMPLEMENT_HERE_ACTION)
    @app.action(THREAD_PLAN_KEEP_PLANNING_ACTION)
    def handle_thread_plan_action(ack, body, client, logger):
        ack()
        user = body.get("user", {}) or {}
        user_id = user.get("id", "")
        if not user_id:
            return
        if not is_allowed_slack_user(user_id):
            logger.warning("Rejected plan action from unauthorized user %s", user_id)
            return
        actions = body.get("actions", []) or []
        action = actions[0] if actions else {}
        try:
            thread_key, action_name = decode_thread_plan_action_value(action.get("value"))
        except Exception as exc:
            logger.exception("Invalid plan action payload from %s: %r", user_id, exc)
            return
        if get_thread_owner_access_error(thread_key, user_id):
            logger.warning(
                "Rejected plan action for non-owner user %s thread %s",
                user_id,
                thread_key,
            )
            return
        channel_id, thread_ts, message_ts = extract_action_channel_thread(body)
        if not channel_id or not thread_ts:
            return

        if action_name == "keep_planning":
            SESSION_STORE.set_latest_plan_selected_action(
                thread_key,
                "keep_planning",
                owner_user_id=user_id,
            )
            text, blocks = build_thread_plan_actions_message(
                thread_key,
                session_id=SESSION_STORE.get(thread_key),
            )
            with suppress(Exception):
                if channel_id and message_ts:
                    client.chat_update(
                        channel=channel_id,
                        ts=message_ts,
                        text=text,
                        blocks=blocks,
                    )
            handle_keep_planning_action(
                client,
                channel_id,
                thread_ts,
                thread_key,
                user_id=user_id,
                logger=logger,
            )
            return

        execution_mode = "clean" if action_name == "clean" else "here"
        SESSION_STORE.set_latest_plan_selected_action(
            thread_key,
            execution_mode,
            owner_user_id=user_id,
        )
        text, blocks = build_thread_plan_actions_message(
            thread_key,
            session_id=SESSION_STORE.get(thread_key),
        )
        with suppress(Exception):
            if channel_id and message_ts:
                client.chat_update(
                    channel=channel_id,
                    ts=message_ts,
                    text=text,
                    blocks=blocks,
                )
        start_text = (
            f"<@{user_id}> 正在按已批准的方案开始实施（`{execution_mode}`），请稍等。"
        )
        client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=start_text,
        )
        try:
            codex_result, details = execute_plan_implementation_action(
                client,
                channel_id,
                thread_ts,
                thread_key,
                user_id=user_id,
                execution_mode=execution_mode,
            )
        except Exception as exc:
            runtime_diagnostics = ""
            if should_reset_runtime_after_exception(exc):
                with suppress(Exception):
                    runtime = get_app_runtime()
                    runtime.reset()
                    runtime_diagnostics = runtime.last_client_diagnostics()
            client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=build_process_error_message(user_id, exc, diagnostics=runtime_diagnostics),
            )
            logger.exception(
                "Plan implementation action failed for %s thread %s: %r",
                user_id,
                thread_key,
                exc,
            )
            return

        next_session_id = details["next_session_id"]
        planning_session_id = details["planning_session_id"]
        workdir = details["workdir"]
        prefix = (
            "已切换到新的 implementation session。"
            if execution_mode == "clean"
            else "已在当前 session 中继续实施这份方案。"
        )
        result = (
            f"{prefix}\n\n"
            f"- planning_session_id: `{planning_session_id or '-'}`\n"
            f"- implementation_session_id: `{next_session_id}`\n"
            f"- workdir: `{workdir}`\n\n"
            f"{codex_result.text}"
        )
        result = sanitize_plan_mode_response_for_slack(result)
        post_chunks(client, channel_id, thread_ts, result)
        if response_contains_proposed_plan(codex_result.text):
            persist_latest_proposed_plan(
                thread_key,
                codex_result.text,
                session_id=next_session_id,
                owner_user_id=user_id,
            )
            post_thread_plan_actions_message(
                client,
                channel_id,
                thread_ts,
                thread_key,
                session_id=next_session_id,
            )

    @app.action(REQUEST_USER_INPUT_OPEN_ACTION)
    def handle_request_user_input_open(ack, body, client, logger):
        ack()
        user = body.get("user", {}) or {}
        user_id = user.get("id", "")
        if not user_id:
            return
        if not is_allowed_slack_user(user_id):
            logger.warning("Rejected request_user_input open from unauthorized user %s", user_id)
            return
        actions = body.get("actions", []) or []
        action = actions[0] if actions else {}
        try:
            token = decode_request_user_input_action_value(action.get("value"))
        except Exception as exc:
            logger.exception("Invalid request_user_input open payload from %s: %r", user_id, exc)
            return
        pending_request = get_pending_user_input_request(token)
        channel_id, thread_ts, _message_ts = extract_action_channel_thread(body)
        if not pending_request:
            if channel_id and thread_ts:
                client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text="这次补充输入请求已经结束或失效了。",
                )
            return
        if pending_request.owner_user_id and pending_request.owner_user_id != user_id:
            logger.warning(
                "Rejected request_user_input open for non-owner user %s token %s",
                user_id,
                token,
            )
            return
        trigger_id = body.get("trigger_id", "")
        if not trigger_id:
            return
        try:
            client.views_open(
                trigger_id=trigger_id,
                view=build_request_user_input_modal(pending_request),
            )
        except Exception as exc:  # pragma: no cover
            logger.exception("Failed opening request_user_input modal for %s: %r", user_id, exc)

    @app.action(REQUEST_USER_INPUT_CANCEL_ACTION)
    def handle_request_user_input_cancel(ack, body, client, logger):
        ack()
        user = body.get("user", {}) or {}
        user_id = user.get("id", "")
        if not user_id:
            return
        if not is_allowed_slack_user(user_id):
            logger.warning("Rejected request_user_input cancel from unauthorized user %s", user_id)
            return
        actions = body.get("actions", []) or []
        action = actions[0] if actions else {}
        try:
            token = decode_request_user_input_action_value(action.get("value"))
        except Exception as exc:
            logger.exception("Invalid request_user_input cancel payload from %s: %r", user_id, exc)
            return
        pending_request = get_pending_user_input_request(token)
        if not pending_request:
            return
        if pending_request.owner_user_id and pending_request.owner_user_id != user_id:
            logger.warning(
                "Rejected request_user_input cancel for non-owner user %s token %s",
                user_id,
                token,
            )
            return
        if resolve_pending_user_input_request(token, {"answers": {}}):
            update_request_user_input_prompt_message(
                client,
                pending_request,
                "这次补充输入已取消，Codex 会继续按空回答处理。",
            )

    @app.view(REQUEST_USER_INPUT_SUBMIT_CALLBACK)
    def handle_request_user_input_submit(ack, body, view, client, logger):
        user = body.get("user", {}) or {}
        user_id = user.get("id", "")
        if not user_id:
            ack()
            return
        if not is_allowed_slack_user(user_id):
            logger.warning("Rejected request_user_input submit from unauthorized user %s", user_id)
            ack()
            return
        try:
            token = decode_request_user_input_action_value(view.get("private_metadata"))
        except Exception as exc:
            logger.exception("Invalid request_user_input submit payload from %s: %r", user_id, exc)
            ack()
            return
        pending_request = get_pending_user_input_request(token)
        if not pending_request:
            ack()
            return
        if pending_request.owner_user_id and pending_request.owner_user_id != user_id:
            logger.warning(
                "Rejected request_user_input submit for non-owner user %s token %s",
                user_id,
                token,
            )
            ack()
            return
        response_payload, errors = extract_request_user_input_submission(
            view.get("state"),
            pending_request,
        )
        if errors:
            ack(response_action="errors", errors=errors)
            return
        ack()
        if resolve_pending_user_input_request(token, response_payload):
            update_request_user_input_prompt_message(
                client,
                pending_request,
                "已收到你的补充输入，Codex 正在继续。",
            )

    @app.event("app_mention")
    def handle_app_mention(body, client, logger):
        event = body.get("event", {})
        if event.get("bot_id") or event.get("bot_profile"):
            return

        prompt = strip_app_mentions(event.get("text", ""))
        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]
        user_id = event.get("user", "")
        if not is_allowed_slack_user(user_id):
            logger.warning("Rejected app_mention from unauthorized user %s", user_id)
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f"<@{user_id}> 你没有权限使用这个 Codex bot。",
            )
            return
        logger.info("Handling app_mention in channel %s", channel)
        start_background_job(client, channel, thread_ts, prompt, user_id, slack_event_payload=body)

    @app.event("message")
    def handle_direct_message(body, client, logger):
        event = body.get("event", {})
        if event.get("bot_id"):
            return
        subtype = str(event.get("subtype") or "").strip()
        if subtype and subtype != "file_share":
            return
        if event.get("channel_type") != "im":
            return

        prompt = (event.get("text") or "").strip()
        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]
        user_id = event.get("user", "")
        if not is_allowed_slack_user(user_id):
            logger.warning("Rejected direct message from unauthorized user %s", user_id)
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="你没有权限使用这个 Codex bot。",
            )
            return
        logger.info("Handling direct message in channel %s", channel)
        start_background_job(client, channel, thread_ts, prompt, user_id, slack_event_payload=body)

    with suppress(Exception):
        restore_result = restore_background_watchers(app.client)
        print(
            "[watcher_restore]"
            f" restored={restore_result.get('restored_count', 0)}"
            f" skipped={len(restore_result.get('skipped', []))}",
            flush=True,
        )
        for row in restore_result.get("restored", []):
            print(
                "[watcher_restore_ok]"
                f" thread_key={row.get('thread_key')}"
                f" session_id={row.get('session_id')}"
                f" mode={row.get('mode')}"
                f" persist_watch={row.get('persist_watch')}"
                f" stop_when_idle={row.get('stop_when_idle')}"
                f" cursor_source={row.get('cursor_source')}"
                f" last_event_key={row.get('last_event_key')}",
                flush=True,
            )
        for row in restore_result.get("skipped", []):
            print(
                "[watcher_restore_skip]"
                f" thread_key={row.get('thread_key')}"
                f" session_id={row.get('session_id')}"
                f" reason={row.get('reason')}",
                flush=True,
            )

    return app


def is_retryable_slack_startup_error(exc):
    current = exc
    visited = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, (urllib.error.URLError, ssl.SSLError, socket.gaierror, TimeoutError)):
            return True
        if isinstance(current, ConnectionError):
            return True
        if isinstance(current, SlackApiError):
            response = getattr(current, "response", None)
            status_code = getattr(response, "status_code", None)
            if status_code in {429, 500, 502, 503, 504}:
                return True
            error_code = ""
            if response is not None:
                with suppress(Exception):
                    error_code = str(response.get("error") or "").strip().lower()
            if error_code in {
                "ratelimited",
                "internal_error",
                "fatal_error",
                "request_timeout",
                "service_unavailable",
            }:
                return True
            return False
        current = current.__cause__ or current.__context__
    return False


def format_startup_exception(exc):
    return compact_exception_text(exc, max_length=500)


def run_socket_mode_forever(
    *,
    app_factory=build_app,
    handler_factory=SocketModeHandler,
    sleep_fn=time.sleep,
):
    attempt = 0
    delay_seconds = get_slack_startup_retry_initial_seconds()
    max_delay_seconds = get_slack_startup_retry_max_seconds()
    while True:
        attempt += 1
        try:
            app = app_factory()
            handler = handler_factory(app, ENV["SLACK_APP_TOKEN"])
            handler.start()
            return
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            if not is_retryable_slack_startup_error(exc):
                raise
            print(
                "[slack_startup_retry]"
                f" attempt={attempt}"
                f" delay_seconds={delay_seconds:.1f}"
                f" error={format_startup_exception(exc)}",
                flush=True,
            )
            sleep_fn(delay_seconds)
            delay_seconds = min(max_delay_seconds, delay_seconds * 2)


def log_session_event(event, thread_key, existing_session_id=None, next_session_id=None):
    print(
        "[session]"
        f" event={event}"
        f" thread_key={thread_key}"
        f" existing_session_id={existing_session_id or '-'}"
        f" next_session_id={next_session_id or '-'}",
        flush=True,
    )


def log_codex_command(mode, workdir, args):
    log_args = list(args)
    if log_args and isinstance(log_args[-1], str):
        log_args[-1] = summarize_text_for_log(log_args[-1])
    print(
        "[codex_cmd]"
        f" mode={mode}"
        f" cwd={workdir}"
        f" args={json.dumps(log_args, ensure_ascii=True)}",
        flush=True,
    )


def summarize_text_for_log(text):
    return f"<chars={len(text or '')}>"


def log_codex_result(mode, exit_code, raw_output, final_output):
    print(
        "[codex_exit]"
        f" mode={mode}"
        f" exit_code={exit_code}"
        f" raw_output={json.dumps(summarize_text_for_log(raw_output), ensure_ascii=True)}"
        f" final_output={json.dumps(summarize_text_for_log(final_output), ensure_ascii=True)}",
        flush=True,
    )


def acquire_instance_lock():
    if fcntl is None:
        return None

    INSTANCE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = INSTANCE_LOCK_PATH.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.seek(0)
        existing_pid = handle.read().strip()
        handle.close()
        details = f" (pid {existing_pid})" if existing_pid else ""
        raise RuntimeError(
            "Another codex-slack server.py instance is already running"
            f"{details}. Stop it before starting a new one."
        ) from exc

    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


def release_instance_lock():
    global INSTANCE_LOCK_HANDLE
    if INSTANCE_LOCK_HANDLE is None:
        return

    try:
        lock_path_matches = False
        if INSTANCE_LOCK_PATH.exists():
            lock_path_matches = INSTANCE_LOCK_PATH.read_text(encoding="utf-8").strip() == str(os.getpid())
        INSTANCE_LOCK_HANDLE.close()
        if lock_path_matches:
            INSTANCE_LOCK_PATH.unlink(missing_ok=True)
    finally:
        INSTANCE_LOCK_HANDLE = None


def main():
    global INSTANCE_LOCK_HANDLE
    validate_env()
    INSTANCE_LOCK_HANDLE = acquire_instance_lock()
    atexit.register(release_instance_lock)
    run_socket_mode_forever()


if __name__ == "__main__":
    main()
