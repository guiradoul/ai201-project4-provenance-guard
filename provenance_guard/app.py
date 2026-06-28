"""API layer — the front door (steps 1, 4 & 6 of the architecture narrative).

``POST /submit`` validates the request, records the submission, runs both
detection signals, combines them into a confidence score, and persists the
verdict. ``GET /submissions/<id>`` serves the stored verdict; ``GET /log``
serves the audit trail. The user-facing transparency label is still a
placeholder until the label mapper is wired in M5.
"""

from __future__ import annotations

import os

from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from provenance_guard.audit import audit_log
from provenance_guard.labeler import map_label
from provenance_guard.scorer import confidence_score
from provenance_guard.signals import (
    GroqSignalError,
    burstiness_signal,
    groq_attribution_signal,
)
from provenance_guard.store import (
    STATUS_CLASSIFIED,
    STATUS_PENDING,
    STATUS_UNDER_REVIEW,
    store,
)

# Request guardrails (validation in the ingestion endpoint).
MAX_TEXT_CHARS = 50_000
MIN_TEXT_CHARS = 1

# Rate limits for /submit, keyed per client (IP). Tiered to fit a real writer
# iterating on their own work while stopping a flooding script. See the
# "Rate limiting" section of the README for the reasoning behind each number.
# Each submission triggers a paid LLM call, so the caps also bound cost.
SUBMIT_RATE_LIMIT = os.environ.get(
    "SUBMIT_RATE_LIMIT", "5 per minute;50 per hour;200 per day"
)

# Label shown when scoring couldn't complete (e.g. Signal 1 unavailable).
PENDING_LABEL = "⚪ Pending — classification could not be completed"


