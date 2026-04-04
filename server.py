import os
import re
import shlex
import tempfile
import threading
import json
import time
from pathlib import Path

import pexpect
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler


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
)
THREAD_LOCKS = {}
THREAD_LOCKS_GUARD = threading.Lock()


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
            elif isinstance(value, dict) and isinstance(value.get("session_id"), str):
                normalized[key] = {
                    "session_id": value["session_id"],
                    "updated_at": value.get("updated_at", 0),
                }
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

    def set(self, key, session_id):
        with self._lock:
            self._sessions[key] = {
                "session_id": session_id,
                "updated_at": int(time.time()),
            }
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


def strip_app_mentions(text):
    return re.sub(r"<@[A-Z0-9]+>", "", text or "").strip()


def is_reset_command(text):
    normalized = (text or "").strip().lower()
    return normalized in {"/reset", "reset", "reset session", "/reset-session"}


def is_fresh_command(text):
    normalized = (text or "").strip()
    return (
        normalized.startswith("/fresh ")
        or normalized == "/fresh"
        or normalized.startswith("fresh ")
        or normalized == "fresh"
    )


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


def is_attach_command(text):
    normalized = (text or "").strip()
    return normalized.startswith("/attach ") or normalized.startswith("attach ")


def strip_attach_command(text):
    normalized = (text or "").strip()
    if normalized.startswith("/attach "):
        return normalized[len("/attach ") :].strip()
    if normalized.startswith("attach "):
        return normalized[len("attach ") :].strip()
    return ""


def strip_fresh_command(text):
    normalized = (text or "").strip()
    if normalized in {"/fresh", "fresh"}:
        return ""
    if normalized.startswith("/fresh "):
        return normalized[len("/fresh ") :].strip()
    if normalized.startswith("fresh "):
        return normalized[len("fresh ") :].strip()
    return normalized


def get_codex_settings():
    codex_bin = ENV.get("CODEX_BIN", "codex")
    model = ENV.get("OPENAI_MODEL", "gpt-5.4")
    workdir = ENV.get("CODEX_WORKDIR", str(Path.cwd()))
    timeout = int(ENV.get("CODEX_TIMEOUT_SECONDS", "900"))
    sandbox = ENV.get("CODEX_SANDBOX", "workspace-write")
    extra_args = ENV.get("CODEX_EXTRA_ARGS", "").strip()
    full_auto = ENV.get("CODEX_FULL_AUTO", "0") == "1"
    return codex_bin, model, workdir, timeout, sandbox, extra_args, full_auto


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


def build_codex_exec_args(prompt, output_file):
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

    args.append(prompt)
    return codex_bin, args, timeout, workdir


def build_codex_resume_args(session_id, prompt, output_file):
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
        if lower.startswith("thinking"):
            continue
        if lower.startswith("working"):
            continue
        if lower.startswith("running"):
            continue
        if lower.startswith("checking"):
            continue
        if lower.startswith("searching"):
            continue
        if lower.startswith("reading"):
            continue
        if lower.startswith("tool call"):
            continue
        if lower.startswith("exec_command"):
            continue
        if lower.startswith("apply_patch"):
            continue
        if lower.startswith("function call"):
            continue
        if lower.startswith("response_item"):
            continue
        if lower.startswith("commentary"):
            continue
        filtered.append(line)

    cleaned = "\n".join(filtered).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


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
        "\n\nTerminal Verify Command:\n"
        "`printenv CODEX_THREAD_ID && pwd`\n"
        f"Expected Session ID: `{session_id or '-'}`\n"
        f"Expected Workdir: `{workdir}`"
    )
    return (base + footer).strip()


def append_recap_footer(text, session_id):
    base = (text or "").strip()
    footer = f"\n\nCurrent Session ID: `{session_id or '-'}`"
    return (base + footer).strip()


def parse_codex_json_events(text):
    session_id = None
    messages = []

    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type")
        if event_type == "thread.started":
            session_id = event.get("thread_id") or session_id
        if event_type != "item.completed":
            continue

        item = event.get("item") or {}
        if item.get("type") != "agent_message":
            continue

        text = item.get("text")
        if text:
            messages.append(text)

    return session_id, "\n\n".join(messages).strip()


