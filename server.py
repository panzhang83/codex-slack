import atexit
import asyncio
import json
import os
import queue
import re
import shlex
import subprocess
import tempfile
import threading
import time
import warnings
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

warnings.filterwarnings(
    "ignore",
    message=r'Field ".*" .* protected namespace "model_".*',
    category=UserWarning,
)

try:
    from codex_app_server_sdk import CodexClient
    from codex_app_server_sdk.transport import CodexTransportError, StdioTransport
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "Missing dependency `codex-app-server-sdk`. "
        "Run `pip install -r requirements.txt` before starting codex-slack."
    ) from exc
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

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
SESSION_MODE_OBSERVE = "observe"
SESSION_MODE_CONTROL = "control"
DEFAULT_WATCH_POLL_SECONDS = 5
DEFAULT_PROGRESS_HEARTBEAT_SECONDS = 300
DEFAULT_PROGRESS_POLL_SECONDS = 15
MAX_APP_SERVER_RETRIES = 2
MAX_WATCH_READ_FAILURES = 2
DEFAULT_APP_SERVER_STDIO_LINE_LIMIT_BYTES = 32 * 1024 * 1024


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
            if not isinstance(value, dict) or not isinstance(value.get("session_id"), str):
                continue

            entry = {
                "session_id": value["session_id"],
                "updated_at": value.get("updated_at", 0),
            }
            mode = value.get("mode")
            if mode in {SESSION_MODE_OBSERVE, SESSION_MODE_CONTROL}:
                entry["mode"] = mode
            owner_user_id = value.get("owner_user_id")
            if isinstance(owner_user_id, str) and owner_user_id:
                entry["owner_user_id"] = owner_user_id
            normalized[key] = entry
        return normalized

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
            return entry.get("mode") or SESSION_MODE_CONTROL

    def get_owner(self, key):
        with self._lock:
            entry = self._sessions.get(key)
            if not entry:
                return None
            return entry.get("owner_user_id")

    def find_owner_for_session(self, session_id):
        with self._lock:
            for entry in self._sessions.values():
                if entry.get("session_id") != session_id:
                    continue
                owner_user_id = entry.get("owner_user_id")
                if owner_user_id:
                    return owner_user_id
            return None

    def set(self, key, session_id, owner_user_id=None):
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
            self._sessions[key] = entry
            self._save_locked()

    def attach_session(self, key, session_id, owner_user_id, allow_unseen=False, mode=SESSION_MODE_OBSERVE):
        with self._lock:
            previous_session_id = self._sessions.get(key, {}).get("session_id")
            existing_thread_owner_user_id = self._sessions.get(key, {}).get("owner_user_id")
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

            self._sessions[key] = {
                "session_id": session_id,
                "updated_at": int(time.time()),
                "owner_user_id": owner_user_id,
                "mode": mode if mode in {SESSION_MODE_OBSERVE, SESSION_MODE_CONTROL} else SESSION_MODE_OBSERVE,
            }
            self._save_locked()
            return previous_session_id, None

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

    def touch(self, key):
        with self._lock:
            entry = self._sessions.get(key)
            if not entry:
                return
            entry["updated_at"] = int(time.time())
            self._save_locked()


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
class ConversationEvent:
    turn_id: str
    item_id: str
    role: str
    text: str


@dataclass(frozen=True)
class ProgressEvent:
    turn_id: str
    item_id: str
    phase: str
    text: str


class WatchAnchorLostError(RuntimeError):
    pass


class LargePayloadStdioTransport(StdioTransport):
    """SDK stdio transport with a larger stdout line limit for huge thread/read payloads."""

    def __init__(self, command, *, cwd=None, env=None, connect_timeout=30.0, line_limit_bytes=None):
        super().__init__(
            command,
            cwd=cwd,
            env=env,
            connect_timeout=connect_timeout,
        )
        self._line_limit_bytes = int(line_limit_bytes or DEFAULT_APP_SERVER_STDIO_LINE_LIMIT_BYTES)

    async def connect(self) -> None:
        if self._proc is not None:
            return
        try:
            self._proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *self._command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    cwd=self._cwd,
                    env=self._env,
                    limit=self._line_limit_bytes,
                ),
                timeout=self._connect_timeout,
            )
        except Exception as exc:  # pragma: no cover
            raise CodexTransportError(
                f"failed to start stdio transport command: {self._command!r}"
            ) from exc


