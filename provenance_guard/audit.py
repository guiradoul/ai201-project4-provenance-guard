"""Audit log — durable, structured trail of every classification decision.

Step 2 of the architecture narrative calls for an audit trail an appeal can
later be reviewed against. Entries are appended as **JSON Lines** (one JSON
object per line) to a file, so they survive restarts and are trivially
greppable and loadable — structured, not ``print()`` statements.

This is intentionally simple for M3. M4 extends each entry with the second
signal + calibrated confidence; M5 adds appeal events. The schema today:

    {
      "content_id": "3f7a2b1e-...",
      "timestamp":  "2025-04-01T14:32:10.123Z",   # UTC, stamped here
      "creator_id": "test-user-1",
      "attribution": "likely_ai",                  # signal 1 assessment
      "confidence": 0.5,                            # placeholder until M4
      "llm_score": 0.81,                            # signal 1 score
      "status": "pending"
    }
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Optional

# JSONL file location — override with AUDIT_LOG_PATH. Defaults to data/ at the
# project root so it sits next to the code but out of the package.
DEFAULT_LOG_PATH = os.environ.get(
    "AUDIT_LOG_PATH",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "audit_log.jsonl"),
)


def _utc_now_iso() -> str:
    """ISO-8601 UTC timestamp, millisecond precision, 'Z' suffix."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class AuditLog:
    """Append-only JSONL audit log, safe for concurrent writes."""

    def __init__(self, path: str = DEFAULT_LOG_PATH) -> None:
        self.path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def append(self, *, content_id: str, **fields) -> dict:
        """Write one structured entry; a UTC timestamp is stamped automatically.

        Returns the entry that was written.
        """
        entry = {"content_id": content_id, "timestamp": _utc_now_iso(), **fields}
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        return entry

    def get_log(self, limit: Optional[int] = 50, content_id: Optional[str] = None) -> list[dict]:
        """Return the most recent entries (newest first), optionally filtered.

        Reads the JSONL file fresh each call so it reflects all writes,
        including those from other processes.
        """
        try:
            with self._lock, open(self.path, "r", encoding="utf-8") as fh:
                entries = [json.loads(line) for line in fh if line.strip()]
        except FileNotFoundError:
            return []
        if content_id is not None:
            entries = [e for e in entries if e.get("content_id") == content_id]
        entries.reverse()  # newest first
        if limit is not None:
            entries = entries[:limit]
        return entries


# Module-level singleton used by the app.
audit_log = AuditLog()
