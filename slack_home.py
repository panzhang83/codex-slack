from typing import Optional


def build_home_view(
    *,
    default_workdir: str,
    default_model: str,
    default_effort: str,
    bindings_summary: str,
    recent_sessions_summary: str,
    help_text: Optional[str] = None,
):
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "codex-slack"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Model*\n`{default_model}`"},
                {"type": "mrkdwn", "text": f"*Effort*\n`{default_effort}`"},
                {"type": "mrkdwn", "text": f"*Default Workdir*\n`{default_workdir}`"},
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Your Slack thread bindings*\n{bindings_summary}"},
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Recent Codex sessions*\n{recent_sessions_summary}"},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "home_refresh",
                    "text": {"type": "plain_text", "text": "Refresh"},
                    "value": "refresh",
                }
            ],
        },
    ]
    if help_text:
        blocks.extend(
            [
                {"type": "divider"},
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": help_text}],
                },
            ]
        )
    return {
        "type": "home",
        "blocks": blocks,
    }
