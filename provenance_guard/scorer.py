"""Confidence scorer — step 4 of the architecture narrative.

Combines the two normalized signal scores into a single probability ``p_ai``
via logistic regression, derives the reported confidence, and maps ``p_ai`` to
a predicted class using the thresholds defined in
``planning.md`` > Uncertainty representation:

    p_ai >= 0.70            -> "ai"        (likely AI)
    0.30 <  p_ai <  0.70    -> "uncertain"
    p_ai <= 0.30            -> "human"     (likely human)

Reported confidence is ``max(p_ai, 1 - p_ai)`` — how sure the system is of
whichever class it picked. A confidence of 0.6 therefore means a weak,
near-coin-flip call (it lands in the uncertain band).
"""

from __future__ import annotations

import math
import os

# --- Logistic-regression weights -------------------------------------------
# PLACEHOLDER until fit on a labeled training set (the spec calls for learned
# weights). Both signals are AI-ness in [0,1], so weights are positive. Signal 1
# (Groq attribution) is the stronger signal, so it carries more weight. Chosen
# so that two neutral 0.5 signals give p_ai = 0.5. Override via env for tuning.
W0 = float(os.environ.get("SCORER_W0", -5.0))  # intercept
W1 = float(os.environ.get("SCORER_W1", 7.0))   # weight on signal 1 (groq)
W2 = float(os.environ.get("SCORER_W2", 3.0))   # weight on signal 2 (burstiness)

# --- Decision thresholds on p_ai (config, not magic numbers) ---------------
THRESHOLD_AI = float(os.environ.get("THRESHOLD_AI", 0.70))
THRESHOLD_HUMAN = float(os.environ.get("THRESHOLD_HUMAN", 0.30))

CLASS_AI = "ai"
CLASS_HUMAN = "human"
CLASS_UNCERTAIN = "uncertain"


def _sigmoid(z: float) -> float:
    """Numerically stable logistic sigmoid."""
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def classify(p_ai: float) -> str:
    """Map a probability to a predicted class per the planning thresholds.

    Boundaries are inclusive toward a decision: exactly 0.70 -> ai,
    exactly 0.30 -> human (matches the table in planning.md).
    """
    if p_ai >= THRESHOLD_AI:
        return CLASS_AI
    if p_ai <= THRESHOLD_HUMAN:
        return CLASS_HUMAN
    return CLASS_UNCERTAIN


def confidence_score(x1: float, x2: float) -> dict:
    """Combine two normalized signal scores into a verdict.

    Args:
        x1: Signal 1 (Groq attribution) AI-ness score in [0, 1].
        x2: Signal 2 (burstiness) AI-ness score in [0, 1].

    Returns:
        dict with ``p_ai`` (probability of AI), ``confidence``
        (max(p_ai, 1-p_ai)), and ``predicted_class``.
    """
    z = W0 + W1 * x1 + W2 * x2
    p_ai = _sigmoid(z)
    return {
        "p_ai": p_ai,
        "confidence": max(p_ai, 1.0 - p_ai),
        "predicted_class": classify(p_ai),
        "weights": {"w0": W0, "w1": W1, "w2": W2},
    }


if __name__ == "__main__":
    # ---- Verification 1: class mapping matches the planning thresholds ----
    # Expected mapping straight from planning.md > Uncertainty representation.
    checks = [
        (0.00, CLASS_HUMAN),
        (0.15, CLASS_HUMAN),
        (0.30, CLASS_HUMAN),      # boundary: <= 0.30 -> human
        (0.3001, CLASS_UNCERTAIN),
        (0.50, CLASS_UNCERTAIN),
        (0.60, CLASS_UNCERTAIN),  # "0.6 means uncertain" per the spec
        (0.6999, CLASS_UNCERTAIN),
        (0.70, CLASS_AI),         # boundary: >= 0.70 -> ai
        (0.85, CLASS_AI),
        (1.00, CLASS_AI),
    ]
    print("Threshold mapping (p_ai -> class):")
    all_ok = True
    for p, expected in checks:
        got = classify(p)
        ok = got == expected
        all_ok &= ok
        print(f"  p_ai={p:<7} -> {got:<9} expected {expected:<9} {'OK' if ok else 'MISMATCH'}")
    assert all_ok, "classify() does not match the planning thresholds!"
    print("  => matches planning.md thresholds (>=0.70 ai, <=0.30 human, else uncertain)\n")

    # ---- Verification 2: confidence is max(p_ai, 1-p_ai) ----
    for x1, x2 in [(0.0, 0.0), (0.5, 0.5), (0.92, 0.5), (1.0, 1.0)]:
        out = confidence_score(x1, x2)
        assert abs(out["confidence"] - max(out["p_ai"], 1 - out["p_ai"])) < 1e-9
    print("confidence == max(p_ai, 1-p_ai): OK\n")

    # ---- Verification 3: combined signals separate AI from human ----
    cases = {
        "both AI-leaning (x1=0.85, x2=0.80)": (0.85, 0.80),
        "both human-leaning (x1=0.20, x2=0.15)": (0.20, 0.15),
        "neutral (x1=0.50, x2=0.50)": (0.50, 0.50),
        "signal 1 AI, signal 2 human (x1=0.85, x2=0.20)": (0.85, 0.20),
    }
    print("Combined scoring:")
    for label, (x1, x2) in cases.items():
        out = confidence_score(x1, x2)
        print(f"  {label}\n    -> p_ai={out['p_ai']:.3f}  "
              f"confidence={out['confidence']:.3f}  class={out['predicted_class']}")