def create_app() -> Flask:
    app = Flask(__name__)

    limiter = Limiter(
        key_func=get_remote_address,
        app=app,
        default_limits=["120 per minute"],
    )

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.post("/submit")
    @limiter.limit(SUBMIT_RATE_LIMIT)
    def submit():
        """Ingestion endpoint (steps 1-4).

        Validates the payload, records the submission (assigning a content_id),
        runs both detection signals, combines them into a confidence score,
        persists the verdict, writes an audit-log entry, and returns it. The
        final transparency label is still a placeholder until M5.
        """
        data = request.get_json(silent=True) or {}
        text = data.get("text")
        creator_id = data.get("creator_id")

        # --- Validation ---
        if not isinstance(text, str) or not text.strip():
            return jsonify({"error": "field 'text' is required and must be non-empty"}), 400
        if len(text) > MAX_TEXT_CHARS:
            return jsonify({"error": f"text exceeds {MAX_TEXT_CHARS} character limit"}), 413
        if not isinstance(creator_id, str) or not creator_id.strip():
            return jsonify({"error": "field 'creator_id' is required and must be non-empty"}), 400

        # --- Record (step 2): assign a content_id, status pending ---
        record = store.create_submission(text=text, creator_id=creator_id)
        content_id = record["id"]

        # --- Signal 1 (step 3): Groq attribution ---
        try:
            signal_1 = groq_attribution_signal(text, return_detail=True)
        except GroqSignalError as exc:
            # Don't lose the submission if the signal is unavailable — record the
            # failure and still return the content_id so the platform can retry.
            signal_1 = {
                "signal": "groq_attribution",
                "error": str(exc),
                "score": None,
                "assessment": None,
            }

        # --- Signal 2 (step 3): burstiness ---
        signal_2 = burstiness_signal(text, return_detail=True)

        x1 = signal_1.get("score")
        x2 = signal_2.get("score")

        # --- Confidence scoring (step 4) + label mapping (step 5) ---
        if x1 is not None and x2 is not None:
            verdict = confidence_score(x1, x2)
            p_ai = verdict["p_ai"]
            confidence = verdict["confidence"]
            predicted_class = verdict["predicted_class"]
            label_text = map_label(p_ai, confidence)["label_text"]
            status = STATUS_CLASSIFIED
        else:
            # Signal 1 unavailable — can't combine; leave the verdict open.
            p_ai = confidence = predicted_class = None
            label_text = PENDING_LABEL
            status = STATUS_PENDING

        # --- Persist the verdict ---
        store.update_submission(
            content_id,
            status=status,
            signals={"signal_1": signal_1, "signal_2": signal_2},
            p_ai=p_ai,
            confidence=confidence,
            predicted_class=predicted_class,
            label_text=label_text,
        )

        # --- Audit log: one structured entry per submission (step 2) ---
        # Captures BOTH signals' individual scores alongside the combined
        # confidence and the overall verdict.
        audit_log.append(
            content_id=content_id,
            creator_id=creator_id,
            attribution=predicted_class,        # combined verdict: ai | uncertain | human
            signal_1_score=x1,                  # Signal 1 (Groq attribution)
            signal_2_score=x2,                  # Signal 2 (burstiness)
            p_ai=p_ai,                          # combined probability of AI
            confidence=confidence,              # combined confidence = max(p_ai, 1-p_ai)
            status=status,
        )

        return jsonify({
            "content_id": content_id,
            "signals": {"signal_1": signal_1, "signal_2": signal_2},
            "p_ai": p_ai,
            "confidence": confidence,
            "predicted_class": predicted_class,
            "label": label_text,                # varies by score (step 5)
            "status": status,
        }), 202

    @app.get("/submissions/<submission_id>")
    def get_submission(submission_id: str):
        """Results endpoint (step 6). Returns the stored verdict for an id."""
        record = store.get_submission(submission_id)
        if record is None:
            return jsonify({"error": "submission not found"}), 404
        # Don't echo the full source text back in the verdict response.
        verdict = {k: v for k, v in record.items() if k != "text"}
        return jsonify(verdict)

    @app.post("/appeal")
    @limiter.limit("30 per minute")
    def appeal():
        """Appeals handler (step 7).

        Accepts a ``content_id`` and the creator's ``creator_reasoning``, flips
        the submission to ``under_review``, and logs the appeal *alongside the
        original classification decision* in the audit log. The original verdict
        is never changed here — re-classification/resolution is a separate step.
        """
        data = request.get_json(silent=True) or {}
        content_id = data.get("content_id")
        creator_reasoning = data.get("creator_reasoning")
        creator_id = data.get("creator_id")      # optional metadata
        evidence_url = data.get("evidence_url")  # optional

        # --- Validation ---
        if not isinstance(content_id, str) or not content_id.strip():
            return jsonify({"error": "field 'content_id' is required"}), 400
        submission = store.get_submission(content_id)
        if submission is None:
            return jsonify({"error": "submission not found"}), 404
        if not isinstance(creator_reasoning, str) or not creator_reasoning.strip():
            return jsonify({"error": "field 'creator_reasoning' is required"}), 400
        if submission["status"] == STATUS_UNDER_REVIEW:
            return jsonify({"error": "an appeal is already open for this submission"}), 409

        # --- Record the appeal + flip status to "under review" (step 7) ---
        appeal_record = store.create_appeal(
            content_id=content_id,
            creator_reasoning=creator_reasoning.strip(),
            creator_id=creator_id,
            evidence_url=evidence_url,
        )
        store.update_submission(content_id, status=STATUS_UNDER_REVIEW)

        # --- Snapshot of the original classification being contested ---
        signals = submission.get("signals") or {}
        original_classification = {
            "attribution": submission.get("predicted_class"),
            "p_ai": submission.get("p_ai"),
            "confidence": submission.get("confidence"),
            "signal_1_score": (signals.get("signal_1") or {}).get("score"),
            "signal_2_score": (signals.get("signal_2") or {}).get("score"),
            "label_text": submission.get("label_text"),
        }

        # --- Audit log: appeal recorded next to the decision it contests ---
        audit_log.append(
            content_id=content_id,
            event="appeal_filed",
            appeal_id=appeal_record["appeal_id"],
            creator_id=creator_id,
            creator_reasoning=creator_reasoning.strip(),
            original_classification=original_classification,
            status=STATUS_UNDER_REVIEW,
        )

        return jsonify({
            "message": "Appeal received; the submission is now under review.",
            "appeal_id": appeal_record["appeal_id"],
            "content_id": content_id,
            "status": STATUS_UNDER_REVIEW,
        }), 201

    @app.get("/log")
    def read_log():
        """Audit-log endpoint — most recent structured entries as JSON.

        For documentation/grading visibility. In a real system this would
        require auth (it exposes classification history).
        """
        return jsonify({"entries": audit_log.get_log()})

    return app


app = create_app()


if __name__ == "__main__":
    # Default to 8000 — on macOS port 5000 is taken by the AirPlay Receiver
    # (ControlCenter/AirTunes), which returns an empty 403 and breaks clients.
    app.run(
        host="127.0.0.1",
        port=int(os.environ.get("PORT", 8000)),
        debug=True,
    )
