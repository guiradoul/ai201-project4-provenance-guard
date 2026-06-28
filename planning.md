# Architecture Narrative: The Journey of One Submission

This is the complete path a single piece of text takes from the moment a creator hits "post" to the label a reader eventually sees — and what happens if the creator disagrees with that label.

## 1. The submission arrives — API Layer (ingestion endpoint)

A creator on the host platform (say, a writing site) submits a story. The platform's backend forwards that text to Provenance Guard through a single POST endpoint (e.g. `POST /submissions`). This endpoint is the front door: it does no analysis itself. Its job is to validate the request (is there actually text? is it within length limits? is the caller authenticated?), assign the submission a unique ID, and hand it inward. It immediately returns that ID so the platform isn't left waiting on the slow part.

## 2. The text is recorded — Datastore (submissions table)

Before anything is judged, the raw submission is written to persistent storage with a status of `pending`. This guarantees nothing is lost, gives us something to attach results to later, and creates the audit trail an appeal will eventually need. Every later component reads from and writes back to this same store.

## 3. The text is judged — Classifier

The heart of the system. The classifier takes the text and decides which class it belongs to — `human`, `ai`, (and optionally `uncertain`/`mixed`). This is where my actual model lives: either a prompted LLM classifier or a fine-tuned model, the same head-to-head setup I used in TakeMeter. It outputs two things: a predicted label and the raw signal behind that prediction (e.g. class probabilities).

## 4. The judgment is scored — Confidence Scorer

A raw label alone ("AI") is dangerous because it hides how sure the system is. The confidence scorer turns the classifier's raw signal into a single 0–1 confidence number. This is what lets the platform treat a 0.92 "AI" differently from a 0.55 "AI." It's a distinct step because the decision logic — including thresholds for what counts as "high confidence" vs. "borderline" — should be reasoned about and tuned separately from the model itself.

## 5. The result becomes something a human can read — Label / Transparency Mapper

The classifier speaks in `human` / `ai` + `0.55`. Readers need plain language. This component maps the (label, confidence) pair onto a user-facing transparency label — for example:

- high-confidence human → 🟢 "High confidence this was human-created"
- high-confidence AI → 🟡 "Shows signals of AI generation"
- low confidence either way → ⚪ "Inconclusive — not enough signal to label"

Keeping this mapping separate means the platform can reword labels or change thresholds without retraining anything. The full result (label, confidence, transparency text) is written back to the datastore and the submission's status flips to `classified`.

## 6. The label is served — API Layer (results endpoint)

The platform fetches the verdict through a GET endpoint (e.g. `GET /submissions/{id}`) and renders the transparency label under the creator's post. This is the label the user finally sees. The flow above is the happy path — everything downstream of here exists because that path is sometimes wrong.

## 7. The creator pushes back — Appeals Handler

A creator who wrote every word but got flagged `ai` clicks "Appeal." A POST endpoint (e.g. `POST /submissions/{id}/appeals`) records their statement, creates an appeal record linked to the original submission, and flips the submission's status to `under_review`. False positives are the central risk of any detection system, so this is a first-class part of the architecture, not an afterthought.

## 8. The appeal is resolved — Review / Resolution flow

The appeal is routed to a moderator (or a re-evaluation step) who upholds or overturns the label. The resolution is written back to the datastore — updating the transparency label the reader sees and closing the appeal. This closes the loop: every label is contestable, and the audit trail from step 2 onward is what makes a fair review possible.

## One-line summary of the chain

API ingestion → Datastore → Classifier → Confidence Scorer → Label Mapper → Datastore → API results endpoint → (if contested) Appeals Handler → Review/Resolution → Datastore.

The guiding principle throughout — straight from the brief — is that Provenance Guard only **informs and labels; it never deletes or punishes**. The host platform decides what to do with the label.

---

# Architecture Diagram

Two flows share one datastore. Arrows are labeled with **what passes** between components.

