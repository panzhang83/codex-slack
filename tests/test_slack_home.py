import unittest
from unittest.mock import patch

import server
import slack_home
from codex_threads import ThreadSummary


class DummyViewClient:
    def __init__(self):
        self.calls = []

    def views_publish(self, **kwargs):
        self.calls.append(kwargs)


class SlackHomeViewTests(unittest.TestCase):
    def test_format_binding_summary_rows_empty(self):
        text = slack_home.format_binding_summary_rows([])
        self.assertIn("No bindings yet", text)

    def test_format_recent_sessions_rows_empty(self):
        text = slack_home.format_recent_sessions_rows([])
        self.assertIn("No recent sessions found", text)

    def test_build_home_view_contains_refresh_action(self):
        view = slack_home.build_home_view(
            default_workdir="/tmp/project",
            default_model="gpt-5.4",
            default_effort="xhigh",
            bindings_summary="binding summary",
            recent_sessions_summary="recent summary",
            help_text="help",
        )
        self.assertEqual(view["type"], "home")
        action_ids = [
            element.get("action_id")
            for block in view["blocks"]
            if block.get("type") == "actions"
            for element in block.get("elements", [])
        ]
        self.assertIn("home_refresh", action_ids)


class ServerAppHomeHelpersTests(unittest.TestCase):
    def test_get_home_recent_sessions_rows_success(self):
        summaries = [
            ThreadSummary(
                thread_id="thr_1",
                preview="Fix test",
                cwd="/repo",
                updated_at=1700000000,
                created_at=1690000000,
                status_type="idle",
                source="cli",
                name="test thread",
            )
        ]
        with patch.object(server.thread_views, "list_threads", return_value={"data": []}):
            with patch.object(server.thread_views, "extract_thread_summaries", return_value=summaries):
                rows = server.get_home_recent_sessions_rows(limit=5)
        self.assertEqual(rows[0]["thread_id"], "thr_1")
        self.assertEqual(rows[0]["status"], "idle")
        self.assertEqual(rows[0]["cwd"], "/repo")

    def test_get_home_recent_sessions_rows_error(self):
        with patch.object(server.thread_views, "list_threads", side_effect=RuntimeError("boom")):
            rows = server.get_home_recent_sessions_rows(limit=5)
        self.assertEqual(rows[0]["thread_id"], "-")
        self.assertIn("boom", rows[0]["title"])

    def test_publish_home_view_calls_views_publish(self):
        client = DummyViewClient()
        with patch.object(server, "get_home_bindings_rows", return_value=[]):
            with patch.object(server, "get_home_recent_sessions_rows", return_value=[]):
                with patch.object(server, "get_codex_settings", return_value=("codex", "gpt-5.4", "/repo", 900, "workspace-write", "", False)):
                    with patch.object(server, "get_default_reasoning_effort", return_value="xhigh"):
                        server.publish_home_view(client, "U123")

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["user_id"], "U123")
        self.assertEqual(client.calls[0]["view"]["type"], "home")
        text_blocks = []
        for block in client.calls[0]["view"]["blocks"]:
            if block.get("type") == "section":
                text_blocks.append(block.get("text", {}).get("text", ""))
            if block.get("type") == "context":
                for element in block.get("elements", []):
                    text_blocks.append(element.get("text", ""))
        joined = "\n".join(text_blocks)
        self.assertIn("workspace-write", joined)
        self.assertIn("full_auto=`0`", joined)


if __name__ == "__main__":
    unittest.main()
