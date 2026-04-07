from typing import Mapping, Optional, Sequence


def _as_text(value, default="-"):
    normalized = str(value or "").strip()
    return normalized or default


def _as_optional_text(value):
    normalized = str(value or "").strip()
    return normalized or None


def _as_rows(rows):
    if not rows:
        return []
    normalized_rows = []
    for row in rows:
        if isinstance(row, Mapping):
            normalized_rows.append(dict(row))
    return normalized_rows


def _binding_row_text(row, index):
    label = _as_text(row.get("label"), default=f"Binding {index}")
    session_id = _as_text(row.get("session_id"))
    mode = _as_text(row.get("mode"))
    cwd = _as_text(row.get("cwd"))
    updated_at = _as_text(row.get("updated_at"))
    status_text = _as_optional_text(row.get("status_text"))
    lines = [
        f"*{index}. {label}*",
        f"`{session_id}` | mode=`{mode}`",
        f"cwd=`{cwd}` | updated=`{updated_at}`",
    ]
    if status_text:
        lines.append(f"_{status_text}_")
    return "\n".join(lines)


def _recent_row_text(row, index):
    label = _as_text(row.get("label"), default=f"Session {index}")
    thread_id = _as_text(row.get("thread_id"))
    title = _as_text(row.get("title"), default="(untitled)")
    cwd = _as_text(row.get("cwd"))
    status = _as_text(row.get("status"))
    status_text = _as_optional_text(row.get("status_text"))
    lines = [
        f"*{index}. {label}*",
        f"`{thread_id}` | {title}",
        f"cwd=`{cwd}` | status=`{status}`",
    ]
    if status_text:
        lines.append(f"_{status_text}_")
    return "\n".join(lines)


def _build_row_section(text, row):
    section = {
        "type": "section",
        "text": {"type": "mrkdwn", "text": text},
    }
    action_id = _as_optional_text(row.get("action_id"))
    action_value = _as_optional_text(row.get("action_value"))
    if action_id and action_value:
        section["accessory"] = {
            "type": "button",
            "action_id": action_id,
            "text": {"type": "plain_text", "text": _as_text(row.get("action_text"), default="Action")},
            "value": action_value,
        }
    return section


def _append_rich_rows(blocks, *, title, rows, row_renderer, empty_text):
    blocks.append({"type": "header", "text": {"type": "plain_text", "text": title}})
    if not rows:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": empty_text}})
        return
    for index, row in enumerate(rows, start=1):
        blocks.append(_build_row_section(row_renderer(row, index), row))


def _append_legacy_summary(blocks, *, title, summary):
    blocks.append({"type": "header", "text": {"type": "plain_text", "text": title}})
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _as_text(summary, default="-")}})


def format_binding_summary_rows(rows):
    normalized_rows = _as_rows(rows)
    if not normalized_rows:
        return "_No bindings yet._\nUse `fresh ...` or `attach <session_id>` in a Slack thread."

    lines = []
    for index, row in enumerate(normalized_rows, start=1):
        lines.append(_binding_row_text(row, index))
    return "\n".join(lines)


def format_recent_sessions_rows(rows):
    normalized_rows = _as_rows(rows)
    if not normalized_rows:
        return "_No recent sessions found._\nStart one with `fresh ...` in DM or `@bot ...` in a channel."

    lines = []
    for index, row in enumerate(normalized_rows, start=1):
        lines.append(_recent_row_text(row, index))
    return "\n".join(lines)


def build_home_view(
    *,
    default_workdir: str,
    default_model: str,
    default_effort: str,
    bindings_summary: str,
    recent_sessions_summary: str,
    help_text: Optional[str] = None,
    bindings_rows: Optional[Sequence[Mapping[str, object]]] = None,
    recent_sessions_rows: Optional[Sequence[Mapping[str, object]]] = None,
    quick_hints: Optional[Sequence[str]] = None,
):
    normalized_bindings_rows = _as_rows(bindings_rows)
    normalized_recent_rows = _as_rows(recent_sessions_rows)
    hint_lines = [line for line in (quick_hints or []) if _as_optional_text(line)]
    hint_text = _as_optional_text(help_text)
    subtitle_lines = [
        "*Operator Dashboard*",
        "Use Home for quick visibility and to manage your Slack thread bindings.",
    ]
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "codex-slack"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(subtitle_lines)},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Model*\n`{_as_text(default_model)}`"},
                {"type": "mrkdwn", "text": f"*Effort*\n`{_as_text(default_effort)}`"},
                {"type": "mrkdwn", "text": f"*Default Workdir*\n`{_as_text(default_workdir)}`"},
            ],
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
        {"type": "divider"},
    ]

    if bindings_rows is not None:
        _append_rich_rows(
            blocks,
            title="Your Slack Thread Bindings",
            rows=normalized_bindings_rows,
            row_renderer=_binding_row_text,
            empty_text="_No bindings yet._\nUse `fresh ...` or `attach <session_id>` in a Slack thread.",
        )
    else:
        _append_legacy_summary(
            blocks,
            title="Your Slack Thread Bindings",
            summary=bindings_summary,
        )

    blocks.append({"type": "divider"})

    if recent_sessions_rows is not None:
        _append_rich_rows(
            blocks,
            title="Recent Codex Sessions",
            rows=normalized_recent_rows,
            row_renderer=_recent_row_text,
            empty_text="_No recent sessions found._\nStart one with `fresh ...` in DM or `@bot ...` in a channel.",
        )
    else:
        _append_legacy_summary(
            blocks,
            title="Recent Codex Sessions",
            summary=recent_sessions_summary,
        )

    if hint_lines or hint_text:
        context_lines = []
        for line in hint_lines:
            context_lines.append(f"- {line}")
        if hint_text:
            context_lines.append(hint_text)
        blocks.extend(
            [
                {"type": "divider"},
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": "\n".join(context_lines)}],
                },
            ]
        )
    return {
        "type": "home",
        "blocks": blocks,
    }
