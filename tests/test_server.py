import asyncio
import concurrent.futures
import os
import ssl
import tempfile
import threading
import time
import urllib.error
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import server
from app_runtime import RuntimeUserInputQuestionOption
from codex_app_server_sdk.errors import CodexTimeoutError


def ns(**kwargs):
    return SimpleNamespace(**kwargs)


def text_item(text):
    return ns(root=ns(type="text", text=text))


def local_image_item(path):
    return ns(root=ns(type="localImage", path=path))


def mention_item(name):
    return ns(root=ns(type="mention", name=name))


def skill_item(name):
    return ns(root=ns(type="skill", name=name))


def user_message(turn_id, item_id, *content):
    return ns(id=turn_id, items=[ns(root=ns(type="userMessage", id=item_id, content=list(content)))])


def agent_message(item_id, phase, text):
    return ns(root=ns(type="agentMessage", id=item_id, phase=phase, text=text))


def make_turn(turn_id, *items):
    return ns(id=turn_id, items=list(items))


def make_thread_response(*turns):
    return ns(thread=ns(turns=list(turns)))


class DummyClient:
    def __init__(self):
        self.messages = []
        self.updated_messages = []
        self.opened_views = []

    def chat_postMessage(self, **kwargs):
        self.messages.append(kwargs)
        return {"ok": True, "ts": str(len(self.messages))}

    def chat_update(self, **kwargs):
        self.updated_messages.append(kwargs)
        return {"ok": True}

    def views_open(self, **kwargs):
        self.opened_views.append(kwargs)
        return {"ok": True}


class CommandParsingTests(unittest.TestCase):
    def test_command_variants(self):
        self.assertTrue(server.is_fresh_command("/fresh summarize status"))
        self.assertEqual(server.strip_fresh_command("fresh do the task"), "do the task")
        self.assertTrue(server.is_recent_command("recent"))
        self.assertTrue(server.is_sessions_command("/sessions --all"))
        self.assertEqual(server.strip_sessions_command("sessions --cwd /tmp/project"), "--cwd /tmp/project")
        self.assertTrue(server.is_attach_command("attach 019-test"))
        self.assertEqual(server.strip_attach_command("/attach 019-test"), "019-test")
        self.assertEqual(server.parse_attach_recent_selector("recent 2"), 2)
        self.assertTrue(server.is_effort_command("effort high"))
        self.assertEqual(server.strip_effort_command("/effort reset"), "reset")
        self.assertTrue(server.is_name_command("name flaky tests"))
        self.assertEqual(server.strip_name_command("/name keep it short"), "keep it short")
        self.assertTrue(server.is_progress_command("progress off"))
        self.assertEqual(server.strip_progress_command("/progress reset"), "reset")
        self.assertTrue(server.is_status_command("whoami"))
        self.assertTrue(server.is_mode_command("mode"))
        self.assertTrue(server.is_subagents_command("subagents"))
        self.assertTrue(server.is_session_command("session id"))
        self.assertTrue(server.is_watch_command("/watch"))
        self.assertFalse(server.is_watch_command("watch raw"))
        self.assertTrue(server.is_unsupported_watch_command("watch raw"))
        self.assertTrue(server.is_unwatch_command("stop watch"))
        self.assertTrue(server.is_control_command("takeover"))
        self.assertTrue(server.is_observe_command("release"))

    def test_parse_fresh_payload_supports_reasoning_effort(self):
        effort, prompt, error = server.parse_fresh_payload("--effort high fix flaky test")
        self.assertEqual(effort, "high")
        self.assertEqual(prompt, "fix flaky test")
        self.assertIsNone(error)

    def test_parse_fresh_payload_rejects_invalid_reasoning_effort(self):
        effort, prompt, error = server.parse_fresh_payload("--effort auto fix flaky test")
        self.assertIsNone(effort)
        self.assertEqual(prompt, "--effort auto fix flaky test")
        self.assertIn("low|medium|high|xhigh", error)

    def test_parse_sessions_payload_supports_all_and_cwd(self):
        self.assertEqual(server.parse_sessions_payload(""), (False, None))
        self.assertEqual(server.parse_sessions_payload("--all"), (True, None))
        self.assertEqual(server.parse_sessions_payload("--cwd /tmp/project"), (False, "/tmp/project"))


class SessionModeResolutionTests(unittest.TestCase):
    def test_get_effective_session_mode_prefers_explicit_mode(self):
        active_record = ns(session_id="sess-active")
        self.assertEqual(
            server.get_effective_session_mode(
                "thread-1",
                session_id="sess-store",
                session_mode=server.SESSION_MODE_OBSERVE,
                active_record=active_record,
            ),
            server.SESSION_MODE_OBSERVE,
        )

    def test_get_effective_session_mode_uses_matching_runtime_active_turn(self):
        active_record = ns(session_id="sess-active")
        self.assertEqual(
            server.get_effective_session_mode(
                "thread-1",
                session_id="sess-active",
                session_mode=None,
                active_record=active_record,
            ),
            server.SESSION_MODE_CONTROL,
        )

    def test_get_effective_session_mode_returns_none_without_match(self):
        active_record = ns(session_id="sess-other")
        self.assertIsNone(
            server.get_effective_session_mode(
                "thread-1",
                session_id="sess-store",
                session_mode=None,
                active_record=active_record,
            )
        )
        self.assertIsNone(server.get_effective_session_mode("thread-1", session_id=None, session_mode=None))


class AppServerConfigTests(unittest.TestCase):
    def test_resume_timeout_defaults_to_request_timeout(self):
        with patch.dict(server.ENV, {"CODEX_SLACK_APP_SERVER_REQUEST_TIMEOUT_SECONDS": "123"}, clear=False):
            self.assertEqual(server.get_app_server_resume_timeout_seconds(), 123.0)

    def test_resume_timeout_and_retry_budget_are_configurable(self):
        with patch.dict(
            server.ENV,
            {
                "CODEX_SLACK_APP_SERVER_REQUEST_TIMEOUT_SECONDS": "90",
                "CODEX_SLACK_APP_SERVER_RESUME_TIMEOUT_SECONDS": "150",
                "CODEX_SLACK_APP_SERVER_RESUME_MAX_RETRIES": "4",
            },
            clear=False,
        ):
            self.assertEqual(server.get_app_server_resume_timeout_seconds(), 150.0)
            self.assertEqual(server.get_app_server_resume_max_retries(), 4)

    def test_build_process_error_message_includes_runtime_diagnostics(self):
        text = server.build_process_error_message(
            "U123",
            CodexTimeoutError("request timed out for method='thread/resume' after 90.0s"),
            diagnostics="stderr line 1\nstderr line 2",
        )
        self.assertIn("CodexTimeoutError", text)
        self.assertIn("thread/resume", text)
        self.assertIn("stderr line 1", text)
        self.assertIn("stderr line 2", text)

    def test_retryable_slack_startup_error_accepts_ssl_eof(self):
        err = urllib.error.URLError(ssl.SSLEOFError("EOF occurred in violation of protocol"))
        self.assertTrue(server.is_retryable_slack_startup_error(err))

    def test_retryable_slack_startup_error_rejects_programming_error(self):
        self.assertFalse(server.is_retryable_slack_startup_error(ValueError("bad config")))

    def test_run_socket_mode_forever_retries_retryable_startup_error(self):
        events = []

        def app_factory():
            events.append("app")
            if events.count("app") == 1:
                raise urllib.error.URLError(
                    ssl.SSLEOFError("EOF occurred in violation of protocol")
                )
            return "app-ok"

        class FakeHandler:
            def __init__(self, app, token):
                events.append(("handler", app, token))

            def start(self):
                events.append("start")

        sleeps = []
        with patch.dict(
            server.ENV,
            {
                "SLACK_APP_TOKEN": "xapp-test",
                "CODEX_SLACK_STARTUP_RETRY_INITIAL_SECONDS": "1",
                "CODEX_SLACK_STARTUP_RETRY_MAX_SECONDS": "4",
            },
            clear=False,
        ):
            server.run_socket_mode_forever(
                app_factory=app_factory,
                handler_factory=FakeHandler,
                sleep_fn=sleeps.append,
            )

        self.assertEqual(sleeps, [1.0])
        self.assertIn(("handler", "app-ok", "xapp-test"), events)
        self.assertIn("start", events)