```
① SUBMISSION FLOW
=================

  ┌────────────────┐
  │  POST /submit  │
  │ (API ingestion)│
  └────────┬───────┘
           │  raw text
           v
  ┌────────────────┐
  │   Signal 1:    │
  │ LLM attribution│
  │     (Groq)     │
  └────────┬───────┘
           │  raw text + attribution score
           v
  ┌────────────────┐
  │    Signal 2    │
  │   Burstiness   │
  └────────┬───────┘
           │  signal scores (attribution, burstiness)
           v
  ┌────────────────┐
  │   Confidence   │
  │     Scorer     │
  └────────┬───────┘
           │  combined 0–1 confidence + predicted class
           v
  ┌────────────────┐
  │  Transparency  │
  │  Label Mapper  │
  └────────┬───────┘
           │  label text + confidence + signal scores
           v
  ╔════════════════╗
  ║  Audit log /   ║<───────────────────┐
  ║   Datastore    ║                    │
  ╚════════╤═══════╝                    │ same
           │  stored verdict            │ persistent
           │  (status: classified)      │ store
           v                            │
  ┌────────────────┐                    │
  │    Response    │                    │
  │ (id + label)   │                    │
  └────────────────┘                    │
                                        │
② APPEAL FLOW                           │
=============                           │
                                        │
  ┌────────────────┐                    │
  │  POST /appeal  │                    │
  │     (API)      │                    │
  └────────┬───────┘                    │
           │  content_id +              │
           │  creator_reasoning         │
           v                            │
  ┌────────────────┐                    │
  │ Status Update  │                    │
  │ (→under_review)│                    │
  └────────┬───────┘                    │
           │  appeal record +           │
           │  status change             │
           v                            │
  ╔════════════════╗                    │
  ║  Audit log /   ║════════════════════┘
  ║   Datastore    ║
  ╚════════╤═══════╝
           │  updated submission
           │  (status: under_review)
           v
  ┌────────────────┐
  │    Response    │
  │ (id + status)  │
  └────────────────┘
```

> Both flows write to and read from the **same datastore**, which is also the audit log (shown with double borders `╔╗`). The submission flow produces the label a reader sees; the appeal flow lets a creator contest it. Provenance Guard only informs and labels — it never deletes or punishes.

---

# Detection Signals (Classifier — step 3)

The classifier's prediction rests on two complementary signals that are *different in kind*: Signal 1 is a holistic LLM judgment, Signal 2 a mechanical distributional statistic. Each catches cases the other misses.

## Signal 1 — LLM attribution assessment (Groq-prompted classifier)

*Implemented in `provenance_guard/signals/groq_attribution.py`.*

- **What property it measures.** A capable instruction-tuned model (served via Groq) reads the passage and judges how likely it was AI-generated, returning a **structured** assessment — an `ai_likelihood ∈ [0,1]` plus the `indicators` and `reasoning` behind it. Rather than one statistic, it weighs many surface and semantic cues at once: generic phrasing, absence of concrete lived detail, uniform fluency, predictable structure.
- **Why it differs between human and AI.** The model has internalized the stylistic fingerprint of likelihood-maximizing generation from vast exposure to both human and AI text — the same fluency-uniformity and genericness the statistical signals only approximate, but read holistically and in context (it can tell formulaic ad copy from a deliberately plain human poem).
- **What it can't capture.**
  - It is a *judgment, not a measurement* — it carries no calibrated probability of its own and can be confidently wrong; we treat its score as one input, never a verdict.
  - Prompt- and model-version-sensitive: the same text can score differently across models or prompt wordings.
  - It can be fooled by adversarial "humanized" AI text, just as a human reader can.
  - It's a network call — latency, cost, and availability are real; outages must degrade gracefully (the endpoint logs `signal_1_failed` and still returns a content_id).
  - Possible training-data / demographic biases (e.g. over-flagging non-native English) that are hard to audit from outside.

## Signal 2 — Burstiness (variance of sentence-level surprise and length) *(planned, M4)*

- **What property it measures.** Not the *average* surprise (Signal 1) but its *spread* — the variance/unevenness across the document, in both sentence length and per-sentence perplexity.
- **Why it differs between human and AI.** Human writing is bursty: a long winding sentence followed by a three-word punch, a jarring word among ordinary ones, register that shifts. AI text trends toward uniform rhythm — similar sentence lengths, evenly distributed surprise, steady register — because the same likelihood pressure is applied at every step. Humans show high variance; AI shows a flat profile.
- **What it can't capture.**
  - Heavily edited/polished human prose (or anything run through a grammar tool) loses its burstiness and reads as AI.
  - AI prompted to "vary sentence length / write naturally" can manufacture burstiness on demand.
  - It needs enough sentences — a single short paragraph can't support a variance estimate.
  - Genre confound: technical and reference writing is uniform regardless of who wrote it.

