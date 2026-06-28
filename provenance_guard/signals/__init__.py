"""Detection signals (Classifier — step 3 of the architecture narrative).

Each signal maps raw text to a normalized "AI-ness" score in ``[0, 1]`` where
higher means *more* likely AI-generated. Signals are kept continuous (not
binary) so the confidence scorer in M4 has gradient to work with.
"""

from provenance_guard.signals.burstiness import burstiness_signal
from provenance_guard.signals.groq_attribution import (
    GroqSignalError,
    groq_attribution_signal,
)
from provenance_guard.signals.perplexity import perplexity_signal

__all__ = [
    "groq_attribution_signal",
    "GroqSignalError",
    "burstiness_signal",
    "perplexity_signal",
]