class SlackAccessTests(unittest.TestCase):
    def test_allowed_user_ids_support_commas_and_whitespace(self):
        with patch.dict(
            server.ENV,
            {"ALLOWED_SLACK_USER_IDS": "U111, U222\nU333\tU444"},
            clear=False,
        ):
            self.assertEqual(server.get_allowed_slack_user_ids(), {"U111", "U222", "U333", "U444"})

    def test_blank_allowlist_means_unrestricted(self):
        with patch.dict(server.ENV, {"ALLOWED_SLACK_USER_IDS": ""}, clear=False):
            self.assertTrue(server.is_allowed_slack_user("U111"))

    def test_attach_accepts_uuid_in_single_user_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = server.SlackThreadSessionStore(Path(tmpdir) / "sessions.json")
            with patch.dict(
                server.ENV,
                {"ALLOWED_SLACK_USER_IDS": "U111", "ALLOW_SHARED_ATTACH": "0"},
                clear=False,
            ):
                self.assertIsNone(
                    server.get_attach_error(
                        "U111",
                        "019d5868-71ba-7101-9143-81867f3db5bf",
                        session_store=store,
                    )
                )

    def test_attach_rejects_non_uuid_session_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = server.SlackThreadSessionStore(Path(tmpdir) / "sessions.json")
            with patch.dict(server.ENV, {"ALLOWED_SLACK_USER_IDS": "U111"}, clear=False):
                error = server.get_attach_error("U111", "thread-name", session_store=store)
        self.assertIn("只接受 Codex session UUID", error)

    def test_attach_rejects_unseen_session_in_multi_user_mode_by_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = server.SlackThreadSessionStore(Path(tmpdir) / "sessions.json")
            with patch.dict(
                server.ENV,
                {"ALLOWED_SLACK_USER_IDS": "U111,U222", "ALLOW_SHARED_ATTACH": "0"},
                clear=False,
            ):
                error = server.get_attach_error(
                    "U111",
                    "019d5868-71ba-7101-9143-81867f3db5bf",
                    session_store=store,
                )
        self.assertIn("ALLOW_SHARED_ATTACH=1", error)

    def test_attach_rejects_session_owned_by_another_user(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = server.SlackThreadSessionStore(Path(tmpdir) / "sessions.json")
            session_id = "019d5868-71ba-7101-9143-81867f3db5bf"
            store.set("C1:1", session_id, owner_user_id="U111")
            with patch.dict(
                server.ENV,
                {"ALLOWED_SLACK_USER_IDS": "U111,U222", "ALLOW_SHARED_ATTACH": "1"},
                clear=False,
            ):
                error = server.get_attach_error("U222", session_id, session_store=store)
        self.assertIn("不允许跨用户接管", error)


class SessionStoreTests(unittest.TestCase):
    def test_get_mode_returns_none_when_thread_has_no_session_entry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = server.SlackThreadSessionStore(Path(tmpdir) / "sessions.json")

        self.assertIsNone(store.get_mode("C1:1"))

    def test_get_mode_defaults_legacy_session_entries_to_control(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sessions.json"
            path.write_text(
                '{\n  "C1:1": {\n    "session_id": "019d5868-71ba-7101-9143-81867f3db5bf",\n    "updated_at": 1\n  }\n}\n',
                encoding="utf-8",
            )
            store = server.SlackThreadSessionStore(path)

        self.assertEqual(store.get_mode("C1:1"), server.SESSION_MODE_CONTROL)

    def test_set_and_reload_preserves_reasoning_effort_session_origin_and_cwd(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sessions.json"
            store = server.SlackThreadSessionStore(path)
            store.set_reasoning_effort("C1:1", "high", owner_user_id="U111")
            store.set(
                "C1:1",
                "019d5868-71ba-7101-9143-81867f3db5bf",
                owner_user_id="U111",
                session_origin=server.SESSION_ORIGIN_ATTACHED,
                session_cwd="/tmp/project-a",
            )

            reloaded = server.SlackThreadSessionStore(path)

        self.assertEqual(reloaded.get_reasoning_effort("C1:1"), "high")
        self.assertEqual(reloaded.get_session_origin("C1:1"), server.SESSION_ORIGIN_ATTACHED)
        self.assertEqual(reloaded.get_session_cwd("C1:1"), "/tmp/project-a")

    def test_set_and_reload_preserves_collaboration_mode_without_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sessions.json"
            store = server.SlackThreadSessionStore(path)
            store.set_collaboration_mode("C1:1", server.COLLABORATION_MODE_PLAN, owner_user_id="U111")

            reloaded = server.SlackThreadSessionStore(path)

        self.assertEqual(reloaded.get_collaboration_mode("C1:1"), server.COLLABORATION_MODE_PLAN)
        self.assertEqual(reloaded.get_owner("C1:1"), "U111")

    def test_set_and_reload_preserves_watch_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sessions.json"
            store = server.SlackThreadSessionStore(path)
            store.set("C1:1", "019d5868-71ba-7101-9143-81867f3db5bf", owner_user_id="U111")
            store.set_watch_enabled("C1:1", True, owner_user_id="U111")

            reloaded = server.SlackThreadSessionStore(path)

        self.assertTrue(reloaded.get_watch_enabled("C1:1"))

    def test_set_and_reload_preserves_watch_last_event_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sessions.json"
            store = server.SlackThreadSessionStore(path)
            store.set("C1:1", "019d5868-71ba-7101-9143-81867f3db5bf", owner_user_id="U111")
            store.set_watch_last_event_key(
                "C1:1",
                "019d5868-71ba-7101-9143-81867f3db5bf",
                ("turn-2", "a2"),
                owner_user_id="U111",
            )

            reloaded = server.SlackThreadSessionStore(path)

        self.assertEqual(
            reloaded.get_watch_last_event_key(
                "C1:1",
                current_session_id="019d5868-71ba-7101-9143-81867f3db5bf",
            ),
            ("turn-2", "a2"),
        )

    def test_get_watch_last_event_key_ignores_mismatched_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sessions.json"
            store = server.SlackThreadSessionStore(path)
            store.set("C1:1", "sess-1", owner_user_id="U111")
            store.set_watch_last_event_key("C1:1", "sess-1", ("turn-2", "a2"), owner_user_id="U111")

            self.assertIsNone(store.get_watch_last_event_key("C1:1", current_session_id="sess-other"))

    def test_attach_session_defaults_to_observe_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = server.SlackThreadSessionStore(Path(tmpdir) / "sessions.json")
            previous_session_id, error = store.attach_session(
                "C1:1",
                "019d5868-71ba-7101-9143-81867f3db5bf",
                owner_user_id="U111",
                allow_unseen=True,
            )

            self.assertIsNone(previous_session_id)
            self.assertIsNone(error)
            self.assertEqual(store.get_mode("C1:1"), server.SESSION_MODE_OBSERVE)

    def test_attach_session_preserves_existing_reasoning_effort(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = server.SlackThreadSessionStore(Path(tmpdir) / "sessions.json")
            store.set_reasoning_effort("C1:1", "medium", owner_user_id="U111")

            previous_session_id, error = store.attach_session(
                "C1:1",
                "019d5868-71ba-7101-9143-81867f3db5bf",
                owner_user_id="U111",
                allow_unseen=True,
            )

        self.assertIsNone(previous_session_id)
        self.assertIsNone(error)
        self.assertEqual(store.get_reasoning_effort("C1:1"), "medium")
        self.assertEqual(store.get_session_origin("C1:1"), server.SESSION_ORIGIN_ATTACHED)

    def test_attach_session_stores_session_cwd(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = server.SlackThreadSessionStore(Path(tmpdir) / "sessions.json")

            previous_session_id, error = store.attach_session(
                "C1:1",
                "019d5868-71ba-7101-9143-81867f3db5bf",
                owner_user_id="U111",
                allow_unseen=True,
                session_cwd="/tmp/project-b",
            )

        self.assertIsNone(previous_session_id)
        self.assertIsNone(error)
        self.assertEqual(store.get_session_cwd("C1:1"), "/tmp/project-b")

    def test_attach_session_rejects_cross_user_takeover(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = server.SlackThreadSessionStore(Path(tmpdir) / "sessions.json")
            session_id = "019d5868-71ba-7101-9143-81867f3db5bf"
            store.set("C1:1", session_id, owner_user_id="U111")

            previous_session_id, error = store.attach_session(
                "C2:2",
                session_id,
                owner_user_id="U222",
                allow_unseen=True,
            )

            self.assertIsNone(previous_session_id)
            self.assertIn("不允许跨用户接管", error)
            self.assertIsNone(store.get("C2:2"))

    def test_pending_subagent_target_persists_and_reload_validates_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sessions.json"
            store = server.SlackThreadSessionStore(path)
            session_id = "019d5868-71ba-7101-9143-81867f3db5bf"
            store.set("C1:1", session_id, owner_user_id="U111")
            store.set_pending_subagent_target(
                "C1:1",
                thread_id="sub-1",
                agent_nickname="Atlas",
                agent_role="explorer",
                owner_user_id="U111",
                session_id=session_id,
                armed_at=int(time.time()),
            )

            reloaded = server.SlackThreadSessionStore(path)

        pending = reloaded.get_pending_subagent_target(
            "C1:1",
            current_session_id=session_id,
            owner_user_id="U111",
        )
        self.assertEqual(pending["thread_id"], "sub-1")
        self.assertEqual(pending["agent_nickname"], "Atlas")
        self.assertIsNone(
            reloaded.get_pending_subagent_target(
                "C1:1",
                current_session_id="different-session",
                owner_user_id="U111",
            )
        )

    def test_thread_owner_access_error_rejects_non_owner(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = server.SlackThreadSessionStore(Path(tmpdir) / "sessions.json")
            store.set("C1:1", "019d5868-71ba-7101-9143-81867f3db5bf", owner_user_id="U111")
            error = server.get_thread_owner_access_error("C1:1", "U222", session_store=store)
        self.assertIn("不允许跨用户继续使用", error)


class ConversationExtractionTests(unittest.TestCase):
    def test_format_user_message_content_uses_placeholders_for_non_text_inputs(self):
        content = server.format_user_message_content(
            [
                text_item("hello"),
                local_image_item("/tmp/example.png"),
                mention_item("repo"),
                skill_item("review"),
            ]
        )
        self.assertEqual(
            content,
            "hello\n[local image: /tmp/example.png]\n[mention: repo]\n[skill: review]",
        )

    def test_extract_conversation_events_keeps_user_and_final_answer_only(self):
        response = make_thread_response(
            make_turn(
                "turn-1",
                ns(root=ns(type="userMessage", id="u1", content=[text_item("hello")])),
                agent_message("a1", "commentary", "thinking"),
                agent_message("a2", "final_answer", "done"),
            )
        )

        events = server.extract_conversation_events(response)

        self.assertEqual(
            events,
            [
                server.ConversationEvent(turn_id="turn-1", item_id="u1", role="user", text="hello"),
                server.ConversationEvent(turn_id="turn-1", item_id="a2", role="assistant", text="done"),
            ],
        )

    def test_extract_conversation_events_supports_dict_payloads_from_sdk(self):
        response = {
            "thread": {
                "turns": [
                    {
                        "id": "turn-1",
                        "items": [
                            {
                                "type": "userMessage",
                                "id": "item-1",
                                "content": [{"type": "text", "text": "hello"}],
                            },
                            {
                                "type": "agentMessage",
                                "id": "item-2",
                                "phase": "commentary",
                                "text": "thinking",
                            },
                            {
                                "type": "agentMessage",
                                "id": "item-3",
                                "phase": "final_answer",
                                "text": "done",
                            },
                        ],
                    }
                ]
            }
        }

        events = server.extract_conversation_events(response)

        self.assertEqual(
            events,
            [
                server.ConversationEvent(turn_id="turn-1", item_id="item-1", role="user", text="hello"),
                server.ConversationEvent(turn_id="turn-1", item_id="item-3", role="assistant", text="done"),
            ],
        )

    def test_get_events_after_key_raises_when_key_missing(self):
        events = [
            server.ConversationEvent("turn-1", "u1", "user", "one"),
            server.ConversationEvent("turn-1", "a1", "assistant", "done one"),
            server.ConversationEvent("turn-2", "u2", "user", "two"),
            server.ConversationEvent("turn-2", "a2", "assistant", "done two"),
        ]

        with self.assertRaises(server.WatchAnchorLostError):
            server.get_events_after_key(events, ("missing", "key"))

    def test_build_watch_bootstrap_prefers_latest_completed_turn(self):
        response = make_thread_response(
            make_turn(
                "turn-1",
                ns(root=ns(type="userMessage", id="u1", content=[text_item("old question")])),
                agent_message("a1", "final_answer", "old answer"),
            ),
            make_turn(
                "turn-2",
                ns(root=ns(type="userMessage", id="u2", content=[text_item("new question")])),
                agent_message("a2", "commentary", "thinking"),
                agent_message("a3", "final_answer", "new answer"),
            ),
            make_turn(
                "turn-3",
                ns(root=ns(type="userMessage", id="u3", content=[text_item("unfinished question")])),
                agent_message("a4", "commentary", "still working"),
            ),
        )

        with patch.object(server, "read_thread_response", return_value=response):
            text, last_key = server.build_watch_bootstrap("019d5868-71ba-7101-9143-81867f3db5bf")

        self.assertIn("最近一轮对话:", text)
        self.assertIn("*User*\n> new question", text)
        self.assertIn("*Codex*\n> new answer", text)
        self.assertNotIn("old question", text)
        self.assertNotIn("unfinished question", text)
        self.assertEqual(last_key, ("turn-2", "a3"))

    def test_read_conversation_events_propagates_thread_read_error(self):
        with patch.object(server, "read_thread_response", side_effect=RuntimeError("sdk failed")):
            with self.assertRaisesRegex(RuntimeError, "sdk failed"):
                server.read_conversation_events("019d5868-71ba-7101-9143-81867f3db5bf")

    def test_advance_watch_cursor_emits_incremental_dialogue_without_heading(self):
        events = [
            server.ConversationEvent("turn-0", "u0", "user", "previous"),
            server.ConversationEvent("turn-0", "a0", "assistant", "done previous"),
            server.ConversationEvent("turn-1", "u1", "user", "hello"),
            server.ConversationEvent("turn-1", "a1", "assistant", "done"),
        ]

        message, last_key, rebased = server.advance_watch_cursor(events, ("turn-0", "a0"))

        self.assertFalse(rebased)
        self.assertEqual(last_key, ("turn-1", "a1"))
        self.assertEqual(message, "*User*\n> hello\n\n*Codex*\n> done")

    def test_advance_watch_cursor_rebases_silently_when_anchor_is_missing(self):
        events = [
            server.ConversationEvent("turn-2", "u2", "user", "next"),
            server.ConversationEvent("turn-2", "a2", "assistant", "done"),
        ]

        message, last_key, rebased = server.advance_watch_cursor(events, ("missing", "key"))

        self.assertTrue(rebased)
        self.assertEqual(last_key, ("turn-2", "a2"))
        self.assertIsNone(message)


class CodexHelperTests(unittest.TestCase):
    def test_read_thread_cwd_uses_metadata_only_thread_read(self):
        with patch.object(server, "read_thread_response", return_value={"thread": {"cwd": "/tmp/project"}}) as mock_read:
            cwd = server.read_thread_cwd("019d5868-71ba-7101-9143-81867f3db5bf")

        self.assertEqual(cwd, "/tmp/project")
        mock_read.assert_called_once_with("019d5868-71ba-7101-9143-81867f3db5bf", include_turns=False)

    def test_get_thread_display_title_uses_metadata_only_thread_read(self):
        with patch.object(
            server,
            "read_thread_response",
            return_value={"thread": {"name": "triage flaky test", "preview": "ignored"}},
        ) as mock_read:
            title = server.get_thread_display_title("019d5868-71ba-7101-9143-81867f3db5bf")

        self.assertEqual(title, "triage flaky test")
        mock_read.assert_called_once_with("019d5868-71ba-7101-9143-81867f3db5bf", include_turns=False)

    def test_resolve_reasoning_effort_prefers_thread_override(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = server.SlackThreadSessionStore(Path(tmpdir) / "sessions.json")
            store.set_reasoning_effort("C1:1", "high")
            store.set(
                "C1:1",
                "019d5868-71ba-7101-9143-81867f3db5bf",
                session_origin=server.SESSION_ORIGIN_ATTACHED,
            )

            effort, source = server.resolve_reasoning_effort(
                "C1:1",
                session_id="019d5868-71ba-7101-9143-81867f3db5bf",
                session_origin=server.SESSION_ORIGIN_ATTACHED,
                session_store=store,
            )

        self.assertEqual((effort, source), ("high", "thread"))

    def test_resolve_reasoning_effort_inherits_for_attached_session_without_override(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = server.SlackThreadSessionStore(Path(tmpdir) / "sessions.json")
            store.set(
                "C1:1",
                "019d5868-71ba-7101-9143-81867f3db5bf",
                session_origin=server.SESSION_ORIGIN_ATTACHED,
            )
            with patch.dict(server.ENV, {"CODEX_REASONING_EFFORT": "high"}, clear=False):
                effort, source = server.resolve_reasoning_effort(
                    "C1:1",
                    session_id="019d5868-71ba-7101-9143-81867f3db5bf",
                    session_origin=server.SESSION_ORIGIN_ATTACHED,
                    session_store=store,
                )

        self.assertEqual((effort, source), (None, "inherited"))

    def test_resolve_reasoning_effort_uses_hard_default_xhigh_for_slack_sessions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = server.SlackThreadSessionStore(Path(tmpdir) / "sessions.json")
            with patch.dict(server.ENV, {"CODEX_REASONING_EFFORT": ""}, clear=False):
                effort, source = server.resolve_reasoning_effort(
                    "C1:1",
                    session_id=None,
                    session_origin=server.SESSION_ORIGIN_SLACK,
                    session_store=store,
                )

        self.assertEqual((effort, source), ("xhigh", "default"))

    def test_build_codex_exec_args_adds_reasoning_effort_config(self):
        with patch.dict(
            server.ENV,
            {
                "CODEX_BIN": "codex",
                "OPENAI_MODEL": "gpt-5.4",
                "CODEX_WORKDIR": "/tmp/work",
                "CODEX_TIMEOUT_SECONDS": "321",
            },
            clear=False,
        ):
            _codex_bin, args, _timeout, _workdir = server.build_codex_exec_args(
                "new task",
                "/tmp/last.txt",
                reasoning_effort="high",
            )

        self.assertIn("--config", args)
        self.assertIn('model_reasoning_effort="high"', args)

    def test_build_codex_resume_args_uses_workdir_override(self):
        with patch.dict(
            server.ENV,
            {
                "CODEX_BIN": "codex",
                "OPENAI_MODEL": "gpt-5.4",
                "CODEX_WORKDIR": "/tmp/default-work",
                "CODEX_TIMEOUT_SECONDS": "321",
            },
            clear=False,
        ):
            _codex_bin, _args, _timeout, workdir = server.build_codex_resume_args(
                "019d5868-71ba-7101-9143-81867f3db5bf",
                "continue",
                "/tmp/last.txt",
                workdir_override="/tmp/attached-project",
            )

        self.assertEqual(workdir, "/tmp/attached-project")

    def test_build_codex_resume_args_uses_session_and_prompt(self):
        with patch.dict(
            server.ENV,
            {
                "CODEX_BIN": "codex",
                "OPENAI_MODEL": "gpt-5.4",
                "CODEX_WORKDIR": "/tmp/work",
                "CODEX_TIMEOUT_SECONDS": "321",
                "CODEX_FULL_AUTO": "1",
            },
            clear=False,
        ):
            codex_bin, args, timeout, workdir = server.build_codex_resume_args(
                "019d5868-71ba-7101-9143-81867f3db5bf",
                "continue",
                "/tmp/last.txt",
            )

        self.assertEqual(codex_bin, "codex")
        self.assertEqual(timeout, 321)
        self.assertEqual(workdir, "/tmp/work")
        self.assertEqual(args[:4], ["exec", "resume", "--model", "gpt-5.4"])
        self.assertIn("--full-auto", args)
        self.assertEqual(args[-2:], ["019d5868-71ba-7101-9143-81867f3db5bf", "continue"])

    def test_build_codex_resume_args_adds_reasoning_effort_config(self):
        with patch.dict(
            server.ENV,
            {
                "CODEX_BIN": "codex",
                "OPENAI_MODEL": "gpt-5.4",
                "CODEX_WORKDIR": "/tmp/work",
                "CODEX_TIMEOUT_SECONDS": "321",
            },
            clear=False,
        ):
            _codex_bin, args, _timeout, _workdir = server.build_codex_resume_args(
                "019d5868-71ba-7101-9143-81867f3db5bf",
                "continue",
                "/tmp/last.txt",
                reasoning_effort="medium",
            )

        self.assertIn("--config", args)
        self.assertIn('model_reasoning_effort="medium"', args)

    def test_build_codex_child_env_strips_slack_variables(self):
        with patch.dict(os.environ, {"PATH": "/bin", "SLACK_BOT_TOKEN": "from-os"}, clear=True):
            with patch.dict(server.ENV, {"CUSTOM_ENV": "1", "SLACK_APP_TOKEN": "from-env"}, clear=False):
                child_env = server.build_codex_child_env()

        self.assertEqual(child_env["CUSTOM_ENV"], "1")
        self.assertNotIn("SLACK_BOT_TOKEN", child_env)
        self.assertNotIn("SLACK_APP_TOKEN", child_env)

    def test_get_app_server_stdio_line_limit_bytes_uses_default_on_invalid_input(self):
        with patch.dict(server.ENV, {"CODEX_SLACK_APP_SERVER_LINE_LIMIT_BYTES": "invalid"}, clear=False):
            value = server.get_app_server_stdio_line_limit_bytes()
        self.assertEqual(value, server.DEFAULT_APP_SERVER_STDIO_LINE_LIMIT_BYTES)

    def test_get_codex_settings_allows_zero_timeout(self):
        with patch.dict(server.ENV, {"CODEX_TIMEOUT_SECONDS": "0"}, clear=False):
            _codex_bin, _model, _workdir, timeout, _sandbox, _extra_args, _full_auto = server.get_codex_settings()
        self.assertEqual(timeout, 0)

    def test_format_elapsed_seconds_formats_hours_minutes_and_seconds(self):
        self.assertEqual(server.format_elapsed_seconds(5), "5s")
        self.assertEqual(server.format_elapsed_seconds(65), "1m 5s")
        self.assertEqual(server.format_elapsed_seconds(3665), "1h 1m 5s")

    def test_parse_codex_json_events_extracts_thread_id_and_agent_messages(self):
        payload = "\n".join(
            [
                '{"type":"thread.started","thread_id":"019d5868-71ba-7101-9143-81867f3db5bf"}',
                '{"type":"turn.started"}',
                '{"type":"item.completed","item":{"type":"agent_message","text":"first"}}',
                '{"type":"item.completed","item":{"type":"agent_message","text":"second"}}',
            ]
        )

        session_id, message_text = server.parse_codex_json_events(payload)

        self.assertEqual(session_id, "019d5868-71ba-7101-9143-81867f3db5bf")
        self.assertEqual(message_text, "first\n\nsecond")

    def test_process_codex_json_event_updates_session_tracker_on_thread_started(self):
        tracker = server.SessionIdTracker()
        parsed_session_id = server.process_codex_json_event(
            {"type": "thread.started", "thread_id": "019d5868-71ba-7101-9143-81867f3db5bf"},
            None,
            [],
            session_id_tracker=tracker,
        )

        self.assertEqual(parsed_session_id, "019d5868-71ba-7101-9143-81867f3db5bf")
        self.assertEqual(tracker.get(), "019d5868-71ba-7101-9143-81867f3db5bf")


class RuntimePolicyTests(unittest.TestCase):
    def test_resolve_runtime_policy_settings_respects_extra_args_full_auto(self):
        with patch.dict(
            server.ENV,
            {
                "CODEX_EXTRA_ARGS": "--full-auto --approval-policy on-failure",
                "CODEX_FULL_AUTO": "0",
                "CODEX_SANDBOX_MODE": "read-only",
            },
            clear=False,
        ):
            sandbox, approval_policy = server.resolve_runtime_policy_settings()

        self.assertEqual(sandbox, "workspace-write")
        self.assertEqual(approval_policy, "on-failure")

    def test_resolve_runtime_policy_settings_respects_dangerous_bypass_flag(self):
        with patch.dict(
            server.ENV,
            {
                "CODEX_EXTRA_ARGS": "--dangerously-bypass-approvals-and-sandbox",
                "CODEX_FULL_AUTO": "0",
                "CODEX_SANDBOX_MODE": "read-only",
            },
            clear=False,
        ):
            sandbox, approval_policy = server.resolve_runtime_policy_settings()

        self.assertEqual(sandbox, "danger-full-access")
        self.assertEqual(approval_policy, "never")


class ProgressExtractionTests(unittest.TestCase):
    def test_async_progress_reporter_batches_messages(self):
        client = DummyClient()
        reporter = server.AsyncProgressReporter(client, "C1", "1", batch_seconds=0.05)
        self.addCleanup(reporter.close)

        reporter.enqueue("*Codex Progress*\n> first")
        reporter.enqueue("*Codex Progress*\n> second")
        deadline = time.monotonic() + 1.5
        while len(client.messages) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertEqual(len(client.messages), 1)
        self.assertEqual(
            client.messages[0]["text"],
            "*Codex Progress*\n> first\n\n*Codex Progress*\n> second",
        )

    def test_async_progress_reporter_flushes_immediately(self):
        client = DummyClient()
        reporter = server.AsyncProgressReporter(client, "C1", "1", batch_seconds=10)
        self.addCleanup(reporter.close)

        reporter.enqueue("*Codex Progress*\n> first")
        reporter.enqueue("*Codex Progress*\n> second")
        reporter.flush()

        self.assertEqual(len(client.messages), 1)
        self.assertEqual(
            client.messages[0]["text"],
            "*Codex Progress*\n> first\n\n*Codex Progress*\n> second",
        )

    def test_extract_progress_events_keeps_non_final_agent_messages(self):
        response = make_thread_response(
            make_turn(
                "turn-1",
                agent_message("a1", "commentary", "working"),
                agent_message("a2", "final_answer", "done"),
            )
        )

        events = server.extract_progress_events(response)

        self.assertEqual(
            events,
            [
                server.ProgressEvent(turn_id="turn-1", item_id="a1", phase="commentary", text="working"),
            ],
        )

    def test_build_progress_messages_emits_only_new_suffix_when_text_grows(self):
        previous = {"a1": "hello"}
        events = [server.ProgressEvent(turn_id="turn-1", item_id="a1", phase="commentary", text="hello world")]

        messages = server.build_progress_messages(events, previous)

        self.assertEqual(messages, ["*Codex Progress*\n> world"])
        self.assertEqual(previous["a1"], "hello world")

    def test_build_progress_messages_skips_unchanged_text(self):
        previous = {"a1": "hello"}
        events = [server.ProgressEvent(turn_id="turn-1", item_id="a1", phase="commentary", text="hello")]

        messages = server.build_progress_messages(events, previous)

        self.assertEqual(messages, [])

    def test_run_codex_with_updates_picks_up_new_session_id_for_progress_polling(self):
        discovered_session_id = "019d5868-71ba-7101-9143-81867f3db5bf"
        progress_calls = []

        def fake_run_codex(
            prompt,
            session_id=None,
            session_id_tracker=None,
            reasoning_effort=None,
            workdir_override=None,
            image_paths=None,
        ):
            self.assertIsNone(session_id)
            self.assertIsNotNone(session_id_tracker)
            self.assertIsNone(reasoning_effort)
            self.assertIsNone(workdir_override)
            self.assertIsNone(image_paths)
            session_id_tracker.set(discovered_session_id)
            time.sleep(1.2)
            return server.CodexRunResult(
                session_id=discovered_session_id,
                text="done",
                exit_code=0,
                raw_output="",
                final_output="done",
                json_output="done",
                cleaned_output="done",
                timed_out=False,
            )

        def fake_progress(client, channel, thread_ts, session_id, previous_text_by_item_id):
            progress_calls.append((session_id, dict(previous_text_by_item_id)))

        with patch.object(server, "run_codex", side_effect=fake_run_codex):
            with patch.object(server, "maybe_post_progress_updates", side_effect=fake_progress):
                with patch.object(server, "get_progress_poll_seconds", return_value=1):
                    with patch.object(server, "get_progress_heartbeat_seconds", return_value=999):
                        result = server.run_codex_with_updates(
                            DummyClient(),
                            "C1",
                            "1",
                            "start a new session",
                            session_id=None,
                            enable_progress=True,
                        )

        self.assertEqual(result.session_id, discovered_session_id)
        self.assertEqual(progress_calls, [(discovered_session_id, {})])

    def test_run_runtime_turn_with_updates_emits_only_filtered_agent_progress(self):
        discovered_session_id = "019d5868-71ba-7101-9143-81867f3db5bf"
        client = DummyClient()

        class FakeRuntime:
            def run_turn(
                self,
                *,
                session_id=None,
                input_items=None,
                thread_config=None,
                turn_overrides=None,
                collaboration_mode=None,
                heartbeat_seconds=None,
                on_turn_started=None,
                on_step=None,
                on_heartbeat=None,
                on_user_input_request=None,
            ):
                on_turn_started(discovered_session_id, "turn-1")
                on_step(ns(text='/bin/zsh -lc "pwd"', step_type="exec", item_type="commandExecution", data={}, turn_id="turn-1", item_id="c1"))
                on_step(
                    ns(
                        text="正在检查测试并准备修复。",
                        step_type="codex",
                        item_type="agentMessage",
                        data={"item": {"phase": "commentary"}},
                        turn_id="turn-1",
                        item_id="a1",
                    )
                )
                on_step(
                    ns(
                        text="最终答案",
                        step_type="codex",
                        item_type="agentMessage",
                        data={"item": {"phase": "final_answer"}},
                        turn_id="turn-1",
                        item_id="a2",
                    )
                )
                return ns(
                    session_id=discovered_session_id,
                    final_text="done",
                    steps=[],
                )

        with patch.object(server, "get_app_runtime", return_value=FakeRuntime()):
            result = server.run_runtime_turn_with_updates(
                client,
                "C1",
                "1",
                "C1:1",
                "continue",
                session_id=None,
                enable_progress=True,
            )

        self.assertEqual(result.session_id, discovered_session_id)
        progress_messages = [message["text"] for message in client.messages if "Codex Progress" in message["text"]]
        self.assertEqual(progress_messages, ["*Codex Progress*\n> 正在检查测试并准备修复。"])

    def test_run_runtime_turn_with_updates_persists_only_conversation_event_cursor(self):
        discovered_session_id = "019d5868-71ba-7101-9143-81867f3db5bf"
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        store = server.SlackThreadSessionStore(Path(tmpdir.name) / "sessions.json")
        thread_key = "C1:1"
        channel = "C1"
        thread_ts = "1"
        user_id = "U111"

        class FakeRuntime:
            def run_turn(
                self,
                *,
                session_id=None,
                input_items=None,
                thread_config=None,
                turn_overrides=None,
                collaboration_mode=None,
                heartbeat_seconds=None,
                on_turn_started=None,
                on_step=None,
                on_heartbeat=None,
                on_user_input_request=None,
            ):
                on_turn_started(discovered_session_id, "turn-1")
                on_step(
                    ns(
                        text="进度说明",
                        step_type="codex",
                        item_type="agentMessage",
                        data={"item": {"phase": "commentary"}},
                        turn_id="turn-1",
                        item_id="a1",
                    )
                )
                on_step(
                    ns(
                        text="最终答案",
                        step_type="codex",
                        item_type="agentMessage",
                        data={"item": {"phase": "final_answer"}},
                        turn_id="turn-1",
                        item_id="a2",
                    )
                )
                return ns(
                    session_id=discovered_session_id,
                    final_text="done",
                    steps=[],
                )

        with patch.object(server, "SESSION_STORE", store):
            with patch.object(server, "get_app_runtime", return_value=FakeRuntime()):
                server.run_runtime_turn_with_updates(
                    DummyClient(),
                    channel,
                    thread_ts,
                    thread_key,
                    "continue",
                    session_id=None,
                    enable_progress=False,
                    owner_user_id=user_id,
                )

        self.assertEqual(
            store.get_watch_last_event_key(
                thread_key,
                current_session_id=discovered_session_id,
            ),
            ("turn-1", "a2"),
        )

    def test_run_runtime_turn_with_updates_uses_reporter_and_flushes_before_return(self):
        discovered_session_id = "019d5868-71ba-7101-9143-81867f3db5bf"

        class FakeReporter:
            def __init__(self):
                self.messages = []
                self.flushed = False
                self.closed = False

            def enqueue(self, text):
                self.messages.append(text)

            def flush(self, timeout=10):
                self.flushed = True

            def close(self, timeout=10):
                self.closed = True

        class FakeRuntime:
            def run_turn(
                self,
                *,
                session_id=None,
                input_items=None,
                thread_config=None,
                turn_overrides=None,
                collaboration_mode=None,
                heartbeat_seconds=None,
                on_turn_started=None,
                on_step=None,
                on_heartbeat=None,
                on_user_input_request=None,
            ):
                on_turn_started(discovered_session_id, "turn-1")
                on_step(
                    ns(
                        text="正在检查测试并准备修复。",
                        step_type="codex",
                        item_type="agentMessage",
                        data={"item": {"phase": "commentary"}},
                        turn_id="turn-1",
                        item_id="a1",
                    )
                )
                on_heartbeat(discovered_session_id, "turn-1", 12)
                return ns(
                    session_id=discovered_session_id,
                    final_text="done",
                    steps=[],
                )

        fake_reporter = FakeReporter()
        with patch.object(server, "get_app_runtime", return_value=FakeRuntime()):
            with patch.object(server, "create_progress_reporter", return_value=fake_reporter):
                result = server.run_runtime_turn_with_updates(
                    DummyClient(),
                    "C1",
                    "1",
                    "C1:1",
                    "continue",
                    session_id=None,
                    enable_progress=True,
                )

        self.assertEqual(result.session_id, discovered_session_id)
        self.assertEqual(
            fake_reporter.messages,
            [
                "*Codex Progress*\n> 正在检查测试并准备修复。",
                "仍在运行，已持续 12s。 session `019d5868-71ba-7101-9143-81867f3db5bf`",
            ],
        )
        self.assertTrue(fake_reporter.flushed)
        self.assertTrue(fake_reporter.closed)

    def test_run_runtime_turn_with_updates_passes_collaboration_mode_to_runtime(self):
        discovered_session_id = "019d5868-71ba-7101-9143-81867f3db5bf"
        captured = {}

        class FakeRuntime:
            def run_turn(
                self,
                *,
                session_id=None,
                input_items=None,
                thread_config=None,
                turn_overrides=None,
                collaboration_mode=None,
                heartbeat_seconds=None,
                on_turn_started=None,
                on_step=None,
                on_heartbeat=None,
                on_user_input_request=None,
            ):
                captured["collaboration_mode"] = collaboration_mode
                captured["has_user_input_handler"] = callable(on_user_input_request)
                on_turn_started(discovered_session_id, "turn-1")
                return ns(session_id=discovered_session_id, final_text="done", steps=[])

        with patch.object(server, "get_app_runtime", return_value=FakeRuntime()):
            result = server.run_runtime_turn_with_updates(
                DummyClient(),
                "C1",
                "1",
                "C1:1",
                "continue",
                session_id=None,
                collaboration_mode=server.COLLABORATION_MODE_PLAN,
                reasoning_effort="high",
            )

        self.assertEqual(result.session_id, discovered_session_id)
        self.assertEqual(
            captured["collaboration_mode"],
            {
                "mode": "plan",
                "settings": {
                    "model": server.get_codex_settings()[1],
                    "reasoningEffort": "high",
                    "developerInstructions": None,
                },
            },
        )
        self.assertTrue(captured["has_user_input_handler"])


class RequestUserInputHelperTests(unittest.TestCase):
    def test_extract_request_user_input_submission_supports_other_option(self):
        pending_request = server.PendingSlackUserInputRequest(
            token="tok-1",
            thread_key="C1:1",
            channel="C1",
            thread_ts="1",
            owner_user_id="U111",
            session_id="sess-1",
            request=server.RuntimeUserInputRequest(
                request_id="req-1",
                thread_id="sess-1",
                turn_id="turn-1",
                item_id="item-1",
                questions=[
                    server.RuntimeUserInputQuestion(
                        id="choose",
                        header="Choose",
                        question="Pick one",
                        is_other=True,
                        options=[
                            RuntimeUserInputQuestionOption(
                                label="Yes (Recommended)",
                                description="Continue",
                            ),
                            RuntimeUserInputQuestionOption(
                                label="No",
                                description="Stop",
                            ),
                        ],
                    )
                ],
            ),
            future=concurrent.futures.Future(),
        )
        view_state = {
            "values": {
                server.get_request_user_input_choice_block_id(0): {
                    "choice": {
                        "selected_option": {
                            "value": server.REQUEST_USER_INPUT_OTHER_VALUE,
                        }
                    }
                },
                server.get_request_user_input_other_block_id(0): {
                    "other_text": {
                        "value": "Something else",
                    }
                },
            }
        }

        payload, errors = server.extract_request_user_input_submission(view_state, pending_request)

        self.assertEqual(errors, {})
        self.assertEqual(payload, {"answers": {"choose": {"answers": ["Something else"]}}})

    def test_build_thread_collaboration_mode_message_marks_active_button(self):
        text, blocks = server.build_thread_collaboration_mode_message(
            "C1:1",
            session_id="sess-1",
            collaboration_mode=server.COLLABORATION_MODE_PLAN,
        )

        self.assertIn("Current: `Plan`", blocks[0]["text"]["text"])
        plan_button, default_button = blocks[1]["elements"]
        self.assertNotEqual(plan_button["action_id"], default_button["action_id"])
        self.assertEqual(plan_button.get("style"), "primary")
        self.assertNotIn("style", default_button)


class ProcessPromptTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.store = server.SlackThreadSessionStore(Path(self.tmpdir.name) / "sessions.json")
        self.session_store_patcher = patch.object(server, "SESSION_STORE", self.store)
        self.session_store_patcher.start()
        self.addCleanup(self.session_store_patcher.stop)
        self.selection_cache = server.session_catalog.SessionSelectionCache()
        self.selection_cache_patcher = patch.object(server, "SESSION_SELECTION_CACHE", self.selection_cache)
        self.selection_cache_patcher.start()
        self.addCleanup(self.selection_cache_patcher.stop)
        self.turn_registry_patcher = patch.object(server, "ACTIVE_TURN_REGISTRY", server.ActiveTurnRegistry())
        self.turn_registry_patcher.start()
        self.addCleanup(self.turn_registry_patcher.stop)
        server.WATCHERS.clear()
        self.addCleanup(server.WATCHERS.clear)
        self.client = DummyClient()
        self.channel = "C1"
        self.thread_ts = "1"
        self.user_id = "U111"
        self.thread_key = server.make_thread_key(self.channel, self.thread_ts)
        self.session_id = "019d5868-71ba-7101-9143-81867f3db5bf"

    def test_watch_command_starts_dialogue_watch(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_mode(self.thread_key, server.SESSION_MODE_OBSERVE)

        with patch.object(
            server,
            "build_watch_bootstrap",
            return_value=("最近一轮对话:\n\n*User*\n> hello", ("turn-1", "u1")),
        ) as build_watch_bootstrap:
            with patch.object(server, "start_watcher") as start_watcher:
                server.process_prompt(self.client, self.channel, self.thread_ts, "watch", self.user_id)

        build_watch_bootstrap.assert_called_once_with(self.session_id)
        start_watcher.assert_called_once()
        self.assertIn("已开始持续 watch", self.client.messages[0]["text"])

    def test_restore_background_watchers_restarts_persisted_watch(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_mode(self.thread_key, server.SESSION_MODE_OBSERVE)
        self.store.set_watch_enabled(self.thread_key, True, owner_user_id=self.user_id)
        self.store.set_watch_last_event_key(
            self.thread_key,
            self.session_id,
            ("turn-persisted", "a-persisted"),
            owner_user_id=self.user_id,
        )

        with patch.object(server, "get_latest_event_key_for_session") as get_latest_event_key:
            with patch.object(server, "start_watcher") as start_watcher:
                restored = server.restore_background_watchers(self.client)

        self.assertEqual(restored["restored_count"], 1)
        self.assertEqual(restored["restored"][0]["cursor_source"], "persisted")
        get_latest_event_key.assert_not_called()
        start_watcher.assert_called_once_with(
            self.client,
            self.channel,
            self.thread_ts,
            self.thread_key,
            self.session_id,
            last_event_key=("turn-persisted", "a-persisted"),
            persist_watch=True,
            stop_when_idle=False,
        )

    def test_restore_background_watchers_starts_ephemeral_watch_for_active_control_session(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_mode(self.thread_key, server.SESSION_MODE_CONTROL)

        with patch.object(server, "should_restore_control_recovery_watch", return_value=True):
            with patch.object(server, "get_latest_event_key_for_session", return_value=("turn-9", "a9")):
                with patch.object(server, "start_watcher") as start_watcher:
                    restored = server.restore_background_watchers(self.client)

        self.assertEqual(restored["restored_count"], 1)
        self.assertEqual(restored["restored"][0]["cursor_source"], "latest")
        start_watcher.assert_called_once_with(
            self.client,
            self.channel,
            self.thread_ts,
            self.thread_key,
            self.session_id,
            last_event_key=("turn-9", "a9"),
            persist_watch=False,
            stop_when_idle=True,
        )

    def test_restore_background_watchers_skips_idle_control_session_without_watch(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_mode(self.thread_key, server.SESSION_MODE_CONTROL)

        with patch.object(server, "should_restore_control_recovery_watch", return_value=False):
            with patch.object(server, "start_watcher") as start_watcher:
                restored = server.restore_background_watchers(self.client)

        self.assertEqual(restored["restored_count"], 0)
        self.assertEqual(restored["skipped"][0]["reason"], "idle_control_session")
        start_watcher.assert_not_called()

    def test_restore_background_watchers_backfills_idle_control_session_when_backlog_exists(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_mode(self.thread_key, server.SESSION_MODE_CONTROL)
        self.store.set_watch_last_event_key(
            self.thread_key,
            self.session_id,
            ("turn-old", "a-old"),
            owner_user_id=self.user_id,
        )

        with patch.object(server, "should_restore_control_recovery_watch", return_value=False):
            with patch.object(server, "get_latest_event_key_for_session", return_value=("turn-new", "a-new")):
                with patch.object(server, "start_watcher") as start_watcher:
                    restored = server.restore_background_watchers(self.client)

        self.assertEqual(restored["restored_count"], 1)
        self.assertEqual(restored["restored"][0]["cursor_source"], "persisted")
        start_watcher.assert_called_once_with(
            self.client,
            self.channel,
            self.thread_ts,
            self.thread_key,
            self.session_id,
            last_event_key=("turn-old", "a-old"),
            persist_watch=False,
            stop_when_idle=True,
        )

    def test_watch_loop_async_backfills_backlog_immediately_after_restore(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_mode(self.thread_key, server.SESSION_MODE_CONTROL)

        metadata_response = object()
        thread_response = object()
        stop_event = threading.Event()
        posted_messages = []

        class FakeWatchClient:
            async def start(self):
                return None

            async def initialize(self):
                return None

            async def close(self):
                return None

        async def run_test():
            with patch.object(server, "create_app_server_client", return_value=FakeWatchClient()):
                with patch.object(
                    server,
                    "_read_thread_response_with_client",
                    side_effect=[metadata_response, metadata_response, thread_response],
                ):
                    with patch.object(
                        server,
                        "extract_watch_thread_snapshot",
                        return_value=server.WatchThreadSnapshot(
                            path=None,
                            updated_at=1,
                            status_type="idle",
                        ),
                    ):
                        with patch.object(
                            server,
                            "advance_watch_cursor",
                            return_value=("补发的缺失消息", ("turn-1", "a2"), False),
                        ):
                            with patch.object(
                                server,
                                "_wait_for_watch_signal",
                                side_effect=AssertionError("initial sync should not wait for signal"),
                            ):
                                with patch.object(
                                    server,
                                    "post_chunks",
                                    side_effect=lambda *args: posted_messages.append(args[3]),
                                ):
                                    await server.watch_loop_async(
                                        self.client,
                                        self.channel,
                                        self.thread_ts,
                                        self.thread_key,
                                        self.session_id,
                                        stop_event,
                                        last_event_key=("turn-1", "a1"),
                                        stop_when_idle=True,
                                    )

        asyncio.run(run_test())

        self.assertEqual(posted_messages, ["补发的缺失消息"])
        self.assertEqual(
            self.store.get_watch_last_event_key(
                self.thread_key,
                current_session_id=self.session_id,
            ),
            ("turn-1", "a2"),
        )

    def test_watch_rejects_extra_arguments(self):
        server.process_prompt(self.client, self.channel, self.thread_ts, "watch raw", self.user_id)
        self.assertIn("不再接受参数", self.client.messages[0]["text"])

    def test_recent_command_posts_scoped_session_list(self):
        self.store.set(
            self.thread_key,
            self.session_id,
            owner_user_id=self.user_id,
            session_origin=server.SESSION_ORIGIN_ATTACHED,
            session_cwd="/tmp/attached-project",
        )

        with patch.object(server, "get_recent_sessions_text", return_value="recent list") as get_recent_sessions_text:
            server.process_prompt(self.client, self.channel, self.thread_ts, "recent", self.user_id)

        get_recent_sessions_text.assert_called_once_with(
            self.thread_key,
            self.session_id,
            cwd="/tmp/attached-project",
            include_all=False,
            heading=f"<@{self.user_id}> 当前工作目录下最近的 Codex sessions:",
        )
        self.assertEqual(self.client.messages[0]["text"], "recent list")

    def test_sessions_cwd_command_posts_explicit_scope_list(self):
        with patch.object(server, "get_recent_sessions_text", return_value="cwd list") as get_recent_sessions_text:
            server.process_prompt(self.client, self.channel, self.thread_ts, "sessions --cwd /tmp/project-x", self.user_id)

        get_recent_sessions_text.assert_called_once_with(
            self.thread_key,
            None,
            cwd="/tmp/project-x",
            include_all=False,
            heading=f"<@{self.user_id}> 当前范围下最近的 Codex sessions:",
        )
        self.assertEqual(self.client.messages[0]["text"], "cwd list")

    def test_progress_command_sets_thread_override(self):
        server.process_prompt(self.client, self.channel, self.thread_ts, "progress off", self.user_id)

        self.assertFalse(self.store.get_progress_updates(self.thread_key))
        self.assertIn("progress 推送设置为 `off`", self.client.messages[0]["text"])

    def test_progress_command_reset_clears_thread_override(self):
        self.store.set_progress_updates(self.thread_key, False, owner_user_id=self.user_id)

        with patch.dict(server.ENV, {"CODEX_PROGRESS_UPDATES": "1"}, clear=False):
            server.process_prompt(self.client, self.channel, self.thread_ts, "progress reset", self.user_id)

        self.assertIsNone(self.store.get_progress_updates(self.thread_key))
        self.assertIn("已清除当前 Slack thread 的 progress 设置覆盖", self.client.messages[0]["text"])

    def test_progress_command_status_reports_effective_state(self):
        self.store.set_progress_updates(self.thread_key, False, owner_user_id=self.user_id)

        with patch.dict(server.ENV, {"CODEX_PROGRESS_UPDATES": "1"}, clear=False):
            server.process_prompt(self.client, self.channel, self.thread_ts, "progress", self.user_id)

        text = self.client.messages[0]["text"]
        self.assertIn("progress_updates_effective: `off`", text)
        self.assertIn("progress_updates_source: `thread`", text)

    def test_attach_command_binds_session_in_observe_mode(self):
        with patch.dict(
            server.ENV,
            {"ALLOWED_SLACK_USER_IDS": self.user_id, "ALLOW_SHARED_ATTACH": "0"},
            clear=False,
        ):
            with patch.object(server, "stop_watcher", return_value=False):
                with patch.object(server, "read_thread_cwd", return_value="/tmp/attached-project"):
                    server.process_prompt(
                        self.client,
                        self.channel,
                        self.thread_ts,
                        f"attach {self.session_id}",
                        self.user_id,
                    )

        self.assertEqual(self.store.get(self.thread_key), self.session_id)
        self.assertEqual(self.store.get_mode(self.thread_key), server.SESSION_MODE_OBSERVE)
        self.assertEqual(self.store.get_session_cwd(self.thread_key), "/tmp/attached-project")
        self.assertIn("默认已进入 `observe` 模式", self.client.messages[0]["text"])
        self.assertIn("`/tmp/attached-project`", self.client.messages[0]["text"])

    def test_attach_recent_binds_cached_session_in_observe_mode(self):
        selected_session_id = "019d5868-71ba-7101-9143-81867f3db5c0"
        self.selection_cache.put(self.thread_key, [self.session_id, selected_session_id])

        with patch.dict(
            server.ENV,
            {"ALLOWED_SLACK_USER_IDS": self.user_id, "ALLOW_SHARED_ATTACH": "0"},
            clear=False,
        ):
            with patch.object(server, "stop_watcher", return_value=False):
                with patch.object(server, "read_thread_cwd", return_value="/tmp/selected-project"):
                    server.process_prompt(
                        self.client,
                        self.channel,
                        self.thread_ts,
                        "attach recent 2",
                        self.user_id,
                    )

        self.assertEqual(self.store.get(self.thread_key), selected_session_id)
        self.assertEqual(self.store.get_mode(self.thread_key), server.SESSION_MODE_OBSERVE)
        self.assertEqual(self.store.get_session_cwd(self.thread_key), "/tmp/selected-project")
        self.assertIn(selected_session_id, self.client.messages[0]["text"])

    def test_name_command_renames_current_session(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)

        with patch.object(server.thread_views, "rename_thread", return_value="triage flaky test") as rename_thread:
            server.process_prompt(self.client, self.channel, self.thread_ts, "name triage flaky test", self.user_id)

        rename_thread.assert_called_once_with(
            server.get_codex_app_server_config(),
            self.session_id,
            "triage flaky test",
        )
        self.assertIn("已将当前 session 重命名为 `triage flaky test`", self.client.messages[0]["text"])

    def test_handoff_uses_runtime_path_for_controlled_session(self):
        self.store.set(
            self.thread_key,
            self.session_id,
            owner_user_id=self.user_id,
            session_origin=server.SESSION_ORIGIN_SLACK,
            session_cwd="/tmp/runtime-project",
        )
        self.store.set_mode(self.thread_key, server.SESSION_MODE_CONTROL)
        result = server.CodexRunResult(
            session_id=self.session_id,
            text="Current Goal:\ncontinue testing",
            exit_code=0,
            raw_output="",
            final_output="Current Goal:\ncontinue testing",
            json_output="",
            cleaned_output="Current Goal:\ncontinue testing",
            timed_out=False,
        )

        with patch.object(server, "read_thread_cwd", return_value="/tmp/runtime-project"):
            with patch.object(server, "run_runtime_turn_with_updates", return_value=result) as run_runtime_turn_with_updates:
                with patch.object(server, "run_codex_with_updates") as run_codex_with_updates:
                    server.process_prompt(self.client, self.channel, self.thread_ts, "handoff", self.user_id)

        run_runtime_turn_with_updates.assert_called_once()
        run_codex_with_updates.assert_not_called()
        self.assertEqual(run_runtime_turn_with_updates.call_args.args[4], server.build_handoff_prompt())
        self.assertEqual(run_runtime_turn_with_updates.call_args.kwargs["session_id"], self.session_id)
        self.assertEqual(run_runtime_turn_with_updates.call_args.kwargs["workdir_override"], "/tmp/runtime-project")

    def test_recap_uses_runtime_path_for_controlled_session(self):
        self.store.set(
            self.thread_key,
            self.session_id,
            owner_user_id=self.user_id,
            session_origin=server.SESSION_ORIGIN_SLACK,
            session_cwd="/tmp/runtime-project",
        )
        self.store.set_mode(self.thread_key, server.SESSION_MODE_CONTROL)
        result = server.CodexRunResult(
            session_id=self.session_id,
            text="Recent Progress:\n- tested steer",
            exit_code=0,
            raw_output="",
            final_output="Recent Progress:\n- tested steer",
            json_output="",
            cleaned_output="Recent Progress:\n- tested steer",
            timed_out=False,
        )

        with patch.object(server, "read_thread_cwd", return_value="/tmp/runtime-project"):
            with patch.object(server, "run_runtime_turn_with_updates", return_value=result) as run_runtime_turn_with_updates:
                with patch.object(server, "run_codex_with_updates") as run_codex_with_updates:
                    server.process_prompt(self.client, self.channel, self.thread_ts, "recap", self.user_id)

        run_runtime_turn_with_updates.assert_called_once()
        run_codex_with_updates.assert_not_called()
        self.assertEqual(run_runtime_turn_with_updates.call_args.args[4], server.build_recap_prompt())
        self.assertEqual(run_runtime_turn_with_updates.call_args.kwargs["session_id"], self.session_id)
        self.assertEqual(run_runtime_turn_with_updates.call_args.kwargs["workdir_override"], "/tmp/runtime-project")

    def test_image_only_message_uses_default_prompt_and_image_paths(self):
        result = server.CodexRunResult(
            session_id=self.session_id,
            text="done",
            exit_code=0,
            raw_output="",
            final_output="done",
            json_output="",
            cleaned_output="done",
            timed_out=False,
        )
        image_download = server.slack_image_inputs.SlackImageDownload(
            file_id="F1",
            filename="cat.png",
            download_url="https://files.slack.com/cat.png",
            mimetype="image/png",
        )
        image_path = Path("/tmp/cat.png")

        with patch.object(server.slack_image_inputs, "build_image_downloads_from_event", return_value=[image_download]):
            with patch.object(server.slack_image_inputs, "download_slack_image_files", return_value=[image_path]):
                with patch.object(server.slack_image_inputs, "cleanup_downloaded_files") as cleanup_downloaded_files:
                    with patch.object(server.slack_image_inputs, "cleanup_download_directory") as cleanup_download_directory:
                        with patch.object(
                            server,
                            "run_runtime_turn_with_updates",
                            return_value=result,
                        ) as run_runtime_turn_with_updates:
                            server.process_prompt(
                                self.client,
                                self.channel,
                                self.thread_ts,
                                "",
                                self.user_id,
                                slack_event_payload={"event": {"files": [{"id": "F1"}]}},
                            )

        self.assertEqual(run_runtime_turn_with_updates.call_args.kwargs["image_paths"], [image_path])
        self.assertEqual(run_runtime_turn_with_updates.call_args.args[4], server.DEFAULT_IMAGE_ONLY_PROMPT)
        cleanup_downloaded_files.assert_called_once_with([image_path])
        cleanup_download_directory.assert_called_once()

    def test_image_download_failure_returns_user_facing_error(self):
        image_download = server.slack_image_inputs.SlackImageDownload(
            file_id="F1",
            filename="cat.png",
            download_url="https://files.slack.com/cat.png",
            mimetype="image/png",
        )

        with patch.object(server.slack_image_inputs, "build_image_downloads_from_event", return_value=[image_download]):
            with patch.object(server.slack_image_inputs, "download_slack_image_files", side_effect=RuntimeError("boom")):
                with patch.object(server, "run_codex_with_updates") as run_codex_with_updates:
                    server.process_prompt(
                        self.client,
                        self.channel,
                        self.thread_ts,
                        "",
                        self.user_id,
                        slack_event_payload={"event": {"files": [{"id": "F1"}]}},
                    )

        run_codex_with_updates.assert_not_called()
        self.assertIn("下载 Slack 图片失败", self.client.messages[-1]["text"])

    def test_document_only_message_uses_default_prompt_and_document_manifest(self):
        result = server.CodexRunResult(
            session_id=self.session_id,
            text="done",
            exit_code=0,
            raw_output="",
            final_output="done",
            json_output="",
            cleaned_output="done",
            timed_out=False,
        )
        document_download = server.slack_document_inputs.SlackDocumentDownload(
            file_id="F1",
            filename="report.pdf",
            download_url="https://files.slack.com/report.pdf",
            mimetype="application/pdf",
        )
        downloaded_document = server.slack_document_inputs.DownloadedSlackDocument(
            file_id="F1",
            filename="report.pdf",
            path=Path("/tmp/report.pdf"),
            mimetype="application/pdf",
        )

        with patch.object(server.slack_image_inputs, "build_image_downloads_from_event", return_value=[]):
            with patch.object(server.slack_document_inputs, "build_document_downloads_from_event", return_value=[document_download]):
                with patch.object(server.slack_document_inputs, "download_slack_document_files", return_value=[downloaded_document]):
                    with patch.object(server.slack_document_inputs, "cleanup_downloaded_documents") as cleanup_downloaded_documents:
                        with patch.object(server.slack_document_inputs, "cleanup_download_directory") as cleanup_download_directory:
                            with patch.object(
                                server,
                                "run_runtime_turn_with_updates",
                                return_value=result,
                            ) as run_runtime_turn_with_updates:
                                server.process_prompt(
                                    self.client,
                                    self.channel,
                                    self.thread_ts,
                                    "",
                                    self.user_id,
                                    slack_event_payload={"event": {"files": [{"id": "F1"}]}},
                                )

        prompt = run_runtime_turn_with_updates.call_args.args[4]
        self.assertIn(server.DEFAULT_DOCUMENT_ONLY_PROMPT, prompt)
        self.assertIn("report.pdf", prompt)
        self.assertIn("/tmp/report.pdf", prompt)
        cleanup_downloaded_documents.assert_called_once_with([downloaded_document])
        cleanup_download_directory.assert_called_once()

    def test_prompt_with_document_attachment_appends_document_manifest(self):
        result = server.CodexRunResult(
            session_id=self.session_id,
            text="done",
            exit_code=0,
            raw_output="",
            final_output="done",
            json_output="",
            cleaned_output="done",
            timed_out=False,
        )
        document_download = server.slack_document_inputs.SlackDocumentDownload(
            file_id="F1",
            filename="notes.md",
            download_url="https://files.slack.com/notes.md",
            mimetype="text/markdown",
        )
        downloaded_document = server.slack_document_inputs.DownloadedSlackDocument(
            file_id="F1",
            filename="notes.md",
            path=Path("/tmp/notes.md"),
            mimetype="text/markdown",
        )

        with patch.object(server.slack_image_inputs, "build_image_downloads_from_event", return_value=[]):
            with patch.object(server.slack_document_inputs, "build_document_downloads_from_event", return_value=[document_download]):
                with patch.object(server.slack_document_inputs, "download_slack_document_files", return_value=[downloaded_document]):
                    with patch.object(server.slack_document_inputs, "cleanup_downloaded_documents"):
                        with patch.object(server.slack_document_inputs, "cleanup_download_directory"):
                            with patch.object(
                                server,
                                "run_runtime_turn_with_updates",
                                return_value=result,
                            ) as run_runtime_turn_with_updates:
                                server.process_prompt(
                                    self.client,
                                    self.channel,
                                    self.thread_ts,
                                    "请总结这份文档",
                                    self.user_id,
                                    slack_event_payload={"event": {"files": [{"id": "F1"}]}},
                                )

        prompt = run_runtime_turn_with_updates.call_args.args[4]
        self.assertTrue(prompt.startswith("请总结这份文档"))
        self.assertIn("notes.md", prompt)
        self.assertIn("/tmp/notes.md", prompt)

    def test_document_download_failure_returns_user_facing_error(self):
        document_download = server.slack_document_inputs.SlackDocumentDownload(
            file_id="F1",
            filename="report.pdf",
            download_url="https://files.slack.com/report.pdf",
            mimetype="application/pdf",
        )

        with patch.object(server.slack_image_inputs, "build_image_downloads_from_event", return_value=[]):
            with patch.object(server.slack_document_inputs, "build_document_downloads_from_event", return_value=[document_download]):
                with patch.object(server.slack_document_inputs, "download_slack_document_files", side_effect=RuntimeError("boom")):
                    with patch.object(server, "run_codex_with_updates") as run_codex_with_updates:
                        server.process_prompt(
                            self.client,
                            self.channel,
                            self.thread_ts,
                            "",
                            self.user_id,
                            slack_event_payload={"event": {"files": [{"id": "F1"}]}},
                        )

        run_codex_with_updates.assert_not_called()
        self.assertIn("下载 Slack 文档失败", self.client.messages[-1]["text"])

    def test_unsupported_attachment_without_text_returns_hint(self):
        with patch.object(server.slack_image_inputs, "build_image_downloads_from_event", return_value=[]):
            with patch.object(server.slack_document_inputs, "build_document_downloads_from_event", return_value=[]):
                server.process_prompt(
                    self.client,
                    self.channel,
                    self.thread_ts,
                    "",
                    self.user_id,
                    slack_event_payload={"event": {"files": [{"id": "F1", "name": "archive.zip"}]}},
                )

        self.assertIn("暂不支持的附件类型", self.client.messages[-1]["text"])

    def test_get_home_bindings_rows_prefers_session_title_and_rename_action(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id, session_cwd="/tmp/project")
        with patch.object(server, "read_thread_response", return_value={"thread": {"name": "mobile handoff"}}):
            rows = server.get_home_bindings_rows(self.user_id, limit=5)

        self.assertEqual(rows[0]["label"], "mobile handoff")
        self.assertEqual(rows[0]["status_text"], "Channel Thread")
        self.assertEqual(rows[0]["action_id"], "binding_rename_open")
        self.assertEqual(rows[0]["action_text"], "Rename")

    def test_get_home_bindings_rows_falls_back_to_binding_label_when_thread_title_unavailable(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id, session_cwd="/tmp/project")

        with patch.object(server, "read_thread_response", side_effect=RuntimeError("boom")):
            rows = server.get_home_bindings_rows(self.user_id, limit=5)

        self.assertEqual(rows[0]["label"], "Channel Thread")
        self.assertIsNone(rows[0]["status_text"])

    def test_get_home_bindings_rows_includes_pending_subagent_summary(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id, session_cwd="/tmp/project")
        self.store.set_pending_subagent_target(
            self.thread_key,
            thread_id="sub-1",
            agent_nickname="Atlas",
            agent_role="explorer",
            owner_user_id=self.user_id,
            session_id=self.session_id,
        )

        with patch.object(server, "read_thread_response", return_value={"thread": {"name": "mobile handoff"}}):
            rows = server.get_home_bindings_rows(self.user_id, limit=5)

        self.assertIn("pending next target: Atlas (explorer)", rows[0]["status_text"])

    def test_build_home_rename_modal_encodes_binding_metadata(self):
        modal = server.build_home_rename_modal(
            thread_key=self.thread_key,
            session_id=self.session_id,
            initial_title="triage flaky test",
        )

        self.assertEqual(modal["callback_id"], "binding_rename_submit")
        self.assertEqual(
            server.decode_home_binding_value(modal["private_metadata"]),
            (self.thread_key, self.session_id),
        )
        input_element = modal["blocks"][0]["element"]
        self.assertEqual(input_element["initial_value"], "triage flaky test")

    def test_status_reports_watch_state(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)

        with patch.object(server, "get_watcher", return_value=object()):
            server.process_prompt(self.client, self.channel, self.thread_ts, "status", self.user_id)

        text = self.client.messages[0]["text"]
        self.assertIn("- thread_key: `C1:1`", text)
        self.assertIn(f"- session_id: `{self.session_id}`", text)
        self.assertIn("- watch_active: `yes`", text)
        self.assertEqual(len(self.client.messages), 1)

    def test_status_reports_reasoning_effort_state(self):
        self.store.set_reasoning_effort(self.thread_key, "high", owner_user_id=self.user_id)
        self.store.set(
            self.thread_key,
            self.session_id,
            owner_user_id=self.user_id,
            session_origin=server.SESSION_ORIGIN_SLACK,
            session_cwd="/tmp/project-c",
        )

        server.process_prompt(self.client, self.channel, self.thread_ts, "status", self.user_id)

        text = self.client.messages[0]["text"]
        self.assertIn("- session_origin: `slack`", text)
        self.assertIn("- session_cwd: `/tmp/project-c`", text)
        self.assertIn("- thread_reasoning_effort: `high`", text)
        self.assertIn("- effective_reasoning_effort: `high (thread)`", text)
        self.assertEqual(len(self.client.messages), 1)

    def test_status_reports_pending_subagent_target(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_pending_subagent_target(
            self.thread_key,
            thread_id="sub-1",
            agent_nickname="Atlas",
            agent_role="explorer",
            owner_user_id=self.user_id,
            session_id=self.session_id,
            armed_at=int(time.time()),
        )

        server.process_prompt(self.client, self.channel, self.thread_ts, "status", self.user_id)

        text = self.client.messages[0]["text"]
        self.assertIn("pending_subagent_target", text)
        self.assertIn("Atlas (explorer)", text)

    def test_subagents_command_without_session_returns_hint(self):
        server.process_prompt(self.client, self.channel, self.thread_ts, "subagents", self.user_id)
        self.assertIn("还没有绑定 session", self.client.messages[0]["text"])

    def test_subagents_command_posts_blocks_in_observe_mode(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_mode(self.thread_key, server.SESSION_MODE_OBSERVE)
        subagents = [
            {
                "thread_id": "sub-1",
                "agent_nickname": "Atlas",
                "agent_role": "explorer",
                "status": "idle",
                "updated_at": int(time.time()),
            }
        ]

        with patch.object(server, "discover_subagents", return_value=subagents):
            server.process_prompt(self.client, self.channel, self.thread_ts, "subagents", self.user_id)

        self.assertEqual(self.client.messages[0]["blocks"][0]["text"]["text"].splitlines()[0], "*Subagents*")
        self.assertIn("observe", self.client.messages[0]["blocks"][0]["text"]["text"])
        action_ids = [
            element["action_id"]
            for block in self.client.messages[0]["blocks"]
            if block.get("type") == "actions"
            for element in block.get("elements", [])
        ]
        self.assertNotIn(server.SUBAGENT_SEND_NEXT_ACTION, action_ids)
        self.assertIn(server.SUBAGENT_OBSERVE_ACTION, action_ids)
        self.assertIn(server.SUBAGENT_ATTACH_ACTION, action_ids)

    def test_pending_subagent_route_sends_one_message_and_clears_state(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_mode(self.thread_key, server.SESSION_MODE_CONTROL)
        self.store.set_pending_subagent_target(
            self.thread_key,
            thread_id="sub-1",
            agent_nickname="Atlas",
            agent_role="explorer",
            owner_user_id=self.user_id,
            session_id=self.session_id,
        )
        result = server.CodexRunResult(
            session_id="sub-1",
            text="subagent done",
            exit_code=0,
            raw_output="",
            final_output="subagent done",
            json_output="",
            cleaned_output="subagent done",
            timed_out=False,
        )

        with patch.object(
            server,
            "find_subagent_for_main_session",
            return_value={
                "thread_id": "sub-1",
                "agent_nickname": "Atlas",
                "agent_role": "explorer",
                "status": "idle",
                "updated_at": int(time.time()),
            },
        ):
            with patch.object(server, "read_thread_cwd", return_value="/tmp/subagent"):
                with patch.object(server, "run_runtime_turn_with_updates", return_value=result) as run_runtime_turn:
                    with patch.object(
                        server,
                        "maybe_prefix_thread_output",
                        side_effect=lambda _sid, text, **_kwargs: f"Atlas · explorer\n\n{text}",
                    ):
                        server.process_prompt(self.client, self.channel, self.thread_ts, "continue", self.user_id)

        self.assertEqual(run_runtime_turn.call_args.kwargs["session_id"], "sub-1")
        self.assertFalse(run_runtime_turn.call_args.kwargs["track_active_turn"])
        self.assertFalse(run_runtime_turn.call_args.kwargs["persist_session_binding"])
        self.assertIsNone(
            self.store.get_pending_subagent_target(
                self.thread_key,
                current_session_id=self.session_id,
                owner_user_id=self.user_id,
            )
        )
        self.assertIn("当前目标已恢复为 `main`", self.client.messages[-1]["text"])
        self.assertEqual(self.store.get(self.thread_key), self.session_id)

    def test_status_command_does_not_consume_pending_subagent_target(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_pending_subagent_target(
            self.thread_key,
            thread_id="sub-1",
            agent_nickname="Atlas",
            agent_role="explorer",
            owner_user_id=self.user_id,
            session_id=self.session_id,
        )

        server.process_prompt(self.client, self.channel, self.thread_ts, "status", self.user_id)

        self.assertIsNotNone(
            self.store.get_pending_subagent_target(
                self.thread_key,
                current_session_id=self.session_id,
                owner_user_id=self.user_id,
            )
        )

    def test_invalid_pending_subagent_target_falls_back_to_main_session(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_mode(self.thread_key, server.SESSION_MODE_CONTROL)
        self.store.set_pending_subagent_target(
            self.thread_key,
            thread_id="sub-404",
            agent_nickname="Atlas",
            agent_role="explorer",
            owner_user_id=self.user_id,
            session_id=self.session_id,
        )
        result = server.CodexRunResult(
            session_id=self.session_id,
            text="main done",
            exit_code=0,
            raw_output="",
            final_output="main done",
            json_output="",
            cleaned_output="main done",
            timed_out=False,
        )

        with patch.object(server, "find_subagent_for_main_session", return_value=None):
            with patch.object(server, "run_runtime_turn_with_updates", return_value=result) as run_runtime_turn:
                server.process_prompt(self.client, self.channel, self.thread_ts, "continue", self.user_id)

        self.assertEqual(run_runtime_turn.call_args.kwargs["session_id"], self.session_id)
        self.assertIn("已取消这次 subagent 单次路由", self.client.messages[0]["text"])
        self.assertIsNone(
            self.store.get_pending_subagent_target(
                self.thread_key,
                current_session_id=self.session_id,
                owner_user_id=self.user_id,
            )
        )

    def test_control_command_clears_pending_subagent_target(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_mode(self.thread_key, server.SESSION_MODE_OBSERVE)
        self.store.set_pending_subagent_target(
            self.thread_key,
            thread_id="sub-1",
            agent_nickname="Atlas",
            agent_role="explorer",
            owner_user_id=self.user_id,
            session_id=self.session_id,
        )

        server.process_prompt(self.client, self.channel, self.thread_ts, "control", self.user_id)

        self.assertIsNone(
            self.store.get_pending_subagent_target(
                self.thread_key,
                current_session_id=self.session_id,
                owner_user_id=self.user_id,
            )
        )

    def test_observe_command_clears_pending_subagent_target(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_mode(self.thread_key, server.SESSION_MODE_CONTROL)
        self.store.set_pending_subagent_target(
            self.thread_key,
            thread_id="sub-1",
            agent_nickname="Atlas",
            agent_role="explorer",
            owner_user_id=self.user_id,
            session_id=self.session_id,
        )

        server.process_prompt(self.client, self.channel, self.thread_ts, "observe", self.user_id)

        self.assertIsNone(
            self.store.get_pending_subagent_target(
                self.thread_key,
                current_session_id=self.session_id,
                owner_user_id=self.user_id,
            )
        )

    def test_reset_command_clears_pending_subagent_target(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_pending_subagent_target(
            self.thread_key,
            thread_id="sub-1",
            agent_nickname="Atlas",
            agent_role="explorer",
            owner_user_id=self.user_id,
            session_id=self.session_id,
        )

        server.process_prompt(self.client, self.channel, self.thread_ts, "reset", self.user_id)

        self.assertIsNone(self.store.get(self.thread_key))
        self.assertIsNone(
            self.store.get_pending_subagent_target(
                self.thread_key,
                current_session_id=self.session_id,
                owner_user_id=self.user_id,
            )
        )

    def test_fresh_command_clears_pending_subagent_target(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_pending_subagent_target(
            self.thread_key,
            thread_id="sub-1",
            agent_nickname="Atlas",
            agent_role="explorer",
            owner_user_id=self.user_id,
            session_id=self.session_id,
        )
        result = server.CodexRunResult(
            session_id="sess-fresh",
            text="fresh done",
            exit_code=0,
            raw_output="",
            final_output="fresh done",
            json_output="",
            cleaned_output="fresh done",
            timed_out=False,
        )

        with patch.object(server, "run_runtime_turn_with_updates", return_value=result):
            server.process_prompt(self.client, self.channel, self.thread_ts, "fresh new task", self.user_id)

        self.assertIsNone(
            self.store.get_pending_subagent_target(
                self.thread_key,
                current_session_id="sess-fresh",
                owner_user_id=self.user_id,
            )
        )

    def test_handle_subagent_send_next_action_blocks_in_observe_mode(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_mode(self.thread_key, server.SESSION_MODE_OBSERVE)

        server.handle_subagent_send_next_action(
            self.client,
            MagicMock(),
            thread_key=self.thread_key,
            session_id=self.session_id,
            subagent_thread_id="sub-1",
            user_id=self.user_id,
            channel_id=self.channel,
            thread_ts=self.thread_ts,
        )

        self.assertIn("observe", self.client.messages[-1]["text"])
        self.assertIsNone(
            self.store.get_pending_subagent_target(
                self.thread_key,
                current_session_id=self.session_id,
                owner_user_id=self.user_id,
            )
        )

    def test_handle_subagent_send_cancel_action_clears_pending_target(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_pending_subagent_target(
            self.thread_key,
            thread_id="sub-1",
            agent_nickname="Atlas",
            agent_role="explorer",
            owner_user_id=self.user_id,
            session_id=self.session_id,
        )

        server.handle_subagent_send_cancel_action(
            self.client,
            thread_key=self.thread_key,
            channel_id=self.channel,
            thread_ts=self.thread_ts,
        )

        self.assertIn("已取消这次 subagent 单次路由", self.client.messages[-1]["text"])
        self.assertIsNone(
            self.store.get_pending_subagent_target(
                self.thread_key,
                current_session_id=self.session_id,
                owner_user_id=self.user_id,
            )
        )

    def test_handle_subagent_observe_action_attaches_subagent_in_observe_mode(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)

        with patch.object(server, "attach_thread_to_session") as attach_thread_to_session:
            server.handle_subagent_observe_action(
                self.client,
                thread_key=self.thread_key,
                session_id=self.session_id,
                subagent_thread_id="sub-1",
                user_id=self.user_id,
                channel_id=self.channel,
                thread_ts=self.thread_ts,
            )

        attach_thread_to_session.assert_called_once_with(
            self.client,
            self.channel,
            self.thread_ts,
            self.thread_key,
            session_id="sub-1",
            user_id=self.user_id,
            mode=server.SESSION_MODE_OBSERVE,
            include_bootstrap=True,
        )

    def test_handle_subagent_attach_action_rejects_stale_main_session(self):
        self.store.set(self.thread_key, "sess-new", owner_user_id=self.user_id)

        with patch.object(server, "attach_thread_to_session") as attach_thread_to_session:
            server.handle_subagent_attach_action(
                self.client,
                thread_key=self.thread_key,
                session_id=self.session_id,
                subagent_thread_id="sub-1",
                user_id=self.user_id,
                channel_id=self.channel,
                thread_ts=self.thread_ts,
            )

        attach_thread_to_session.assert_not_called()
        self.assertIn("当前主 session 已变化", self.client.messages[-1]["text"])

    def test_handoff_rebuild_posts_pending_subagent_target_notice(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_mode(self.thread_key, server.SESSION_MODE_CONTROL)
        self.store.set_pending_subagent_target(
            self.thread_key,
            thread_id="sub-1",
            agent_nickname="Atlas",
            agent_role="explorer",
            owner_user_id=self.user_id,
            session_id=self.session_id,
        )
        result = server.CodexRunResult(
            session_id="sess-rebuilt",
            text="handoff body",
            exit_code=0,
            raw_output="",
            final_output="handoff body",
            json_output="",
            cleaned_output="handoff body",
            timed_out=False,
        )

        with patch.object(server, "run_runtime_turn_with_updates", return_value=result):
            with patch.object(server, "append_handoff_footer", return_value="handoff body"):
                server.process_prompt(self.client, self.channel, self.thread_ts, "handoff", self.user_id)

        self.assertTrue(
            any("挂起的 subagent 单次路由" in message["text"] for message in self.client.messages),
            self.client.messages,
        )
        self.assertIsNone(
            self.store.get_pending_subagent_target(
                self.thread_key,
                current_session_id="sess-rebuilt",
                owner_user_id=self.user_id,
            )
        )

    def test_mode_command_posts_collaboration_mode_card(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)

        server.process_prompt(self.client, self.channel, self.thread_ts, "mode", self.user_id)

        self.assertEqual(len(self.client.messages), 1)
        self.assertEqual(self.client.messages[0]["blocks"][0]["text"]["text"].splitlines()[0], "*Collaboration Mode*")

    def test_proposed_plan_response_posts_mode_card_after_result(self):
        result = server.CodexRunResult(
            session_id=self.session_id,
            text="<proposed_plan>\nhello\n</proposed_plan>",
            exit_code=0,
            raw_output="",
            final_output="<proposed_plan>\nhello\n</proposed_plan>",
            json_output="",
            cleaned_output="<proposed_plan>\nhello\n</proposed_plan>",
            timed_out=False,
        )

        with patch.object(server, "run_runtime_turn_with_updates", return_value=result):
            server.process_prompt(
                self.client,
                self.channel,
                self.thread_ts,
                "start a brand new task",
                self.user_id,
            )

        self.assertEqual(len(self.client.messages), 3)
        self.assertIn("<proposed_plan>", self.client.messages[1]["text"])
        self.assertIn("*Approved Plan*", self.client.messages[2]["blocks"][0]["text"]["text"])

    def test_plan_mode_prompt_requires_full_plan_only(self):
        prompt = server.build_plan_mode_prompt("请先给方案")

        self.assertIn("请直接输出完整 `<proposed_plan>...</proposed_plan>`", prompt)
        self.assertNotIn("<implementation_recommendation>", prompt)

    def test_process_prompt_in_plan_mode_strips_recommendation_only_tag_from_visible_output(self):
        self.store.set_collaboration_mode(
            self.thread_key,
            server.COLLABORATION_MODE_PLAN,
            owner_user_id=self.user_id,
        )
        result = server.CodexRunResult(
            session_id=self.session_id,
            text="这里先给一点口头说明。\n<implementation_recommendation>here</implementation_recommendation>",
            exit_code=0,
            raw_output="",
            final_output="这里先给一点口头说明。\n<implementation_recommendation>here</implementation_recommendation>",
            json_output="",
            cleaned_output="这里先给一点口头说明。\n<implementation_recommendation>here</implementation_recommendation>",
            timed_out=False,
        )

        with patch.object(server, "run_runtime_turn_with_updates", return_value=result):
            server.process_prompt(
                self.client,
                self.channel,
                self.thread_ts,
                "请更新方案",
                self.user_id,
            )

        posted_texts = [message.get("text", "") for message in self.client.messages]
        self.assertTrue(any("这里先给一点口头说明。" in text for text in posted_texts))
        self.assertFalse(any("<implementation_recommendation>" in text for text in posted_texts))
        self.assertFalse(any(message.get("blocks") for message in self.client.messages))

    def test_sanitize_plan_mode_response_strips_recommendation_when_plan_present(self):
        sanitized = server.sanitize_plan_mode_response_for_slack(
            "<proposed_plan>\nhello\n</proposed_plan>\n<implementation_recommendation>clean</implementation_recommendation>"
        )

        self.assertIn("<proposed_plan>", sanitized)
        self.assertNotIn("<implementation_recommendation>", sanitized)

    def test_build_thread_plan_actions_message_keeps_neutral_implementation_buttons_without_recommendation(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_latest_plan(
            self.thread_key,
            "<proposed_plan>\nhello\n</proposed_plan>",
            session_id=self.session_id,
            owner_user_id=self.user_id,
        )
        text, blocks = server.build_thread_plan_actions_message(self.thread_key, session_id=self.session_id)

        self.assertNotIn("Recommended execution:", text)
        button_by_id = {
            element["action_id"]: element
            for element in blocks[1]["elements"]
        }
        self.assertNotIn("style", button_by_id[server.THREAD_PLAN_IMPLEMENT_CLEAN_ACTION])
        self.assertNotIn("style", button_by_id[server.THREAD_PLAN_IMPLEMENT_HERE_ACTION])
        self.assertEqual(button_by_id[server.THREAD_PLAN_KEEP_PLANNING_ACTION]["text"]["text"], "Keep planning")

    def test_persist_latest_proposed_plan_does_not_store_agent_recommendation(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)

        plan_text = server.persist_latest_proposed_plan(
            self.thread_key,
            "<proposed_plan>\nhello\n</proposed_plan>\n<implementation_recommendation>clean</implementation_recommendation>",
            session_id=self.session_id,
            owner_user_id=self.user_id,
        )

        self.assertEqual(plan_text, "hello")
        self.assertIsNone(self.store.get_latest_plan_recommended_execution_mode(self.thread_key))
        self.assertIsNone(self.store.get_latest_plan_selected_action(self.thread_key))

    def test_build_thread_plan_actions_message_highlights_selected_action(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_latest_plan(
            self.thread_key,
            "<proposed_plan>\nhello\n</proposed_plan>",
            session_id=self.session_id,
            owner_user_id=self.user_id,
        )
        self.store.set_latest_plan_selected_action(self.thread_key, "here", owner_user_id=self.user_id)

        text, blocks = server.build_thread_plan_actions_message(self.thread_key, session_id=self.session_id)

        self.assertNotIn("Recommended execution:", text)
        button_by_id = {element["action_id"]: element for element in blocks[1]["elements"]}
        self.assertNotIn("style", button_by_id[server.THREAD_PLAN_IMPLEMENT_CLEAN_ACTION])
        self.assertEqual(button_by_id[server.THREAD_PLAN_IMPLEMENT_HERE_ACTION]["style"], "primary")
        self.assertNotIn("style", button_by_id[server.THREAD_PLAN_KEEP_PLANNING_ACTION])

    def test_persist_latest_proposed_plan_clears_previous_selected_action(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_latest_plan(
            self.thread_key,
            "<proposed_plan>\nold\n</proposed_plan>",
            session_id=self.session_id,
            owner_user_id=self.user_id,
        )
        self.store.set_latest_plan_selected_action(self.thread_key, "clean", owner_user_id=self.user_id)

        server.persist_latest_proposed_plan(
            self.thread_key,
            "<proposed_plan>\nnew\n</proposed_plan>\n<implementation_recommendation>here</implementation_recommendation>",
            session_id=self.session_id,
            owner_user_id=self.user_id,
        )

        self.assertIsNone(self.store.get_latest_plan_selected_action(self.thread_key))
        self.assertIsNone(self.store.get_latest_plan_recommended_execution_mode(self.thread_key))

    def test_continue_planning_action_runs_new_plan_turn_and_posts_updated_card(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_latest_plan(
            self.thread_key,
            "<proposed_plan>\nold plan\n</proposed_plan>",
            session_id=self.session_id,
            owner_user_id=self.user_id,
        )
        self.store.set_mode(self.thread_key, server.SESSION_MODE_CONTROL)
        self.store.set_collaboration_mode(self.thread_key, server.COLLABORATION_MODE_PLAN, owner_user_id=self.user_id)
        result = server.CodexRunResult(
            session_id=self.session_id,
            text="<proposed_plan>\nrefined plan\n</proposed_plan>",
            exit_code=0,
            raw_output="",
            final_output="<proposed_plan>\nrefined plan\n</proposed_plan>",
            json_output="",
            cleaned_output="<proposed_plan>\nrefined plan\n</proposed_plan>",
            timed_out=False,
        )
        with patch.object(server, "run_runtime_turn_with_updates", return_value=result) as run_runtime_turn:
            server.handle_keep_planning_action(
                self.client,
                self.channel,
                self.thread_ts,
                self.thread_key,
                user_id=self.user_id,
                logger=MagicMock(),
        )

        self.assertEqual(run_runtime_turn.call_args.kwargs["collaboration_mode"], server.COLLABORATION_MODE_PLAN)
        self.assertIn("请继续细化这份已批准方案", run_runtime_turn.call_args.args[4])
        self.assertNotIn("<implementation_recommendation>", run_runtime_turn.call_args.args[4])
        self.assertTrue(any("正在继续细化这份方案" in message["text"] for message in self.client.messages))
        self.assertTrue(any("已继续在当前 planning session 中细化方案" in message["text"] for message in self.client.messages))
        self.assertTrue(any(message.get("blocks") and "*Approved Plan*" in message["blocks"][0]["text"]["text"] for message in self.client.messages))

    def test_effort_command_sets_thread_override(self):
        self.store.set(
            self.thread_key,
            self.session_id,
            owner_user_id=self.user_id,
            session_origin=server.SESSION_ORIGIN_ATTACHED,
        )

        server.process_prompt(self.client, self.channel, self.thread_ts, "effort high", self.user_id)

        self.assertEqual(self.store.get_reasoning_effort(self.thread_key), "high")
        self.assertIn("已将当前 Slack thread 的 reasoning effort 设为 `high`", self.client.messages[0]["text"])

    def test_effort_reset_on_attached_session_restores_inherited_behavior(self):
        self.store.set_reasoning_effort(self.thread_key, "medium", owner_user_id=self.user_id)
        self.store.set(
            self.thread_key,
            self.session_id,
            owner_user_id=self.user_id,
            session_origin=server.SESSION_ORIGIN_ATTACHED,
        )

        server.process_prompt(self.client, self.channel, self.thread_ts, "effort reset", self.user_id)

        self.assertIsNone(self.store.get_reasoning_effort(self.thread_key))
        self.assertIn("会继续继承原 session 的 effort 设置", self.client.messages[0]["text"])

    def test_fresh_effort_sets_override_and_uses_it_for_new_slack_session(self):
        result = server.CodexRunResult(
            session_id=self.session_id,
            text="done",
            exit_code=0,
            raw_output="",
            final_output="done",
            json_output="",
            cleaned_output="done",
            timed_out=False,
        )

        with patch.object(
            server,
            "run_runtime_turn_with_updates",
            return_value=result,
        ) as run_runtime_turn_with_updates:
            server.process_prompt(
                self.client,
                self.channel,
                self.thread_ts,
                "fresh --effort high fix the flaky test",
                self.user_id,
        )

        self.assertEqual(self.store.get_reasoning_effort(self.thread_key), "high")
        self.assertEqual(self.store.get_session_origin(self.thread_key), server.SESSION_ORIGIN_SLACK)
        self.assertEqual(run_runtime_turn_with_updates.call_args.kwargs["reasoning_effort"], "high")
        self.assertIsNone(run_runtime_turn_with_updates.call_args.kwargs["session_id"])

    def test_new_slack_session_uses_runtime_path(self):
        result = server.CodexRunResult(
            session_id=self.session_id,
            text="done",
            exit_code=0,
            raw_output="",
            final_output="done",
            json_output="",
            cleaned_output="done",
            timed_out=False,
        )

        with patch.object(server, "run_runtime_turn_with_updates", return_value=result) as run_runtime_turn_with_updates:
            with patch.object(server, "run_codex_with_updates") as run_codex_with_updates:
                server.process_prompt(
                    self.client,
                    self.channel,
                    self.thread_ts,
                    "start a brand new task",
                    self.user_id,
                )

        run_runtime_turn_with_updates.assert_called_once()
        run_codex_with_updates.assert_not_called()

    def test_attached_session_without_override_preserves_inherited_effort(self):
        self.store.set(
            self.thread_key,
            self.session_id,
            owner_user_id=self.user_id,
            session_origin=server.SESSION_ORIGIN_ATTACHED,
            session_cwd="/tmp/original-project",
        )
        self.store.set_mode(self.thread_key, server.SESSION_MODE_CONTROL)
        result = server.CodexRunResult(
            session_id=self.session_id,
            text="done",
            exit_code=0,
            raw_output="",
            final_output="done",
            json_output="",
            cleaned_output="done",
            timed_out=False,
        )

        with patch.dict(server.ENV, {"CODEX_REASONING_EFFORT": "xhigh"}, clear=False):
            with patch.object(server, "read_thread_cwd", return_value="/tmp/original-project"):
                with patch.object(
                    server,
                    "run_runtime_turn_with_updates",
                    return_value=result,
                ) as run_runtime_turn_with_updates:
                    server.process_prompt(self.client, self.channel, self.thread_ts, "continue", self.user_id)

        self.assertIsNone(run_runtime_turn_with_updates.call_args.kwargs["reasoning_effort"])
        self.assertEqual(run_runtime_turn_with_updates.call_args.kwargs["workdir_override"], "/tmp/original-project")

    def test_controlled_existing_session_uses_runtime_path_instead_of_cli_resume(self):
        self.store.set(
            self.thread_key,
            self.session_id,
            owner_user_id=self.user_id,
            session_origin=server.SESSION_ORIGIN_ATTACHED,
            session_cwd="/tmp/original-project",
        )
        self.store.set_mode(self.thread_key, server.SESSION_MODE_CONTROL)
        result = server.CodexRunResult(
            session_id=self.session_id,
            text="done",
            exit_code=0,
            raw_output="",
            final_output="done",
            json_output="",
            cleaned_output="done",
            timed_out=False,
        )

        with patch.object(server, "read_thread_cwd", return_value="/tmp/original-project"):
            with patch.object(server, "run_runtime_turn_with_updates", return_value=result) as run_runtime_turn_with_updates:
                with patch.object(server, "run_codex_with_updates") as run_codex_with_updates:
                    server.process_prompt(self.client, self.channel, self.thread_ts, "continue", self.user_id)

        run_runtime_turn_with_updates.assert_called_once()
        run_codex_with_updates.assert_not_called()

    def test_resume_rebuild_preserves_effort_override_and_switches_origin_to_slack(self):
        original_result = server.CodexRunResult(
            session_id=self.session_id,
            text="session not found",
            exit_code=1,
            raw_output="session not found",
            final_output="",
            json_output="",
            cleaned_output="session not found",
            timed_out=False,
        )
        rebuilt_result = server.CodexRunResult(
            session_id="019d5868-71ba-7101-9143-81867f3db5c0",
            text="rebuilt",
            exit_code=0,
            raw_output="",
            final_output="rebuilt",
            json_output="",
            cleaned_output="rebuilt",
            timed_out=False,
        )
        self.store.set_reasoning_effort(self.thread_key, "high", owner_user_id=self.user_id)
        self.store.set(
            self.thread_key,
            self.session_id,
            owner_user_id=self.user_id,
            session_origin=server.SESSION_ORIGIN_ATTACHED,
            session_cwd="/tmp/original-project",
        )
        self.store.set_mode(self.thread_key, server.SESSION_MODE_CONTROL)

        with patch.object(server, "read_thread_cwd", return_value="/tmp/original-project"):
            with patch.object(
                server,
                "run_runtime_turn_with_updates",
                side_effect=[original_result, rebuilt_result],
            ) as run_runtime_turn_with_updates:
                server.process_prompt(self.client, self.channel, self.thread_ts, "continue", self.user_id)

        self.assertEqual(run_runtime_turn_with_updates.call_args_list[0].kwargs["reasoning_effort"], "high")
        self.assertEqual(run_runtime_turn_with_updates.call_args_list[1].kwargs["reasoning_effort"], "high")
        self.assertEqual(run_runtime_turn_with_updates.call_args_list[0].kwargs["workdir_override"], "/tmp/original-project")
        self.assertEqual(run_runtime_turn_with_updates.call_args_list[1].kwargs["workdir_override"], "/tmp/original-project")
        self.assertEqual(self.store.get(self.thread_key), "019d5868-71ba-7101-9143-81867f3db5c0")
        self.assertEqual(self.store.get_reasoning_effort(self.thread_key), "high")
        self.assertEqual(self.store.get_session_origin(self.thread_key), server.SESSION_ORIGIN_SLACK)
        self.assertEqual(self.store.get_session_cwd(self.thread_key), "/tmp/original-project")

    def test_unwatch_reports_when_watcher_is_stopped(self):
        with patch.object(server, "stop_watcher", return_value=True):
            server.process_prompt(self.client, self.channel, self.thread_ts, "unwatch", self.user_id)
        self.assertIn("已停止当前 Slack thread 的持续 watch", self.client.messages[0]["text"])

    def test_observe_mode_mentions_existing_watch(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        with patch.object(server, "get_watcher", return_value=object()):
            server.process_prompt(self.client, self.channel, self.thread_ts, "observe", self.user_id)
        text = self.client.messages[0]["text"]
        self.assertIn("已切到 `observe` 模式", text)
        self.assertIn("`watch` 仍在运行", text)
        self.assertIn("`unwatch` 或 `stop watch`", text)

    def test_control_mode_stops_existing_watch(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        with patch.object(server, "stop_watcher", return_value=True) as stop_watcher:
            server.process_prompt(self.client, self.channel, self.thread_ts, "control", self.user_id)
        stop_watcher.assert_called_once_with(self.thread_key)
        text = self.client.messages[0]["text"]
        self.assertIn("已切到 `control` 模式", text)
        self.assertIn("已自动停止当前 Slack thread 的 `watch`", text)

    def test_watch_is_blocked_in_control_mode(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_mode(self.thread_key, server.SESSION_MODE_CONTROL)
        server.process_prompt(self.client, self.channel, self.thread_ts, "watch", self.user_id)
        text = self.client.messages[0]["text"]
        self.assertIn("已处于 `control` 模式", text)
        self.assertIn("为避免重复消息", text)

    def test_observe_mode_blocks_normal_resume(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_mode(self.thread_key, server.SESSION_MODE_OBSERVE)

        with patch.object(server, "run_codex") as run_codex:
            server.process_prompt(self.client, self.channel, self.thread_ts, "continue", self.user_id)

        run_codex.assert_not_called()
        self.assertIn("处于 `observe` 模式", self.client.messages[0]["text"])

    def test_runtime_timeout_is_reported_to_slack_and_resets_runtime(self):
        self.store.set(
            self.thread_key,
            self.session_id,
            owner_user_id=self.user_id,
            session_origin=server.SESSION_ORIGIN_ATTACHED,
            session_cwd="/tmp/original-project",
        )
        self.store.set_mode(self.thread_key, server.SESSION_MODE_CONTROL)
        fake_runtime = ns(reset=MagicMock())

        with patch.object(server, "get_app_runtime", return_value=fake_runtime) as get_app_runtime:
            with patch.object(server, "read_thread_cwd", return_value="/tmp/original-project"):
                with patch.object(
                    server,
                    "run_runtime_turn_with_updates",
                    side_effect=CodexTimeoutError(
                        "request timed out for method='thread/resume' after 90.0s"
                    ),
                ):
                    server.process_prompt(self.client, self.channel, self.thread_ts, "continue", self.user_id)

        get_app_runtime.assert_called_once()
        fake_runtime.reset.assert_called_once()
        self.assertIn("服务仍在运行", self.client.messages[-1]["text"])
        self.assertIn("CodexTimeoutError", self.client.messages[-1]["text"])
        self.assertIn("thread/resume", self.client.messages[-1]["text"])


if __name__ == "__main__":
    unittest.main()