def run_codex(prompt, session_id=None):
    with tempfile.NamedTemporaryFile(prefix="codex-last-message-", suffix=".txt", delete=False) as tmp:
        output_file = tmp.name

    mode = "resume" if session_id else "new"
    if session_id:
        codex_bin, args, timeout, workdir = build_codex_resume_args(session_id, prompt, output_file)
        log_codex_command(mode, workdir, [codex_bin, *args])
    else:
        codex_bin, args, timeout, workdir = build_codex_exec_args(prompt, output_file)
        log_codex_command(mode, workdir, [codex_bin, *args])
    child_env = os.environ.copy()
    child_env.update(ENV)
    child = pexpect.spawn(
        codex_bin,
        args=args,
        encoding="utf-8",
        timeout=timeout,
        env=child_env,
        cwd=workdir,
    )

    try:
        child.expect(pexpect.EOF)
        raw_output = child.before or ""
    except pexpect.TIMEOUT:
        child.close(force=True)
        return session_id, f"Codex timed out after {timeout} seconds."
    finally:
        if child.isalive():
            child.close()

    try:
        final_output = read_output_file(output_file)
    finally:
        Path(output_file).unlink(missing_ok=True)

    parsed_session_id, json_output = parse_codex_json_events(raw_output)
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
    cleaned_output = clean_codex_output(raw_output)
    exit_code = child.exitstatus if child.exitstatus is not None else child.signalstatus
    log_codex_result(mode, exit_code, raw_output, final_output)
    if exit_code not in (0, None):
        if final_output:
            return effective_session_id, final_output
        fallback_output = json_output or cleaned_output
        return effective_session_id, f"Codex exited with status {exit_code}.\n\n{fallback_output}".strip()

    return effective_session_id, final_output or json_output or cleaned_output or "Codex finished without returning text."


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


def make_thread_key(channel, thread_ts):
    return f"{channel}:{thread_ts}"


