import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import server
import turn_control
from app_runtime import RuntimeActiveTurn
from codex_threads import CodexAppServerConfig


def ns(**kwargs):
    return SimpleNamespace(**kwargs)


def make_config():
    return CodexAppServerConfig(
        codex_bin="codex",
        workdir="/tmp",
        env={},
    )


def make_thread_response(thread_id="thr-1", status_type="active", turns=None):
    return ns(thread=ns(id=thread_id, status=ns(type=status_type), turns=list(turns or [])))


def make_turn(turn_id, status):
    return ns(id=turn_id, status=status)


class DummyClient:
    def __init__(self):
        self.messages = []

    def chat_postMessage(self, **kwargs):
        self.messages.append(kwargs)


class FakeRuntime:
    def __init__(self, active_turn=None, steer_error=None, interrupt_error=None):
        self.active_turn = active_turn
        self.steer_error = steer_error
        self.interrupt_error = interrupt_error
        self.steer_calls = []
        self.interrupt_calls = []

    def get_active_turn(self, session_id):
        if self.active_turn and self.active_turn.session_id == session_id:
            return self.active_turn
        return None

    def steer_active_turn(self, active_turn, text):
        self.steer_calls.append((active_turn.session_id, active_turn.turn_id, text))
        if self.steer_error:
            raise self.steer_error
        if not self.active_turn or self.active_turn.session_id != active_turn.session_id:
            raise RuntimeError("no active runtime turn")
        return self.active_turn

    def interrupt_active_turn(self, active_turn):
        self.interrupt_calls.append((active_turn.session_id, active_turn.turn_id))
        if self.interrupt_error:
            raise self.interrupt_error
        if not self.active_turn or self.active_turn.session_id != active_turn.session_id:
            raise RuntimeError("no active runtime turn")
        return self.active_turn


class TurnControlHelperTests(unittest.TestCase):
    def test_find_active_turn_prefers_latest_in_progress_turn(self):
        response = make_thread_response(
            turns=[
                make_turn("turn-old", "completed"),
                make_turn("turn-running-1", ns(value="inProgress")),
                make_turn("turn-running-2", "inProgress"),
            ]
        )

        active = turn_control.find_active_turn(response)

        self.assertIsNotNone(active)
        self.assertEqual(active.thread_id, "thr-1")
        self.assertEqual(active.turn_id, "turn-running-2")

    def test_find_active_turn_requires_active_thread_status(self):
        response = make_thread_response(
            status_type="idle",
            turns=[make_turn("turn-running", "inProgress")],
        )

        self.assertIsNone(turn_control.find_active_turn(response))

    def test_interrupt_active_turn_raises_when_no_active_turn(self):
        with patch.object(turn_control, "read_thread_response", return_value=make_thread_response(status_type="idle")):
            with self.assertRaises(RuntimeError):
                turn_control.interrupt_active_turn(make_config(), "session-1")

    def test_steer_active_turn_calls_sdk_with_expected_payload(self):
        response = make_thread_response(
            thread_id="thr-steer",
            turns=[make_turn("turn-steer", "inProgress")],
        )

        with patch.object(turn_control, "read_thread_response", return_value=response):
            with patch.object(turn_control, "steer_turn") as steer_turn:
                active = turn_control.steer_active_turn(make_config(), "session-2", "focus on tests")

        self.assertEqual(active.turn_id, "turn-steer")
        steer_turn.assert_called_once_with(
            make_config(),
            thread_id="thr-steer",
            expected_turn_id="turn-steer",
            input_items=[{"type": "text", "text": "focus on tests"}],
        )


class TurnCommandParsingTests(unittest.TestCase):
    def test_interrupt_and_steer_command_variants(self):
        self.assertTrue(server.is_interrupt_command("/interrupt"))
        self.assertTrue(server.is_interrupt_command("stop turn"))
        self.assertFalse(server.is_interrupt_command("interrupt now please"))
        self.assertTrue(server.is_steer_command("steer focus tests"))
        self.assertEqual(server.strip_steer_command("/steer keep it short"), "keep it short")

    def test_steer_command_with_chinese_payload_is_detected(self):
        text = "steer 源码类还应该包括 jl (julia 文件），是否可能包含 ipynb 文件？"
        self.assertTrue(server.is_steer_command(text))
        self.assertEqual(
            server.strip_steer_command(text),
            "源码类还应该包括 jl (julia 文件），是否可能包含 ipynb 文件？",
        )


class ProcessPromptTurnCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.store = server.SlackThreadSessionStore(Path(self.tmpdir.name) / "sessions.json")
        self.session_store_patcher = patch.object(server, "SESSION_STORE", self.store)
        self.session_store_patcher.start()
        self.addCleanup(self.session_store_patcher.stop)
        self.active_turn_registry_patcher = patch.object(server, "ACTIVE_TURN_REGISTRY", turn_control.ActiveTurnRegistry())
        self.active_turn_registry_patcher.start()
        self.addCleanup(self.active_turn_registry_patcher.stop)

        server.WATCHERS.clear()
        self.addCleanup(server.WATCHERS.clear)

        self.client = DummyClient()
        self.channel = "C1"
        self.thread_ts = "1"
        self.user_id = "U111"
        self.thread_key = server.make_thread_key(self.channel, self.thread_ts)
        self.session_id = "019d5868-71ba-7101-9143-81867f3db5bf"

    def test_interrupt_requires_session(self):
        server.process_prompt(self.client, self.channel, self.thread_ts, "interrupt", self.user_id)
        self.assertIn("还没有 Codex session", self.client.messages[0]["text"])

    def test_interrupt_requires_runtime_owned_active_turn(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_mode(self.thread_key, server.SESSION_MODE_OBSERVE)
        runtime = FakeRuntime(active_turn=None)

        with patch.object(server, "get_app_runtime", return_value=runtime):
            server.process_prompt(self.client, self.channel, self.thread_ts, "interrupt", self.user_id)

        self.assertEqual(runtime.interrupt_calls, [])
        self.assertIn("当前没有由 codex-slack runtime 持有的活跃 turn", self.client.messages[0]["text"])

    def test_interrupt_in_observe_mode_can_stop_runtime_owned_turn(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_mode(self.thread_key, server.SESSION_MODE_OBSERVE)
        active_turn = RuntimeActiveTurn(
            session_id=self.session_id,
            turn_id="turn-123",
            started_at=0,
        )
        runtime = FakeRuntime(active_turn=active_turn)

        with patch.object(server, "get_app_runtime", return_value=runtime):
            server.process_prompt(self.client, self.channel, self.thread_ts, "interrupt", self.user_id)

        self.assertEqual(runtime.interrupt_calls, [(self.session_id, "turn-123")])
        self.assertIn("已发送中断请求", self.client.messages[0]["text"])

    def test_steer_requires_control_mode(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_mode(self.thread_key, server.SESSION_MODE_OBSERVE)

        with patch.object(server, "get_app_runtime", return_value=FakeRuntime()) as get_app_runtime:
            server.process_prompt(self.client, self.channel, self.thread_ts, "steer focus tests", self.user_id)

        get_app_runtime.assert_not_called()
        self.assertIn("`steer` 只在 `control` 模式下可用", self.client.messages[0]["text"])

    def test_steer_requires_payload(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_mode(self.thread_key, server.SESSION_MODE_CONTROL)

        server.process_prompt(self.client, self.channel, self.thread_ts, "steer", self.user_id)

        self.assertIn("用法：`steer <", self.client.messages[0]["text"])

    def test_steer_in_control_mode_calls_turn_control(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_mode(self.thread_key, server.SESSION_MODE_CONTROL)
        active_turn = RuntimeActiveTurn(
            session_id=self.session_id,
            turn_id="turn-456",
            started_at=0,
        )
        runtime = FakeRuntime(active_turn=active_turn)

        with patch.object(server, "get_app_runtime", return_value=runtime):
            server.process_prompt(self.client, self.channel, self.thread_ts, "steer focus on failing tests first", self.user_id)

        self.assertEqual(runtime.steer_calls, [(self.session_id, "turn-456", "focus on failing tests first")])
        text = self.client.messages[0]["text"]
        self.assertIn("已向 session", text)
        self.assertIn("turn-456", text)
        cached = server.ACTIVE_TURN_REGISTRY.get_for_thread(self.thread_key)
        self.assertIsNotNone(cached)
        self.assertEqual(cached.turn_id, "turn-456")

    def test_steer_can_use_registry_session_id_before_store_is_bound(self):
        active_turn = RuntimeActiveTurn(
            session_id=self.session_id,
            turn_id="turn-789",
            started_at=0,
        )
        runtime = FakeRuntime(active_turn=active_turn)
        server.ACTIVE_TURN_REGISTRY.set(self.thread_key, self.session_id, "turn-789")

        with patch.object(server, "get_app_runtime", return_value=runtime):
            server.process_prompt(self.client, self.channel, self.thread_ts, "steer keep going", self.user_id)

        self.assertEqual(runtime.steer_calls, [(self.session_id, "turn-789", "keep going")])
        self.assertIn("turn-789", self.client.messages[0]["text"])

    def test_interrupt_runtime_failure_is_reported_instead_of_reclassified(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_mode(self.thread_key, server.SESSION_MODE_CONTROL)
        active_turn = RuntimeActiveTurn(
            session_id=self.session_id,
            turn_id="turn-999",
            started_at=0,
        )
        runtime = FakeRuntime(active_turn=active_turn, interrupt_error=RuntimeError("transport exploded"))

        with patch.object(server, "get_app_runtime", return_value=runtime):
            server.process_prompt(self.client, self.channel, self.thread_ts, "interrupt", self.user_id)

        self.assertEqual(runtime.interrupt_calls, [(self.session_id, "turn-999")])
        self.assertIn("中断当前 turn 失败", self.client.messages[0]["text"])
        self.assertIn("transport exploded", self.client.messages[0]["text"])

    def test_steer_runtime_failure_is_reported_instead_of_reclassified(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_mode(self.thread_key, server.SESSION_MODE_CONTROL)
        active_turn = RuntimeActiveTurn(
            session_id=self.session_id,
            turn_id="turn-321",
            started_at=0,
        )
        runtime = FakeRuntime(active_turn=active_turn, steer_error=RuntimeError("steer rejected"))

        with patch.object(server, "get_app_runtime", return_value=runtime):
            server.process_prompt(self.client, self.channel, self.thread_ts, "steer focus", self.user_id)

        self.assertEqual(runtime.steer_calls, [(self.session_id, "turn-321", "focus")])
        self.assertIn("steer 失败", self.client.messages[0]["text"])
        self.assertIn("steer rejected", self.client.messages[0]["text"])


if __name__ == "__main__":
    unittest.main()