def chunk_text(text, max_length=3500):
    normalized = (text or "").strip()
    if not normalized:
        return ["Codex returned an empty response."]

    chunks = []
    start = 0
    while start < len(normalized):
        chunks.append(normalized[start : start + max_length])
        start += max_length
    return chunks


def read_field(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def read_root(obj):
    if isinstance(obj, dict):
        return obj.get("root", obj)
    return getattr(obj, "root", obj)


def truncate_text(text, max_length=280):
    normalized = (text or "").strip()
    if len(normalized) <= max_length:
        return normalized
    if max_length <= 15:
        return normalized[:max_length]
    return normalized[: max_length - 14].rstrip() + "...<truncated>"


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


def is_handoff_command(text):
    normalized = (text or "").strip().lower()
    return normalized in {"/handoff", "handoff"}


def is_recap_command(text):
    normalized = (text or "").strip().lower()
    return normalized in {"/recap", "recap"}


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


def is_control_command(text):
    normalized = (text or "").strip().lower()
    return normalized in {"/control", "control", "/takeover", "takeover"}


def is_observe_command(text):
    normalized = (text or "").strip().lower()
    return normalized in {"/observe", "observe", "/release", "release"}


def strip_fresh_command(text):
    return strip_command_payload(text, "fresh") or ""


def get_codex_settings():
    codex_bin = ENV.get("CODEX_BIN", "codex")
    model = ENV.get("OPENAI_MODEL", "gpt-5.4")
    workdir = ENV.get("CODEX_WORKDIR", str(Path.cwd()))
    timeout_raw = ENV.get("CODEX_TIMEOUT_SECONDS", "900")
    try:
        timeout = int(timeout_raw)
    except ValueError as exc:
        raise RuntimeError(f"CODEX_TIMEOUT_SECONDS must be an integer, got: {timeout_raw!r}") from exc
    sandbox = ENV.get("CODEX_SANDBOX", "workspace-write")
    extra_args = ENV.get("CODEX_EXTRA_ARGS", "").strip()
    full_auto = ENV.get("CODEX_FULL_AUTO", "0") == "1"
    return codex_bin, model, workdir, timeout, sandbox, extra_args, full_auto


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


def get_watch_poll_seconds():
    raw = str(ENV.get("CODEX_SLACK_WATCH_POLL_SECONDS", DEFAULT_WATCH_POLL_SECONDS)).strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_WATCH_POLL_SECONDS
    return max(1, value)


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


def get_session_mode(thread_key, session_store=None):
    session_store = session_store or SESSION_STORE
    return session_store.get_mode(thread_key) or SESSION_MODE_CONTROL


def get_observe_mode_error(user_id, session_id):
    return (
        f"<@{user_id}> 当前 Slack thread 绑定的 session `{session_id or '-'}` 处于 `observe` 模式。"
        " 为避免和终端里的交互式 Codex 会话并发写入，普通消息暂不会继续 `resume`。"
        " 如果你确认要由 Slack 接管，请先发送 `control` 或 `takeover`。"
    )


def build_codex_exec_args(prompt, output_file, extra_cli_args=None):
    codex_bin, model, workdir, timeout, sandbox, extra_args, full_auto = get_codex_settings()
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

    args.append(prompt)
    return codex_bin, args, timeout, workdir


def build_codex_resume_args(session_id, prompt, output_file, extra_cli_args=None):
    codex_bin, model, workdir, timeout, _sandbox, _extra_args, full_auto = get_codex_settings()
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
    if extra_cli_args:
        args.extend(extra_cli_args)
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


def run_codex(prompt, session_id=None, session_id_tracker=None):
    with tempfile.NamedTemporaryFile(prefix="codex-last-message-", suffix=".txt", delete=False) as tmp:
        output_file = tmp.name

    try:
        mode = "resume" if session_id else "new"
        if session_id:
            codex_bin, args, timeout, workdir = build_codex_resume_args(session_id, prompt, output_file)
            log_codex_command(mode, workdir, [codex_bin, *args])
        else:
            codex_bin, args, timeout, workdir = build_codex_exec_args(prompt, output_file)
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
                text=f"Codex timed out after {timeout} seconds.",
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
                result_text = f"Codex exited with status {exit_code}.\n\n{fallback_output}".strip()
        else:
            result_text = final_output or json_output or cleaned_output or "Codex finished without returning text."

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
    codex_bin, _model, workdir, _timeout, _sandbox, _extra_args, _full_auto = get_codex_settings()
    transport = LargePayloadStdioTransport(
        [codex_bin, "app-server"],
        cwd=workdir,
        env=build_codex_child_env(),
        line_limit_bytes=get_app_server_stdio_line_limit_bytes(),
    )
    return CodexClient(transport)


async def read_thread_response_async(session_id):
    client = create_app_server_client()
    await client.start()
    await client.initialize()
    try:
        return await client.read_thread(session_id, include_turns=True)
    finally:
        with suppress(Exception):
            await client.close()


def read_thread_response(session_id):
    last_error = None
    for _attempt in range(MAX_APP_SERVER_RETRIES):
        try:
            return asyncio.run(read_thread_response_async(session_id))
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"读取 thread 对话失败: {last_error}")


