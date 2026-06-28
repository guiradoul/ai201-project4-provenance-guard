"""Transparency label mapper — step 5 of the architecture narrative.

Maps a ``(p_ai, confidence)`` verdict onto the user-facing transparency label.
The three variants and their *exact* wording come from
``planning.md`` > Transparency label design. Keeping this mapping separate from
the model means the platform can reword labels or shift thresholds without
retraining anything — the strings live here and nowhere else.
"""

from __future__ import annotations

from provenance_guard.scorer import CLASS_AI, CLASS_HUMAN, CLASS_UNCERTAIN, classify

# Icons per class (planning.md §3).
ICONS = {
    CLASS_AI: "🟡",
    CLASS_HUMAN: "🟢",
    CLASS_UNCERTAIN: "⚪",
}

# Primary line shown to the reader (verbatim from planning.md §3).
HEADLINES = {
    CLASS_AI: "Shows signals of AI generation",
    CLASS_HUMAN: "High confidence this was human-created",
    CLASS_UNCERTAIN: "Inconclusive — not enough signal to label",
}


def _detail(predicted_class: str, confidence: float) -> str:
    """Secondary line — the honest caveat + appeal path (verbatim, planning §3)."""
    if predicted_class == CLASS_AI:
        return (
            f"Our automated check is {confidence:.0%} confident this text was "
            "largely AI-generated. This is a signal, not a verdict — the creator can appeal."
        )
    if predicted_class == CLASS_HUMAN:
        return (
            f"Our automated check is {confidence:.0%} confident this text was "
            "written by a person. No AI-generation signals stood out."
        )
    return (
        "Our automated check couldn't confidently tell whether this was human- "
        "or AI-written. No claim is being made either way."
    )


def map_label(p_ai: float, confidence: float) -> dict:
    """Map a verdict to its transparency label.

    Args:
        p_ai: combined probability of AI (drives the class via the same
            thresholds the scorer uses — single source of truth).
        confidence: reported confidence (``max(p_ai, 1-p_ai)``), shown in the
            AI/human variants so the label never overstates certainty.

    Returns:
        dict with ``predicted_class``, ``icon``, ``headline``, ``detail``, and
        the combined ``label_text`` (icon + headline + detail).
    """
    predicted_class = classify(p_ai)
    icon = ICONS[predicted_class]
    headline = HEADLINES[predicted_class]
    detail = _detail(predicted_class, confidence)
    return {
        "predicted_class": predicted_class,
        "icon": icon,
        "headline": headline,
        "detail": detail,
        "label_text": f"{icon} {headline}\n{detail}",
    }


if __name__ == "__main__":
    # Verification: produce all three variants and confirm the text matches the
    # spec, including the threshold boundaries (0.30 -> human, 0.70 -> ai).
    cases = [
        (0.85, 0.85, CLASS_AI),
        (0.70, 0.70, CLASS_AI),       # boundary
        (0.15, 0.85, CLASS_HUMAN),
        (0.30, 0.70, CLASS_HUMAN),    # boundary
        (0.50, 0.50, CLASS_UNCERTAIN),
        (0.60, 0.60, CLASS_UNCERTAIN),
    ]
    for p_ai, confidence, expected in cases:
        out = map_label(p_ai, confidence)
        assert out["predicted_class"] == expected, (
            f"p_ai={p_ai} -> {out['predicted_class']}, expected {expected}"
        )
        assert out["headline"] == HEADLINES[expected]
        print(f"p_ai={p_ai}  conf={confidence:.0%}  class={out['predicted_class']}")
        print(f"  {out['label_text']}\n")
    print("All three variants produced; headlines match planning.md §3 exactly.")
