"""In-memory datastore — step 2 of the architecture narrative.

This is the single store every component reads from and writes back to. It is
deliberately an in-memory stub for M3 so the pipeline can be built and tested
without a database; the interface (``create_submission`` / ``get_submission`` /
``update_submission``) is what a real persistent store would expose, so swapping
in SQLite/Postgres later does not touch the rest of the app.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Optional


# Submission lifecycle statuses (see planning.md).
STATUS_PENDING = "pending"          # raw text recorded, not yet judged
STATUS_CLASSIFIED = "classified"    # verdict written back
STATUS_UNDER_REVIEW = "under_review"  # an appeal is open

# Appeal record statuses.
APPEAL_OPEN = "open"
APPEAL_RESOLVED = "resolved"


def _utc_now_iso() -> str:
    """ISO-8601 UTC timestamp, millisecond precision, 'Z' suffix."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class SubmissionStore:
    """Thread-safe in-memory store keyed by submission id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._submissions: dict[str, dict] = {}
        self._appeals: dict[str, dict] = {}

    def create_submission(self, text: str, creator_id: Optional[str] = None) -> dict:
        """Record a raw submission with status ``pending`` and return it.

        Nothing is judged here — this just guarantees the text is persisted and
        gives every later component an id to attach results to.
        """
        submission_id = uuid.uuid4().hex
        record = {
            "id": submission_id,
            "text": text,
            "creator_id": creator_id,
            "status": STATUS_PENDING,
            # Result fields, populated by the classifier/scorer/label-mapper later:
            "signals": None,        # {"perplexity": float, "burstiness": float}
            "p_ai": None,           # combined 0-1 probability of AI
            "confidence": None,     # max(p_ai, 1 - p_ai)
            "predicted_class": None,  # "ai" | "uncertain" | "human"
            "label_text": None,     # user-facing transparency string
        }
        with self._lock:
            self._submissions[submission_id] = record
        return dict(record)

    def get_submission(self, submission_id: str) -> Optional[dict]:
        with self._lock:
            record = self._submissions.get(submission_id)
            return dict(record) if record else None

    def update_submission(self, submission_id: str, **fields) -> Optional[dict]:
        with self._lock:
            record = self._submissions.get(submission_id)
            if record is None:
                return None
            record.update(fields)
            return dict(record)

    def create_appeal(
        self,
        content_id: str,
        creator_reasoning: str,
        creator_id: Optional[str] = None,
        evidence_url: Optional[str] = None,
    ) -> dict:
        """Record an appeal linked to a submission and return it."""
        appeal_id = uuid.uuid4().hex
        record = {
            "appeal_id": appeal_id,
            "content_id": content_id,
            "creator_id": creator_id,           # optional metadata
            "creator_reasoning": creator_reasoning,
            "evidence_url": evidence_url,
            "status": APPEAL_OPEN,
            "created_at": _utc_now_iso(),
            # Filled in when a reviewer resolves it (M5 review flow):
            "resolution": None,
            "reviewer_id": None,
            "resolved_at": None,
        }
        with self._lock:
            self._appeals[appeal_id] = record
        return dict(record)

    def get_appeal(self, appeal_id: str) -> Optional[dict]:
        with self._lock:
            record = self._appeals.get(appeal_id)
            return dict(record) if record else None


# Module-level singleton used by the app. Swap for a real store in production.
store = SubmissionStore()