## Why this pair — and an honest caveat

Pairing a holistic LLM judgment (Signal 1) with a mechanical distributional statistic (Signal 2) is deliberate: they fail in *different* ways, so an input that fools one — burstiness defeated by light editing, say — may still be caught by the other. The shared residual risk is "humanized"/paraphrased AI text, which can fool a human-like reader and surface statistics at once.

**Perplexity** (token predictability under a reference model) is implemented in `provenance_guard/signals/perplexity.py` and kept as an **optional future statistical signal** — a natural third leg if we want a second, fully local, model-independent measurement alongside burstiness. It was the original Signal 1 before we adopted the Groq classifier.

---

# Design Decisions

Implementation-ready answers to the five core design questions. The "what each signal measures / why it differs / blind spots" detail lives in **Detection Signals** above; this section nails down outputs, math, thresholds, exact label text, the appeals contract, and known failure modes.

## 1. Detection signals — outputs and combination

Two signals (see above): **Signal 1 = LLM attribution (Groq)**, **Signal 2 = burstiness**.

**Each signal's raw output and its normalized form:**

| Signal | Raw output | Normalized "AI-ness" output |
|---|---|---|
| LLM attribution (Groq) | structured JSON — `ai_likelihood ∈ [0,1]`, `assessment`, `indicators`, `reasoning` | `x1 = ai_likelihood` (already in [0,1], higher = more AI) |
| Burstiness | a dispersion statistic = variance of sentence-level surprise + coefficient of variation of sentence length — a non-negative real | `x2 ∈ [0,1]` — squashed so *lower* burstiness → *higher* AI-ness |

Both signals resolve to **continuous scores in [0,1]**, not binary flags. We keep them continuous so the confidence scorer has gradient to work with; binarizing early would throw away exactly the borderline information the system exists to surface.

Signal 1 already emits a `[0,1]` likelihood, so it needs no rescaling. Signal 2's raw statistic is normalized with a min–max squash using percentiles learned from the training set (clip at the 5th/95th percentile to resist outliers), so a raw burstiness value maps to a stable fraction regardless of document.

**Combination → single confidence score.** A logistic regression over the two normalized signals, with weights fit on the labeled training set:

```
p_ai = sigmoid(w0 + w1 * x1 + w2 * x2)
```

`p_ai ∈ [0,1]` is the model's probability that the text is AI-generated. Logistic regression (rather than a hand-picked weighted average) is chosen because (a) it learns the weights from data, (b) it is inherently a probability output, and (c) it is trivially re-fit and inspectable — we can read off which signal carries more weight. The system's **reported confidence** in its prediction is `confidence = max(p_ai, 1 − p_ai)` — i.e. how sure it is of whichever class it picked.

## 2. Uncertainty representation

**What a 0.6 means.** Confidence is `P(predicted class)`. A confidence of **0.6 means the system assigns 60% probability to the class it chose** — a weak, near-coin-flip call that should read to a human as "leaning, not sure." It explicitly does *not* mean "60% of the text is AI."

