import threading
import time
from dataclasses import dataclass
from typing import Optional

from codex_threads import ThreadSummary


DEFAULT_SELECTION_TTL_SECONDS = 15 * 60


@dataclass(frozen=True)
class SessionSelectionSnapshot:
    thread_ids: tuple[str, ...]
    created_at: int


class SessionSelectionCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._snapshots = {}

    def put(self, thread_key, thread_ids):
        with self._lock:
            self._snapshots[thread_key] = SessionSelectionSnapshot(
                thread_ids=tuple(thread_ids),
                created_at=int(time.time()),
            )

    def get(self, thread_key) -> Optional[SessionSelectionSnapshot]:
        with self._lock:
            return self._snapshots.get(thread_key)

    def clear(self, thread_key):
        with self._lock:
            self._snapshots.pop(thread_key, None)


def is_snapshot_fresh(snapshot: Optional[SessionSelectionSnapshot], ttl_seconds=DEFAULT_SELECTION_TTL_SECONDS):
    if snapshot is None:
        return False
    return (int(time.time()) - snapshot.created_at) <= max(1, int(ttl_seconds))


def resolve_recent_index(snapshot: Optional[SessionSelectionSnapshot], index: int, ttl_seconds=DEFAULT_SELECTION_TTL_SECONDS):
    if not is_snapshot_fresh(snapshot, ttl_seconds=ttl_seconds):
        raise RuntimeError("最近一次 recent/sessions 列表已经过期，请先重新发送 `recent` 或 `sessions`。")

    if index < 1 or index > len(snapshot.thread_ids):
        raise RuntimeError(f"`attach recent {index}` 超出可选范围，请先重新查看 `recent`。")

    return snapshot.thread_ids[index - 1]


def _format_updated_at(updated_at):
    if not updated_at:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(updated_at))


def format_thread_summaries(summaries: list[ThreadSummary], *, heading: Optional[str] = None):
    lines = []
    if heading:
        lines.append(heading)
        lines.append("")

    if not summaries:
        lines.append("当前没有可显示的 Codex sessions。")
        return "\n".join(lines).strip()

    for index, summary in enumerate(summaries, start=1):
        title = summary.name or summary.preview or "(untitled)"
        lines.append(
            f"{index}. `{summary.thread_id}` | {title} | cwd=`{summary.cwd or '-'}` | "
            f"updated=`{_format_updated_at(summary.updated_at)}` | status=`{summary.status_type}`"
        )

    return "\n".join(lines).strip()
