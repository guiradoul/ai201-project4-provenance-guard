"""Signal 2 — burstiness (variance of sentence-level structure).

What it measures (see planning.md > Detection Signals):
    The unevenness of the writing. Human prose is "bursty" — sentence lengths
    swing widely (a long winding clause, then a three-word punch). AI prose
    trends toward a uniform rhythm. We quantify this with the coefficient of
    variation (std / mean) of sentence length in words.

Output:
    A normalized "AI-ness" score ``x2 in [0, 1]`` where *lower burstiness maps
    to higher AI-ness*. Per the spec, the raw statistic is squashed with a
    min-max clip at training-set percentiles (P5/P95).

Note:
    The spec's burstiness also names "variance of sentence-level surprise,"
    which needs a per-sentence perplexity model. That half is deferred to the
    optional perplexity signal (signals/perplexity.py); this implementation
    uses the model-free structural half (sentence-length variation), so it runs
    with no heavyweight dependencies.
"""

from __future__ import annotations

import math
import re

# --- Calibration constants -------------------------------------------------
# Placeholder percentiles of the raw burstiness statistic (sentence-length
# coefficient of variation) over the training corpus. Refit from real data
# alongside the scorer weights. Uniform AI prose clusters low; human prose high.
BURSTINESS_P5 = 0.15   # very uniform -> treat as ~fully AI-like
BURSTINESS_P95 = 0.85  # very bursty -> treat as ~fully human-like

# Need a few sentences before variance means anything (see edge case #5).
MIN_SENTENCES = 3

_SENTENCE_SPLIT = re.compile(r"[.!?]+(?:\s+|$)")


def _split_sentences(text: str) -> list[str]:
    """Naive sentence splitter on terminal punctuation. Good enough for a
    length-dispersion estimate; not a linguistic tokenizer."""
    return [s for s in (p.strip() for p in _SENTENCE_SPLIT.split(text)) if s]


def _coefficient_of_variation(values: list[int]) -> float:
    """std / mean — scale-free dispersion. 0 means perfectly uniform."""
    n = len(values)
    mean = sum(values) / n
    if mean == 0:
        return 0.0
    variance = sum((v - mean) ** 2 for v in values) / n
    return math.sqrt(variance) / mean


def _normalize(raw_burstiness: float) -> float:
    """Squash raw burstiness to an AI-ness score in [0, 1].

    Lower burstiness -> higher AI-ness. Linear between the P5/P95 percentiles,
    clipped outside that band, then inverted (bursty = human = low AI-ness).
    """
    span = BURSTINESS_P95 - BURSTINESS_P5
    human_ness = (raw_burstiness - BURSTINESS_P5) / span
    human_ness = max(0.0, min(1.0, human_ness))
    return 1.0 - human_ness


def burstiness_signal(text: str, *, return_detail: bool = False):
    """Compute the Signal 2 AI-ness score for ``text``.

    Args:
        text: raw submission text.
        return_detail: if True, return a dict with the raw statistic, sentence
            count, and a ``reliable`` flag alongside the score.

    Returns:
        ``float`` in [0, 1] (higher = more AI-like), or a detail dict.
    """
    if not text or not text.strip():
        raise ValueError("text must be a non-empty string")

    sentences = _split_sentences(text)
    lengths = [len(s.split()) for s in sentences]

    if len(lengths) < MIN_SENTENCES:
        # Too few sentences to judge rhythm; sit at the neutral middle rather
        # than emit a confident score (see edge case #5 in planning.md).
        score, raw, reliable = 0.5, float("nan"), False
    else:
        raw = _coefficient_of_variation(lengths)
        score = _normalize(raw)
        reliable = True

    if return_detail:
        return {
            "signal": "burstiness",
            "score": score,            # x2 in [0, 1], higher = more AI-like
            "raw_cov": raw,            # sentence-length coefficient of variation
            "sentences": len(lengths),
            "reliable": reliable,
        }
    return score


if __name__ == "__main__":
    # Independent verification: clearly-uniform vs clearly-bursty text.
    samples = {
        "uniform / AI-like (even sentence lengths)": (
            "The system processes the input. It returns a result. The result "
            "is then stored. The user receives a response. The process repeats."
        ),
        "bursty / human (mixed lengths)": (
            "I stopped. Out past the cracked fence, where the old man used to "
            "keep his impossible roses, something moved in the long grass and I "
            "felt my heart climb into my throat. Then nothing. Just wind."
        ),
        "too short": "Hello there. Nice day.",
    }
    for label, sample in samples.items():
        d = burstiness_signal(sample, return_detail=True)
        cov = "n/a" if math.isnan(d["raw_cov"]) else f"{d['raw_cov']:.3f}"
        print(f"\n{label}\n  raw CoV = {cov}  sentences={d['sentences']}  "
              f"reliable={d['reliable']}\n  score (AI-ness) = {d['score']:.3f}")