def get_thread_lock(thread_key):
    with THREAD_LOCKS_GUARD:
        lock = THREAD_LOCKS.get(thread_key)
        if lock is None:
            lock = threading.Lock()
            THREAD_LOCKS[thread_key] = lock
        return lock


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

        lock = get_thread_lock(thread_key)
        with lock:
            if is_handoff_command(prompt):
                current_session_id = SESSION_STORE.get(thread_key)
                if not current_session_id:
                    client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=f"<@{user_id}> 当前 Slack thread 还没有 Codex session，暂时无法生成 handoff note。",
                    )
                    return

                client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=f"<@{user_id}> 正在基于当前 session 整理 handoff note，请稍等。",
                )
                _codex_bin, _model, workdir, _timeout, _sandbox, _extra_args, _full_auto = get_codex_settings()
                next_session_id, result = run_codex(
                    build_handoff_prompt(),
                    session_id=current_session_id,
                )
                result = append_handoff_footer(result, next_session_id or current_session_id, workdir)
                print(
                    "[codex_result]"
                    f" thread_key={thread_key}"
                    f" handoff=1"
                    f" result_length={len(result or '')}",
                    flush=True,
                )
                if next_session_id and next_session_id != current_session_id:
                    SESSION_STORE.set(thread_key, next_session_id)
                elif next_session_id:
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
                current_session_id = SESSION_STORE.get(thread_key)
                if not current_session_id:
                    client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=f"<@{user_id}> 当前 Slack thread 还没有 Codex session，暂时无法生成 recap。",
                    )
                    return

                client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=f"<@{user_id}> 正在整理当前 session 的 recap，请稍等。",
                )
                next_session_id, result = run_codex(
                    build_recap_prompt(),
                    session_id=current_session_id,
                )
                result = append_recap_footer(result, next_session_id or current_session_id)
                print(
                    "[codex_result]"
                    f" thread_key={thread_key}"
                    f" recap=1"
                    f" result_length={len(result or '')}",
                    flush=True,
                )
                if next_session_id and next_session_id != current_session_id:
                    SESSION_STORE.set(thread_key, next_session_id)
                elif next_session_id:
                    SESSION_STORE.touch(thread_key)
                log_session_event(
                    "recap",
                    thread_key,
                    existing_session_id=current_session_id,
                    next_session_id=next_session_id,
                )
                post_chunks(client, channel, thread_ts, result)
                return

            if is_status_command(prompt):
                current_session_id = SESSION_STORE.get(thread_key)
                codex_bin, model, workdir, timeout, sandbox, extra_args, full_auto = get_codex_settings()
                client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=(
                        f"<@{user_id}> 当前 Slack thread 的运行状态:\n\n"
                        f"- thread_key: `{thread_key}`\n"
                        f"- session_id: `{current_session_id or '-'}`\n"
                        f"- model: `{model}`\n"
                        f"- workdir: `{workdir}`\n"
                        f"- sandbox: `{sandbox or '-'}`\n"
                        f"- timeout_seconds: `{timeout}`\n"
                        f"- codex_bin: `{codex_bin}`\n"
                        f"- full_auto: `{1 if full_auto else 0}`\n"
                        f"- extra_args: `{extra_args or '-'}`\n"
                        f"- session_store: `{SESSION_STORE_PATH}`\n\n"
                        "如果你想让终端继续同一个会话，可以在终端里使用这个 `session_id` 执行 `codex exec resume ...`。"
                    ),
                )
                return

            if is_session_command(prompt):
                current_session_id = SESSION_STORE.get(thread_key)
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
                if not attach_session_id:
                    client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text="请用 `attach <session_id>` 绑定一个已有的 Codex 会话。",
                    )
                    return

                previous_session_id = SESSION_STORE.get(thread_key)
                SESSION_STORE.set(thread_key, attach_session_id)
                log_session_event(
                    "attach",
                    thread_key,
                    existing_session_id=previous_session_id,
                    next_session_id=attach_session_id,
                )
                client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=(
                        f"<@{user_id}> 当前 Slack thread 已绑定到 Codex session `{attach_session_id}`。\n\n"
                        "后续你在这个 thread 里发送的普通消息会继续这个会话。"
                        " 为避免上下文冲突，请不要同时在终端和 Slack 两边并发操作同一个 session。"
                    ),
                )
                return

            if is_reset_command(prompt):
                previous_session_id = SESSION_STORE.get(thread_key)
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

            existing_session_id = None if force_fresh else SESSION_STORE.get(thread_key)
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

            next_session_id, result = run_codex(effective_prompt, session_id=existing_session_id)
            print(
                "[codex_result]"
                f" thread_key={thread_key}"
                f" result_length={len(result or '')}",
                flush=True,
            )
            if existing_session_id and is_invalid_session_result(result):
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
                next_session_id, result = run_codex(effective_prompt)
                print(
                    "[codex_result]"
                    f" thread_key={thread_key}"
                    f" rebuilt=1"
                    f" result_length={len(result or '')}",
                    flush=True,
                )

            if next_session_id and next_session_id != existing_session_id:
                SESSION_STORE.set(thread_key, next_session_id)
            elif next_session_id:
                SESSION_STORE.touch(thread_key)

            log_session_event(
                "completed",
                thread_key,
                existing_session_id=existing_session_id,
                next_session_id=next_session_id,
            )

            post_chunks(client, channel, thread_ts, result)
    except Exception as exc:
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
        text = (event.get("text") or "").replace("\n", "\\n")
        if len(text) > 200:
            text = text[:200] + "...<truncated>"
        print(
            "[slack_event]"
            f" type={event.get('type', '-')}"
            f" channel_type={event.get('channel_type', '-')}"
            f" subtype={event.get('subtype', '-')}"
            f" user={event.get('user', '-')}"
            f" channel={event.get('channel', '-')}"
            f" text={json.dumps(text, ensure_ascii=True)}",
            flush=True,
        )
        next()

    @app.event("app_mention")
    def handle_app_mention(body, client, logger):
        event = body.get("event", {})
        if event.get("bot_id"):
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
    print(
        "[codex_cmd]"
        f" mode={mode}"
        f" cwd={workdir}"
        f" args={json.dumps(args, ensure_ascii=True)}",
        flush=True,
    )


def log_codex_result(mode, exit_code, raw_output, final_output):
    raw_preview = (raw_output or "").strip().replace("\n", "\\n")
    if len(raw_preview) > 500:
        raw_preview = raw_preview[:500] + "...<truncated>"
    final_preview = (final_output or "").strip().replace("\n", "\\n")
    if len(final_preview) > 500:
        final_preview = final_preview[:500] + "...<truncated>"
    print(
        "[codex_exit]"
        f" mode={mode}"
        f" exit_code={exit_code}"
        f" raw_preview={json.dumps(raw_preview, ensure_ascii=True)}"
        f" final_preview={json.dumps(final_preview, ensure_ascii=True)}",
        flush=True,
    )


def main():
    validate_env()
    app = build_app()
    handler = SocketModeHandler(app, ENV["SLACK_APP_TOKEN"])
    handler.start()


if __name__ == "__main__":
    main()