def format_user_input(user_input):
    root = read_root(user_input)
    input_type = read_field(root, "type")
    if input_type == "text":
        return (read_field(root, "text", "") or "").strip()
    if input_type == "image":
        return "[image]"
    if input_type == "localImage":
        return f"[local image: {read_field(root, 'path', '-') or '-'}]"
    if input_type == "skill":
        return f"[skill: {read_field(root, 'name', '-') or '-'}]"
    if input_type == "mention":
        return f"[mention: {read_field(root, 'name', '-') or '-'}]"
    return ""


def format_user_message_content(content_items):
    parts = []
    for item in content_items or []:
        part = format_user_input(item)
        if part:
            parts.append(part)
    return "\n".join(parts).strip()


def is_final_answer_phase(phase):
    return phase == "final_answer"


def is_progress_phase(phase):
    return bool(phase) and phase != "final_answer"


def extract_conversation_events(thread_read_response):
    thread = read_field(thread_read_response, "thread", thread_read_response)
    events = []
    fallback_index = 0

    for turn in read_field(thread, "turns", []) or []:
        turn_id = read_field(turn, "id") or f"turn-{fallback_index}"
        for item in read_field(turn, "items", []) or []:
            root = read_root(item)
            item_type = read_field(root, "type")
            item_id = read_field(root, "id") or f"{turn_id}:item-{fallback_index}"
            fallback_index += 1

            if item_type == "userMessage":
                text = format_user_message_content(read_field(root, "content", []) or [])
                if text:
                    events.append(ConversationEvent(turn_id=turn_id, item_id=item_id, role="user", text=text))
                continue

            if item_type != "agentMessage":
                continue

            if not is_final_answer_phase(read_field(root, "phase")):
                continue

            text = (read_field(root, "text", "") or "").strip()
            if text:
                events.append(ConversationEvent(turn_id=turn_id, item_id=item_id, role="assistant", text=text))

    return events


def extract_progress_events(thread_read_response):
    thread = read_field(thread_read_response, "thread", thread_read_response)
    events = []
    fallback_index = 0

    for turn in read_field(thread, "turns", []) or []:
        turn_id = read_field(turn, "id") or f"turn-{fallback_index}"
        for item in read_field(turn, "items", []) or []:
            root = read_root(item)
            item_type = read_field(root, "type")
            item_id = read_field(root, "id") or f"{turn_id}:progress-{fallback_index}"
            fallback_index += 1

            if item_type != "agentMessage":
                continue

            phase = read_field(root, "phase")
            if not is_progress_phase(phase):
                continue

            text = (read_field(root, "text", "") or "").strip()
            if text:
                events.append(ProgressEvent(turn_id=turn_id, item_id=item_id, phase=phase, text=text))

    return events


def read_conversation_events(session_id):
    return extract_conversation_events(read_thread_response(session_id))


def get_event_key(event):
    return (event.turn_id, event.item_id)


def get_recent_turn_events(events):
    if not events:
        return []
    last_turn_id = events[-1].turn_id
    return [event for event in events if event.turn_id == last_turn_id]


