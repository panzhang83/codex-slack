import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import server


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

    def chat_postMessage(self, **kwargs):
        self.messages.append(kwargs)
        return {"ok": True, "ts": str(len(self.messages))}


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
        self.assertTrue(server.is_status_command("whoami"))
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


class CodexHelperTests(unittest.TestCase):
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


class ProgressExtractionTests(unittest.TestCase):
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
                        with patch.object(server, "run_codex_with_updates", return_value=result) as run_codex_with_updates:
                            server.process_prompt(
                                self.client,
                                self.channel,
                                self.thread_ts,
                                "",
                                self.user_id,
                                slack_event_payload={"event": {"files": [{"id": "F1"}]}},
                            )

        self.assertEqual(run_codex_with_updates.call_args.kwargs["image_paths"], [image_path])
        self.assertEqual(run_codex_with_updates.call_args.args[3], server.DEFAULT_IMAGE_ONLY_PROMPT)
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

        with patch.object(server, "run_codex_with_updates", return_value=result) as run_codex_with_updates:
            server.process_prompt(
                self.client,
                self.channel,
                self.thread_ts,
                "fresh --effort high fix the flaky test",
                self.user_id,
            )

        self.assertEqual(self.store.get_reasoning_effort(self.thread_key), "high")
        self.assertEqual(self.store.get_session_origin(self.thread_key), server.SESSION_ORIGIN_SLACK)
        self.assertEqual(run_codex_with_updates.call_args.kwargs["reasoning_effort"], "high")
        self.assertIsNone(run_codex_with_updates.call_args.kwargs["session_id"])

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
                with patch.object(server, "run_codex_with_updates", return_value=result) as run_codex_with_updates:
                    server.process_prompt(self.client, self.channel, self.thread_ts, "continue", self.user_id)

        self.assertIsNone(run_codex_with_updates.call_args.kwargs["reasoning_effort"])
        self.assertEqual(run_codex_with_updates.call_args.kwargs["workdir_override"], "/tmp/original-project")

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
                "run_codex_with_updates",
                side_effect=[original_result, rebuilt_result],
            ) as run_codex_with_updates:
                server.process_prompt(self.client, self.channel, self.thread_ts, "continue", self.user_id)

        self.assertEqual(run_codex_with_updates.call_args_list[0].kwargs["reasoning_effort"], "high")
        self.assertEqual(run_codex_with_updates.call_args_list[1].kwargs["reasoning_effort"], "high")
        self.assertEqual(run_codex_with_updates.call_args_list[0].kwargs["workdir_override"], "/tmp/original-project")
        self.assertEqual(run_codex_with_updates.call_args_list[1].kwargs["workdir_override"], "/tmp/original-project")
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

    def test_watch_loop_incremental_push_uses_plain_dialogue_without_update_heading(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        events = [
            server.ConversationEvent("turn-0", "u0", "user", "previous"),
            server.ConversationEvent("turn-0", "a0", "assistant", "done previous"),
            server.ConversationEvent("turn-1", "u1", "user", "hello"),
            server.ConversationEvent("turn-1", "a1", "assistant", "done"),
        ]
        calls = {"count": 0}

        class FakeStopEvent:
            def wait(self, _seconds):
                calls["count"] += 1
                return calls["count"] > 1

        with patch.object(server, "get_watch_poll_seconds", return_value=1):
            with patch.object(server, "read_conversation_events", return_value=events):
                with patch.object(server, "post_chunks") as post_chunks:
                    with patch.object(server, "get_watcher", return_value=None):
                        server.watch_loop(
                            self.client,
                            self.channel,
                            self.thread_ts,
                            self.thread_key,
                            self.session_id,
                            FakeStopEvent(),
                            last_event_key=("turn-0", "a0"),
                        )

        post_chunks.assert_called_once()
        self.assertEqual(post_chunks.call_args.args[3], "*User*\n> hello\n\n*Codex*\n> done")

    def test_watch_loop_stops_when_anchor_event_is_missing(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        events = [
            server.ConversationEvent("turn-2", "u2", "user", "next"),
            server.ConversationEvent("turn-2", "a2", "assistant", "done"),
        ]

        class FakeStopEvent:
            def wait(self, _seconds):
                return False

        with patch.object(server, "get_watch_poll_seconds", return_value=1):
            with patch.object(server, "read_conversation_events", return_value=events):
                with patch.object(server, "post_chunks") as post_chunks:
                    with patch.object(server, "get_watcher", return_value=None):
                        server.watch_loop(
                            self.client,
                            self.channel,
                            self.thread_ts,
                            self.thread_key,
                            self.session_id,
                            FakeStopEvent(),
                            last_event_key=("missing", "key"),
                        )

        post_chunks.assert_called_once()
        self.assertIn("请重新发送 `watch`", post_chunks.call_args.args[3])

    def test_observe_mode_blocks_normal_resume(self):
        self.store.set(self.thread_key, self.session_id, owner_user_id=self.user_id)
        self.store.set_mode(self.thread_key, server.SESSION_MODE_OBSERVE)

        with patch.object(server, "run_codex") as run_codex:
            server.process_prompt(self.client, self.channel, self.thread_ts, "continue", self.user_id)

        run_codex.assert_not_called()
        self.assertIn("处于 `observe` 模式", self.client.messages[0]["text"])


if __name__ == "__main__":
    unittest.main()
