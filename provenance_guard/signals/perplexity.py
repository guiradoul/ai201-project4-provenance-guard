"""Signal 1 — token predictability (perplexity under a reference model).

What it measures (see planning.md > Detection Signals):
    The average per-token "surprise" of the text under a reference language
    model = mean negative log-likelihood per token. Low perplexity means the
    text sits in the model's high-probability comfort zone, which is the
    fingerprint of likelihood-maximizing AI decoders.

Output:
    A normalized "AI-ness" score ``x1 in [0, 1]`` where *lower perplexity maps
    to higher AI-ness*. Per the spec, the raw perplexity is squashed with a
    min-max clip at training-set percentiles (P5/P95) so a single document
    can't blow up the scale.

Reference model:
    GPT-2 (small) via ``transformers`` is the default backend — the standard
    way to compute perplexity for AI-text detection. The model is loaded lazily
    and cached, so importing this module is cheap and the heavy load only
    happens on first use. The backend is isolated behind ``_reference_nll`` so
    it can be swapped (a different LM, a remote scoring service) without
    touching the normalization or the public API.
"""

from __future__ import annotations

import math
from typing import Optional

try:
    import torch  # type: ignore
except ImportError:
    torch = None

# --- Calibration constants -------------------------------------------------
# Placeholder percentiles of raw perplexity over the *human* reference corpus.
# These are deliberately rough for M3 and get refit from real data in M4
# (the "min-max squash with percentiles learned from the training set" step).
PERPLEXITY_P5 = 20.0    # very predictable text -> treat as ~fully AI-like
PERPLEXITY_P95 = 120.0  # very surprising text -> treat as ~fully human-like

# Minimum tokens needed for a stable estimate. Below this, perplexity is noisy
# (see edge case #5 in planning.md); we surface that to the caller.
MIN_TOKENS = 20

# Sliding-window length for long inputs (GPT-2 context is 1024 tokens).
_MAX_WINDOW = 512

# Lazily-initialized model/tokenizer cache.
_model = None
_tokenizer = None


def _load_reference_model():
    """Load and cache GPT-2 + its tokenizer on first use."""
    global _model, _tokenizer
    if _model is not None:
        return _model, _tokenizer
    try:
        if torch is None:
            raise ImportError("torch is not available")
        from transformers import GPT2LMHeadModel, GPT2TokenizerFast  # type: ignore
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise ImportError(
            "Signal 1 needs a local reference LM. Install it with:\n"
            "    pip install transformers torch\n"
            "(or swap _reference_nll for a different scoring backend)."
        ) from exc

    _tokenizer = GPT2TokenizerFast.from_pretrained("gpt2")
    _model = GPT2LMHeadModel.from_pretrained("gpt2")
    _model.eval()
    return _model, _tokenizer


def _reference_nll(text: str) -> tuple[float, int]:
    """Return (mean per-token negative log-likelihood, token_count) for ``text``.

    Uses a sliding window so inputs longer than the model context are scored in
    chunks and averaged by token count. This is the only model-specific code;
    everything else works off the scalar NLL it returns.
    """
    model, tokenizer = _load_reference_model()
    input_ids = tokenizer(text, return_tensors="pt").input_ids
    n_tokens = int(input_ids.shape[1])
    if n_tokens < 2:
        return float("nan"), n_tokens

    total_nll = 0.0
    total_counted = 0
    with torch.no_grad():
        for start in range(0, n_tokens, _MAX_WINDOW):
            window = input_ids[:, start : start + _MAX_WINDOW]
            if window.shape[1] < 2:
                break
            # labels == input_ids => model returns mean CE loss over the window,
            # which is exactly the mean per-token NLL for that window.
            loss = model(window, labels=window).loss.item()
            counted = window.shape[1] - 1  # first token has no prediction
            total_nll += loss * counted
            total_counted += counted

    if total_counted == 0:
        return float("nan"), n_tokens
    return total_nll / total_counted, n_tokens


def _normalize(perplexity: float) -> float:
    """Squash raw perplexity to an AI-ness score in [0, 1].

    Lower perplexity -> higher AI-ness. Linear between the P5/P95 percentiles,
    clipped outside that band.
    """
    span = PERPLEXITY_P95 - PERPLEXITY_P5
    x1 = (PERPLEXITY_P95 - perplexity) / span
    return max(0.0, min(1.0, x1))


def perplexity_signal(text: str, *, return_detail: bool = False):
    """Compute the Signal 1 AI-ness score for ``text``.

    Args:
        text: raw submission text.
        return_detail: if True, return a dict with the raw perplexity, token
            count, and a ``reliable`` flag alongside the score (useful for
            verification and for the confidence scorer in M4).

    Returns:
        ``float`` in [0, 1] (higher = more AI-like), or a detail dict if
        ``return_detail`` is True.
    """
    if not text or not text.strip():
        raise ValueError("text must be a non-empty string")

    mean_nll, n_tokens = _reference_nll(text)
    if math.isnan(mean_nll):
        # Too short to score; bias toward the neutral middle rather than a
        # confident label (see edge case #5 in planning.md).
        score, perplexity, reliable = 0.5, float("nan"), False
    else:
        perplexity = math.exp(mean_nll)
        score = _normalize(perplexity)
        reliable = n_tokens >= MIN_TOKENS

    if return_detail:
        return {
            "score": score,            # x1 in [0, 1], higher = more AI-like
            "perplexity": perplexity,  # raw mean per-token perplexity
            "tokens": n_tokens,
            "reliable": reliable,      # False if too short for a stable estimate
        }
    return score


if __name__ == "__main__":
    # M3 verification harness: exercise the function directly on a few inputs
    # BEFORE wiring it into the endpoint (per the AI Tool Plan in planning.md).
    samples = {
        "clearly AI-ish (fluent, generic)": (
            "In today's fast-paced world, it is important to recognize that "
            "effective communication plays a crucial role in fostering "
            "meaningful connections and driving successful outcomes."
        ),
        "clearly human (specific, bursty)": (
            "My grandmother's kitchen smelled of burnt cardamom. She'd swat my "
            "hand from the pan, mutter in Mooré, then sneak me the crispy bits "
            "anyway. I never learned the recipe. I wish I had."
        ),
        "too short": "Hello there.",
    }
    for label, sample in samples.items():
        detail = perplexity_signal(sample, return_detail=True)
        print(f"\n{label}")
        print(f"  perplexity = {detail['perplexity']:.1f}" if not math.isnan(detail["perplexity"]) else "  perplexity = n/a")
        print(f"  score (AI-ness) = {detail['score']:.3f}  reliable={detail['reliable']}")