def get_latest_completed_turn_events(events):
    if not events:
        return []

    grouped_turns = []
    current_turn_id = None
    current_events = []
    for event in events:
        if event.turn_id != current_turn_id:
            if current_events:
                grouped_turns.append(current_events)
            current_turn_id = event.turn_id
            current_events = [event]
        else:
            current_events.append(event)
    if current_events:
        grouped_turns.append(current_events)

    for turn_events in reversed(grouped_turns):
        if any(event.role == "assistant" for event in turn_events):
            return turn_events
    return []


def get_events_after_key(events, last_key):
    if last_key is None:
        return list(events)

    for index, event in enumerate(events):
        if get_event_key(event) == last_key:
            return events[index + 1 :]

    raise WatchAnchorLostError(f"watch anchor {last_key!r} is no longer present in the current thread view")


def format_conversation_events(events, heading=None):
    if not events:
        return "当前 thread 还没有可显示的对话内容。"

    blocks = []
    if heading:
        blocks.append(heading)

    for event in events:
        label = "User" if event.role == "user" else "Codex"
        quoted_text = "\n".join(
            f"> {line}" if line else ">"
            for line in (event.text or "").splitlines()
        ).strip()
        blocks.append(f"*{label}*\n{quoted_text}")

    return "\n\n".join(blocks).strip()


def build_watch_bootstrap(session_id):
    events = read_conversation_events(session_id)
    bootstrap_events = get_latest_completed_turn_events(events) or get_recent_turn_events(events)
    last_event_key = get_event_key(bootstrap_events[-1]) if bootstrap_events else None
    return format_conversation_events(bootstrap_events, heading="最近一轮对话:"), last_event_key


def capture_progress_baseline(session_id):
    try:
        progress_events = extract_progress_events(read_thread_response(session_id))
    except Exception:
        return {}
    return {event.item_id: event.text for event in progress_events}


def format_progress_message(text):
    quoted_text = "\n".join(
        f"> {line}" if line else ">"
        for line in (text or "").splitlines()
    ).strip()
    return f"*Codex Progress*\n{quoted_text}"


def build_progress_messages(progress_events, previous_text_by_item_id):
    messages = []

    for event in progress_events:
        previous_text = previous_text_by_item_id.get(event.item_id)
        current_text = event.text
        if previous_text == current_text:
            continue

        display_text = current_text
        if previous_text and current_text.startswith(previous_text):
            delta_text = current_text[len(previous_text) :].strip()
            if not delta_text:
                previous_text_by_item_id[event.item_id] = current_text
                continue
            display_text = delta_text
        else:
            display_text = truncate_text(current_text, max_length=1200)

        previous_text_by_item_id[event.item_id] = current_text
        messages.append(format_progress_message(display_text))

    return messages


def maybe_post_progress_updates(client, channel, thread_ts, session_id, previous_text_by_item_id):
    try:
        progress_events = extract_progress_events(read_thread_response(session_id))
    except Exception:
        return

    for message in build_progress_messages(progress_events, previous_text_by_item_id):
        post_chunks(client, channel, thread_ts, message)


