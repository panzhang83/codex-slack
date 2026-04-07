import asyncio
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import codex_threads

try:
    from codex_app_server_sdk.client import (
        _TurnSession,
        _extract_turn_id,
        _is_transport_error_event,
        _turn_overrides_to_params,
    )
    from codex_app_server_sdk.models import ConversationStep
    from codex_app_server_sdk.transport import CodexTransportError
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "Missing dependency `codex-app-server-sdk`. "
        "Run `pip install -r requirements.txt` before starting codex-slack."
    ) from exc


@dataclass(frozen=True)
class RuntimeActiveTurn:
    session_id: str
    turn_id: str
    started_at: float


@dataclass(frozen=True)
class RuntimeTurnResult:
    session_id: str
    turn_id: str
    final_text: str
    steps: list[ConversationStep] = field(default_factory=list)
    raw_events: list[dict[str, Any]] = field(default_factory=list)
    interrupted: bool = False


class AppServerRuntime:
    def __init__(self, config_factory):
        self._config_factory = config_factory
        self._guard = threading.Lock()
        self._loop_ready = threading.Event()
        self._loop = None
        self._thread = None
        self._client = None
        self._closed = False
        self._active_turns = {}
        self._active_turns_guard = threading.Lock()
        self._client_init_lock = None

    def get_active_turn(self, session_id) -> Optional[RuntimeActiveTurn]:
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            return None
        with self._active_turns_guard:
            return self._active_turns.get(normalized_session_id)

    def run_turn(
        self,
        *,
        session_id=None,
        input_items,
        thread_config=None,
        turn_overrides=None,
        heartbeat_seconds=None,
        on_turn_started: Optional[Callable[[str, str], None]] = None,
        on_step: Optional[Callable[[ConversationStep], None]] = None,
        on_heartbeat: Optional[Callable[[str, str, float], None]] = None,
    ) -> RuntimeTurnResult:
        future = self._submit(
            self._run_turn_async(
                session_id=session_id,
                input_items=list(input_items or []),
                thread_config=thread_config,
                turn_overrides=turn_overrides,
                heartbeat_seconds=heartbeat_seconds,
                on_turn_started=on_turn_started,
                on_step=on_step,
                on_heartbeat=on_heartbeat,
            )
        )
        return future.result()

    def steer_turn(self, session_id, text):
        active_turn = self.get_active_turn(session_id)
        if not active_turn:
            raise RuntimeError("当前没有可 steer 的 runtime 活跃 turn。")
        return self.steer_active_turn(active_turn, text)

    def interrupt_turn(self, session_id):
        active_turn = self.get_active_turn(session_id)
        if not active_turn:
            raise RuntimeError("当前没有可中断的 runtime 活跃 turn。")
        return self.interrupt_active_turn(active_turn)

    def steer_active_turn(self, active_turn: RuntimeActiveTurn, text):
        future = self._submit(self._steer_turn_async(active_turn, text))
        future.result()
        return active_turn

    def interrupt_active_turn(self, active_turn: RuntimeActiveTurn):
        future = self._submit(self._interrupt_turn_async(active_turn))
        future.result()
        return active_turn

    def close(self):
        with self._guard:
            if self._closed:
                return
            self._closed = True
            loop = self._loop
            thread = self._thread

        if loop is None or thread is None:
            return

        future = asyncio.run_coroutine_threadsafe(self._shutdown_async(), loop)
        with suppress(Exception):
            future.result(timeout=10)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10)

    def _submit(self, coro):
        loop = self._ensure_loop()
        return asyncio.run_coroutine_threadsafe(coro, loop)

    def _ensure_loop(self):
        with self._guard:
            if self._closed:
                raise RuntimeError("runtime is already closed")
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop_worker, daemon=True)
                self._thread.start()

        self._loop_ready.wait()
        return self._loop

    def _loop_worker(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        # The app-server client lives on this dedicated loop, so its init lock must too.
        self._client_init_lock = asyncio.Lock()
        self._loop_ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                with suppress(Exception):
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            with suppress(Exception):
                loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    async def _shutdown_async(self):
        await self._reset_client_async()

    async def _ensure_client_async(self):
        if self._client is not None:
            return self._client
        async with self._client_init_lock:
            if self._client is not None:
                return self._client
            client = codex_threads.create_app_server_client(self._config_factory())
            await client.start()
            await client.initialize()
            self._client = client
            return client

    async def _reset_client_async(self):
        client = self._client
        self._client = None
        if client is None:
            return
        with suppress(Exception):
            await client.close()

    async def _resolve_thread_async(self, session_id, thread_config):
        client = await self._ensure_client_async()
        if session_id:
            result = await client.resume_thread(session_id, overrides=thread_config)
        else:
            result = await client.start_thread(config=thread_config)
        return result.thread_id

    async def _run_turn_async(
        self,
        *,
        session_id=None,
        input_items,
        thread_config=None,
        turn_overrides=None,
        heartbeat_seconds=None,
        on_turn_started=None,
        on_step=None,
        on_heartbeat=None,
    ) -> RuntimeTurnResult:
        client = None
        active_thread_id = None
        turn_id = None
        start_monotonic = time.monotonic()
        heartbeat_interval = None
        if heartbeat_seconds:
            heartbeat_interval = max(1.0, float(heartbeat_seconds))

        try:
            client = await self._ensure_client_async()
            active_thread_id = await self._resolve_thread_async(session_id, thread_config)

            turn_params = {
                "threadId": active_thread_id,
                "input": [dict(item) for item in (input_items or [])],
            }
            turn_params.update(_turn_overrides_to_params(turn_overrides))
            turn_result = await client.request("turn/start", turn_params)
            turn_id = _extract_turn_id(turn_result)
            if not turn_id:
                raise RuntimeError("turn/start succeeded but no turn id found")

            active_turn = RuntimeActiveTurn(
                session_id=active_thread_id,
                turn_id=turn_id,
                started_at=time.time(),
            )
            with self._active_turns_guard:
                self._active_turns[active_thread_id] = active_turn

            if on_turn_started:
                on_turn_started(active_thread_id, turn_id)

            session = _TurnSession(thread_id=active_thread_id, turn_id=turn_id)
            interrupted = False

            while True:
                if session.failed:
                    failure_message = session.failure_message or "turn failed"
                    normalized_failure = failure_message.lower()
                    if "interrupt" in normalized_failure or "cancel" in normalized_failure:
                        interrupted = True
                        break
                    raise RuntimeError(failure_message)

                if session.completed:
                    break

                try:
                    event = await client._receive_turn_event(
                        turn_id,
                        inactivity_timeout=heartbeat_interval,
                    )
                except asyncio.TimeoutError:
                    if on_heartbeat:
                        on_heartbeat(
                            active_thread_id,
                            turn_id,
                            time.monotonic() - start_monotonic,
                        )
                    continue

                if _is_transport_error_event(event):
                    message = (
                        ((event.get("params") or {}).get("message"))
                        if isinstance(event, dict)
                        else None
                    )
                    raise CodexTransportError(message or "receiver loop failed")

                step_count_before = len(session.step_records)
                client._apply_event_to_session(session, event)

                for record in session.step_records[step_count_before:]:
                    if on_step:
                        on_step(record.step)

            final_text = ""
            if session.completed_agent_messages:
                final_text = (session.completed_agent_messages[-1][1] or "").strip()
            if not final_text:
                codex_steps = [record.step.text for record in session.step_records if record.step.step_type == "codex"]
                final_text = (codex_steps[-1] or "").strip() if codex_steps else ""
            if interrupted and not final_text:
                final_text = "当前 turn 已被中断。"

            return RuntimeTurnResult(
                session_id=active_thread_id,
                turn_id=turn_id,
                final_text=final_text,
                steps=[record.step for record in session.step_records],
                raw_events=list(session.raw_events),
                interrupted=interrupted,
            )
        except Exception as exc:
            if isinstance(exc, CodexTransportError):
                await self._reset_client_async()
            raise
        finally:
            if active_thread_id and turn_id:
                with self._active_turns_guard:
                    current = self._active_turns.get(active_thread_id)
                    if current and current.turn_id == turn_id:
                        self._active_turns.pop(active_thread_id, None)

    async def _steer_turn_async(self, active_turn: RuntimeActiveTurn, text):
        client = await self._ensure_client_async()
        await client.steer_turn(
            thread_id=active_turn.session_id,
            expected_turn_id=active_turn.turn_id,
            input_items=[{"type": "text", "text": text}],
        )

    async def _interrupt_turn_async(self, active_turn: RuntimeActiveTurn):
        client = await self._ensure_client_async()
        await client.request(
            "turn/interrupt",
            {
                "threadId": active_turn.session_id,
                "turnId": active_turn.turn_id,
            },
        )