**Calibration.** Raw logistic-regression outputs are not trustworthy probabilities out of the box, so we calibrate on a held-out validation set using **Platt scaling** (fit a 1-D logistic on the model's scores) — or isotonic regression if the reliability curve is non-monotonic. The target: among all items the system scores ~0.6, ~60% are genuinely that class. We measure calibration with a **reliability diagram** and **Expected Calibration Error (ECE)**, and re-fit whenever the reference model or training data changes.

**Thresholds (on `p_ai`)** — three zones:

| `p_ai` range | Zone | Predicted label |
|---|---|---|
| `p_ai ≥ 0.70` | **Likely AI** | `ai` |
| `0.30 < p_ai < 0.70` | **Uncertain** | `uncertain` |
| `p_ai ≤ 0.30` | **Likely human** | `human` |

The uncertain band is deliberately wide (0.40 of the range) because the cost of a confident wrong label — especially a false "AI" on a real creator — is higher than the cost of admitting we don't know. Thresholds live in config, not code, so they can be tuned without redeployment.

## 3. Transparency label design

Three label variants, finalized now. Each has an icon, a primary line (what the reader sees first), and a secondary line (the honest caveat + appeal path). The numeric confidence is shown so the label never overstates certainty.

**High-confidence AI** (`p_ai ≥ 0.70`):
> 🟡 **Shows signals of AI generation**
> Our automated check is {confidence:.0%} confident this text was largely AI-generated. This is a signal, not a verdict — the creator can appeal.

**High-confidence human** (`p_ai ≤ 0.30`):
> 🟢 **High confidence this was human-created**
> Our automated check is {confidence:.0%} confident this text was written by a person. No AI-generation signals stood out.

**Uncertain** (`0.30 < p_ai < 0.70`):
> ⚪ **Inconclusive — not enough signal to label**
> Our automated check couldn't confidently tell whether this was human- or AI-written. No claim is being made either way.

The mapper stores the rendered `label_text` alongside `p_ai`, `confidence`, and both signal scores, so the exact words shown to the reader are reproducible from the audit log. Reword the strings here without retraining anything.

## 4. Appeals workflow

**Who can appeal.** The authenticated **owner of the submission** (the original creator), verified against the `creator_id` recorded at ingestion. In practice appeals target `ai` or `uncertain` labels, but any label the creator disagrees with is appealable. Readers and third parties cannot appeal.

**What they provide.** `POST /submissions/{id}/appeals` with:
- `submission_id` (path) — must belong to the caller
- `statement` (required, free text, min ~20 chars) — the creator's explanation
- `evidence_url` (optional) — link to drafts, version history, or process proof

**What the system does on receipt** (atomic):
1. Validate ownership and that the submission isn't already `under_review`.
2. Create an **appeal record**: `{appeal_id, submission_id, creator_id, statement, evidence_url, status: "open", created_at}`.
3. Flip `submission.status` → `under_review` (the visible label gains an "under review" marker; the original label is **not** silently changed).
4. Write an **audit-log** entry: `{event: "appeal_filed", submission_id, appeal_id, actor: creator_id, timestamp}`.
5. Return `{appeal_id, status: "under_review"}`.

Note the system never deletes the submission or alters the model's original verdict — it only records the dispute and routes it.

**What a reviewer sees in the appeal queue.** A list of `open` appeals sorted oldest-first, each row showing:
- submission id + original text (or excerpt with expand)
- the model's predicted label, `confidence`, and **both raw signal scores** (so the reviewer can see *why* it flagged)
- the creator's `statement` and `evidence_url`
- timestamps (submitted, appealed)

Reviewer actions: **Uphold** or **Overturn**, each with a required note. On resolution: set the corrected `submission.status` (`classified` or `human`) and `label_text`, set `appeal.status: "resolved"` with `{resolution, reviewer_id, resolved_at}`, and write an audit-log entry `{event: "appeal_resolved", ...}`. This closes the loop.

## 5. Anticipated edge cases (specific failure modes)

Concrete content types this system will handle poorly, and why:

1. **Repetitive, simple-vocabulary verse (poetry, song lyrics, nursery rhymes).** Uniform short lines give *low* burstiness, and the plainness can lead the LLM to read the text as generic/AI-like if it can't distinguish traditional verse from generated filler. A genuinely human poem like "Row, row, row your boat" could be mislabeled `ai` — both signals leaning the same way.

2. **Non-native / ESL or formulaic human writing.** Learners and writers of templated prose (cover letters, boilerplate reports) lean on common, high-frequency constructions → low burstiness, and LLM detectors are documented to **over-flag non-native English** as AI. The two failures compound into a false `ai`. This is a **fairness risk**: the system would systematically over-flag a group of writers.

3. **Heavily tool-edited human prose.** Text run through Grammarly or a style cleaner loses its burstiness and reads as smoother, more "model-like" prose the LLM may judge as AI — a human author gets pushed toward `ai`.

4. **Paraphrased / "humanized" AI text.** The mirror image: an AI draft passed through a paraphraser re-introduces burstiness and can fool the LLM judgment just as it would a human reader, yielding a false `human`. This is the system's most exploitable hole and the reason the architecture treats appeals as first-class.

5. **Very short submissions (micro-fiction, tweets, comments).** Too few sentences to compute burstiness variance, and too little text for the LLM to judge confidently — the result is unreliable and such inputs should bias toward `uncertain` rather than a confident label.

The honest takeaway: cases 1–3 push humans toward `ai` (false positives — the costliest error), case 4 pushes AI toward `human` (false negatives), and case 5 is simply out of signal range. The wide uncertain band (§2) and the appeals loop (§4) are the deliberate mitigations for exactly these.

---

# AI Tool Plan

How I'll use an AI coding assistant across the three implementation milestones. The pattern is the same each time: **feed it the relevant spec sections from this document so it builds to the design, ask for a narrow deliverable, then verify in isolation before integrating.** I never wire generated code into the app until I've exercised it directly.

## M3 — Submission endpoint + first signal

- **Spec sections I'll provide:** the **Detection Signals** section (specifically Signal 1 — LLM attribution / Groq) and the **Architecture Diagram** (submission flow), so the tool knows the endpoint contract, the `POST /submit → raw text → Signal 1` arrow, and what the signal's `[0,1]` output should look like.
- **What I'll ask it to generate:**
  1. A **Flask app skeleton** — `POST /submit` that validates input (non-empty `text` + `creator_id`, length cap), assigns a content_id, and returns it; plus a `GET /submissions/{id}` results endpoint.
  2. The **first signal function** — `groq_attribution_signal(text)` that sends the text to Groq with a prompt returning a structured assessment (`ai_likelihood`, `assessment`, `indicators`, `reasoning`); the normalized `x1 = ai_likelihood ∈ [0,1]`.
- **How I'll verify:** call `groq_attribution_signal()` **directly** on a handful of inputs *before* wiring it into the endpoint — a clearly AI-style paragraph and a clearly human one — and inspect the structured output. Confirm the score is in `[0,1]` and trends the right way (AI text → higher). Only once the function behaves do I wire it behind `/submit` (returning `content_id` + attribution + placeholder confidence/label, with audit-log entries), then smoke-test the endpoint.

  *Done: Signal 1 separated AI-ish prose (0.8, `likely_ai`) from a human passage (0.2, `likely_human`); the endpoint returns a `content_id` and logs `submission_received` + `signal_1_scored`.*

## M4 — Second signal + confidence scoring

- **Spec sections I'll provide:** the **Detection Signals** section (Signal 2 — burstiness), the **Uncertainty representation** section (§2 — the `p_ai = sigmoid(...)` combination, calibration, and the 0.30/0.70 thresholds), and the **Architecture Diagram** (the `Signal 1 → Signal 2 → Confidence Scorer` arrows and their payloads).
- **What I'll ask it to generate:**
  1. The **second signal function** — `burstiness_signal(text) -> float` returning normalized `x2 ∈ [0,1]`.
  2. The **scoring logic** — a `confidence_score(x1, x2)` that applies the logistic combination to produce `p_ai`, derives `confidence = max(p_ai, 1−p_ai)`, and maps `p_ai` to one of `ai` / `uncertain` / `human` via the configured thresholds.
- **What I'll check:** **do scores vary meaningfully between clearly AI and clearly human text?** Run a small labeled set (a few obvious-AI and a few obvious-human samples) through both signals + the scorer and confirm `p_ai` separates the two groups (AI samples land ≥0.70, human samples ≤0.30, with borderline/edited samples landing in the uncertain band). If the groups don't separate, the weights or normalization need refitting before moving on.

## M5 — Production layer

- **Spec sections I'll provide:** the **Transparency label design** section (§3 — the three exact label variants and their `p_ai` ranges), the **Appeals workflow** section (§4 — who can appeal, the request fields, the status changes, and the audit-log entry), and the **Architecture Diagram** (both the label-mapper step and the entire appeal flow).
- **What I'll ask it to generate:**
  1. The **label generation logic** — `map_label(p_ai, confidence) -> {label_text, icon, predicted_class}` producing the exact 🟡/🟢/⚪ strings with the interpolated confidence percentage.
  2. The **`POST /submissions/{id}/appeals` endpoint** — validates ownership, creates the appeal record, flips `submission.status` to `under_review`, writes the audit-log entry, and returns `{appeal_id, status}`.
- **How I'll verify:**
  1. **All three label variants are reachable** — feed `p_ai` values of 0.85, 0.15, and 0.50 through `map_label()` and confirm I get the AI, human, and uncertain strings respectively (boundary-test 0.70 and 0.30 too).
  2. **An appeal updates status correctly** — submit text, hit `/appeals` as the owner, and confirm the submission flips to `under_review`, an appeal record exists, and an `appeal_filed` audit entry was written; also confirm a non-owner is rejected.