def run_codex_with_updates(client, channel, thread_ts, prompt, session_id=None, enable_progress=False):
    result_box = {}
    error_box = {}
    session_id_tracker = SessionIdTracker(session_id=session_id)

    def worker():
        try:
            result_box["result"] = run_codex(
                prompt,
                session_id=session_id,
                session_id_tracker=session_id_tracker,
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


def stop_watcher(thread_key):
    with WATCHERS_GUARD:
        watcher = WATCHERS.pop(thread_key, None)
    if watcher is None:
        return False
    watcher.stop_event.set()
    return True


def watch_loop(client, channel, thread_ts, thread_key, session_id, stop_event, last_event_key=None):
    failure_count = 0
    current_last_event_key = last_event_key
    poll_seconds = get_watch_poll_seconds()

    try:
        while not stop_event.wait(poll_seconds):
            if SESSION_STORE.get(thread_key) != session_id:
                break

            try:
                events = read_conversation_events(session_id)
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

            try:
                new_events = get_events_after_key(events, current_last_event_key)
            except WatchAnchorLostError:
                post_chunks(
                    client,
                    channel,
                    thread_ts,
                    "持续 watch 已停止：当前 thread 对话锚点已经失效。请重新发送 `watch` 重新建立镜像。",
                )
                break
            if not new_events:
                continue

            current_last_event_key = get_event_key(new_events[-1])
            post_chunks(client, channel, thread_ts, format_conversation_events(new_events))
    finally:
        watcher = get_watcher(thread_key)
        if watcher and watcher.stop_event is stop_event:
            clear_watcher(thread_key, watcher)


def start_watcher(client, channel, thread_ts, thread_key, session_id, last_event_key=None):
    stop_watcher(thread_key)
    stop_event = threading.Event()
    thread = threading.Thread(
        target=watch_loop,
        args=(client, channel, thread_ts, thread_key, session_id, stop_event, last_event_key),
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
    return watcher


def process_prompt(client, channel, thread_ts, prompt, user_id):
    thread_key = make_thread_key(channel, thread_ts)
    try:
        if not prompt:
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="给我一个具体任务，再让我调用 Codex。",
            )
            return

        lock = claim_thread_lock(thread_key)
        try:
            with lock:
                owner_error = get_thread_owner_access_error(thread_key, user_id)
                if owner_error:
                    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=owner_error)
                    return

                current_session_id = SESSION_STORE.get(thread_key)
                current_session_mode = get_session_mode(thread_key)

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
                    workdir = ENV.get("CODEX_WORKDIR", str(Path.cwd()))
                    with session_execution_guard(current_session_id):
                        codex_result = run_codex_with_updates(
                            client,
                            channel,
                            thread_ts,
                            build_handoff_prompt(),
                            session_id=current_session_id,
                            enable_progress=False,
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
                    if should_update_session_activity(codex_result) and next_session_id != current_session_id:
                        SESSION_STORE.set(thread_key, next_session_id, owner_user_id=user_id)
                    elif should_update_session_activity(codex_result):
                        SESSION_STORE.touch(thread_key)
                    log_session_event(
                        "handoff",
                        thread_key,
                        existing_session_id=current_session_id,
                        next_session_id=next_session_id,
                    )
                    post_chunks(client, channel, thread_ts, result)
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
                    with session_execution_guard(current_session_id):
                        codex_result = run_codex_with_updates(
                            client,
                            channel,
                            thread_ts,
                            build_recap_prompt(),
                            session_id=current_session_id,
                            enable_progress=False,
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
                    if should_update_session_activity(codex_result) and next_session_id != current_session_id:
                        SESSION_STORE.set(thread_key, next_session_id, owner_user_id=user_id)
                    elif should_update_session_activity(codex_result):
                        SESSION_STORE.touch(thread_key)
                    log_session_event(
                        "recap",
                        thread_key,
                        existing_session_id=current_session_id,
                        next_session_id=next_session_id,
                    )
                    post_chunks(client, channel, thread_ts, result)
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
                    codex_bin, model, workdir, timeout, sandbox, _extra_args, _full_auto = get_codex_settings()
                    watch_active = "yes" if get_watcher(thread_key) else "no"
                    client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=(
                            f"<@{user_id}> 当前 Slack thread 的运行状态:\n\n"
                            f"- thread_key: `{thread_key}`\n"
                            f"- session_id: `{current_session_id or '-'}`\n"
                            f"- session_mode: `{current_session_mode if current_session_id else '-'}`\n"
                            f"- model: `{model}`\n"
                            f"- workdir: `{workdir}`\n"
                            f"- sandbox: `{sandbox or '-'}`\n"
                            f"- timeout_seconds: `{timeout}`\n"
                            f"- codex_bin: `{codex_bin}`\n"
                            f"- watch_active: `{watch_active}`\n"
                            "如果你想让终端继续同一个会话，可以在终端里使用这个 `session_id` 执行 `codex exec resume ...`。"
                        ),
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
                    previous_session_id = None
                    if not normalized_session_id:
                        attach_error = "请用 `attach <session_id>` 绑定一个已有的 Codex 会话。"
                    elif not is_valid_attach_session_id(normalized_session_id):
                        attach_error = (
                            "`attach` 目前只接受 Codex session UUID，例如 "
                            "`attach 019d5868-71ba-7101-9143-81867f3db5bf`。"
                        )
                    else:
                        previous_session_id, attach_error = SESSION_STORE.attach_session(
                            thread_key,
                            normalized_session_id,
                            owner_user_id=user_id,
                            allow_unseen=is_unseen_attach_allowed(user_id),
                            mode=SESSION_MODE_OBSERVE,
                        )
                    if attach_error:
                        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=attach_error)
                        return

                    log_session_event(
                        "attach",
                        thread_key,
                        existing_session_id=previous_session_id,
                        next_session_id=normalized_session_id,
                    )
                    stop_watcher(thread_key)
                    client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=(
                            f"<@{user_id}> 当前 Slack thread 已绑定到 Codex session `{normalized_session_id}`。\n\n"
                            "默认已进入 `observe` 模式。你可以先用 `watch`、`where`、`session` 查看 thread 对话。"
                            " 如果你确认要由 Slack 接管，再发送 `control` 或 `takeover`。"
                        ),
                    )
                    return

                if is_reset_command(prompt):
                    previous_session_id = SESSION_STORE.get(thread_key)
                    stop_watcher(thread_key)
                    SESSION_STORE.delete(thread_key)
                    log_session_event("reset", thread_key, existing_session_id=previous_session_id)
                    client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=f"<@{user_id}> 当前 Slack thread 的 Codex 会话已重置。",
                    )
                    return

                force_fresh = is_fresh_command(prompt)
                effective_prompt = strip_fresh_command(prompt) if force_fresh else prompt
                if not effective_prompt:
                    client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text="`/fresh` 后面要跟具体任务。",
                    )
                    return

                existing_session_id = None if force_fresh else current_session_id
                if existing_session_id and current_session_mode != SESSION_MODE_CONTROL:
                    client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=get_observe_mode_error(user_id, existing_session_id),
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

                with session_execution_guard(existing_session_id):
                    codex_result = run_codex_with_updates(
                        client,
                        channel,
                        thread_ts,
                        effective_prompt,
                        session_id=existing_session_id,
                        enable_progress=True,
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
                        SESSION_STORE.delete(thread_key)
                        client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts,
                            text=f"<@{user_id}> 当前 Slack thread 的 Codex 会话不可恢复，正在自动重建新会话。",
                        )
                        codex_result = run_codex_with_updates(
                            client,
                            channel,
                            thread_ts,
                            effective_prompt,
                            enable_progress=True,
                        )
                        next_session_id = codex_result.session_id
                        result = codex_result.text
                        print(
                            "[codex_result]"
                            f" thread_key={thread_key}"
                            f" rebuilt=1"
                            f" result_length={len(result or '')}",
                            flush=True,
                        )

                if should_update_session_activity(codex_result) and next_session_id != existing_session_id:
                    SESSION_STORE.set(thread_key, next_session_id, owner_user_id=user_id)
                elif should_update_session_activity(codex_result):
                    SESSION_STORE.touch(thread_key)

                log_session_event(
                    "completed",
                    thread_key,
                    existing_session_id=existing_session_id,
                    next_session_id=next_session_id,
                )
                post_chunks(client, channel, thread_ts, result)
        finally:
            release_thread_lock(thread_key)
    except Exception as exc:
        try:
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f"<@{user_id}> 处理这条请求时发生了内部错误，请稍后重试并检查服务日志。",
            )
        except Exception:
            pass
        print(
            "[process_error]"
            f" thread_key={thread_key}"
            f" error={exc!r}",
            flush=True,
        )
        raise


def start_background_job(client, channel, thread_ts, prompt, user_id):
    thread = threading.Thread(
        target=process_prompt,
        args=(client, channel, thread_ts, prompt, user_id),
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


def build_app():
    app = App(
        token=ENV["SLACK_BOT_TOKEN"],
        signing_secret=ENV.get("SLACK_SIGNING_SECRET", ""),
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
        start_background_job(client, channel, thread_ts, prompt, user_id)

    @app.event("message")
    def handle_direct_message(body, client, logger):
        event = body.get("event", {})
        if event.get("bot_id") or event.get("subtype"):
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
        start_background_job(client, channel, thread_ts, prompt, user_id)

    return app


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
    app = build_app()
    handler = SocketModeHandler(app, ENV["SLACK_APP_TOKEN"])
    handler.start()


if __name__ == "__main__":
    main()
