from typing import Optional


def format_binding_summary_rows(rows):
    if not rows:
        return "_No bindings yet._\nUse `fresh ...` or `attach <session_id>` in a Slack thread."

    lines = []
    for index, row in enumerate(rows, start=1):
        lines.append(
            f"{index}. `{row.get('session_id', '-')}` | mode=`{row.get('mode', '-')}` | "
            f"cwd=`{row.get('cwd', '-')}` | updated=`{row.get('updated_at', '-')}`"
        )
    return "\n".join(lines)


def format_recent_sessions_rows(rows):
    if not rows:
        return "_No recent sessions found._\nStart one with `fresh ...` in DM or `@bot ...` in a channel."

    lines = []
    for index, row in enumerate(rows, start=1):
        lines.append(
            f"{index}. `{row.get('thread_id', '-')}` | {row.get('title', '(untitled)')} | "
            f"cwd=`{row.get('cwd', '-')}` | status=`{row.get('status', '-')}`"
        )
    return "\n".join(lines)


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
