import textwrap
import unittest
from unittest.mock import patch

import server


class CommandParsingTests(unittest.TestCase):
    def test_fresh_command_supports_slash_and_plain_variants(self):
        self.assertTrue(server.is_fresh_command("/fresh summarize status"))
        self.assertTrue(server.is_fresh_command("fresh summarize status"))
        self.assertTrue(server.is_fresh_command("/fresh"))
        self.assertTrue(server.is_fresh_command("fresh"))
        self.assertFalse(server.is_fresh_command("freshness check"))

    def test_strip_fresh_command_returns_payload(self):
        self.assertEqual(server.strip_fresh_command("/fresh do the task"), "do the task")
        self.assertEqual(server.strip_fresh_command("fresh do the task"), "do the task")
        self.assertEqual(server.strip_fresh_command("/fresh"), "")
        self.assertEqual(server.strip_fresh_command("fresh"), "")

    def test_attach_command_supports_slash_and_plain_variants(self):
        self.assertTrue(server.is_attach_command("/attach 019-test"))
        self.assertTrue(server.is_attach_command("attach 019-test"))
        self.assertEqual(server.strip_attach_command("/attach 019-test"), "019-test")
        self.assertEqual(server.strip_attach_command("attach 019-test"), "019-test")
        self.assertEqual(server.strip_attach_command("attach "), "")

    def test_status_and_session_commands_support_plain_text(self):
        self.assertTrue(server.is_status_command("/where"))
        self.assertTrue(server.is_status_command("whoami"))
        self.assertTrue(server.is_status_command("status"))
        self.assertTrue(server.is_session_command("/session"))
        self.assertTrue(server.is_session_command("session"))
        self.assertTrue(server.is_session_command("session id"))
        self.assertFalse(server.is_status_command("where are you going"))


class SlackAccessTests(unittest.TestCase):
    def test_allowed_user_ids_support_commas_and_whitespace(self):
        with patch.dict(
            server.ENV,
            {"ALLOWED_SLACK_USER_IDS": "U111, U222\nU333\tU444"},
            clear=False,
        ):
            self.assertEqual(
                server.get_allowed_slack_user_ids(),
                {"U111", "U222", "U333", "U444"},
            )

    def test_blank_allowlist_means_unrestricted(self):
        with patch.dict(server.ENV, {"ALLOWED_SLACK_USER_IDS": ""}, clear=False):
            self.assertTrue(server.is_allowed_slack_user("U111"))

    def test_allowlist_restricts_unknown_user(self):
        with patch.dict(server.ENV, {"ALLOWED_SLACK_USER_IDS": "U111,U222"}, clear=False):
            self.assertTrue(server.is_allowed_slack_user("U111"))
            self.assertFalse(server.is_allowed_slack_user("U999"))


class FormattingTests(unittest.TestCase):
    def test_handoff_footer_includes_terminal_verification(self):
        text = server.append_handoff_footer("Current Goal:\nkeep context", "019-test", "/tmp/workdir")
        self.assertIn("Terminal Verify Command:", text)
        self.assertIn("`printenv CODEX_THREAD_ID && pwd`", text)
        self.assertIn("Expected Session ID: `019-test`", text)
        self.assertIn("Expected Workdir: `/tmp/workdir`", text)

    def test_recap_footer_includes_current_session_id(self):
        text = server.append_recap_footer("Recent Progress:\nupdated docs", "019-test")
        self.assertIn("Current Session ID: `019-test`", text)

    def test_clean_codex_output_filters_progress_noise(self):
        raw = textwrap.dedent(
            """
            thinking about the plan
            running tests
            useful line

            commentary hidden
            final answer
            """
        ).strip()
        self.assertEqual(server.clean_codex_output(raw), "useful line\n\nfinal answer")


class JsonEventParsingTests(unittest.TestCase):
    def test_parse_codex_json_events_extracts_session_and_messages(self):
        raw = "\n".join(
            [
                '{"type":"thread.started","thread_id":"019-test"}',
                '{"type":"item.completed","item":{"type":"agent_message","text":"first reply"}}',
                '{"type":"item.completed","item":{"type":"tool_result","text":"ignored"}}',
                '{"type":"item.completed","item":{"type":"agent_message","text":"second reply"}}',
            ]
        )
        session_id, message = server.parse_codex_json_events(raw)
        self.assertEqual(session_id, "019-test")
        self.assertEqual(message, "first reply\n\nsecond reply")

    def test_parse_codex_json_events_ignores_invalid_lines(self):
        raw = "\n".join(
            [
                "not json",
                '{"type":"thread.started","thread_id":"019-test"}',
                '{"type":"item.completed","item":{"type":"agent_message","text":"reply"}}',
            ]
        )
        session_id, message = server.parse_codex_json_events(raw)
        self.assertEqual(session_id, "019-test")
        self.assertEqual(message, "reply")


if __name__ == "__main__":
    unittest.main()
