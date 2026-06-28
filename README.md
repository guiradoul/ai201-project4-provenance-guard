# ai201-project4-provenance-guard
Provenance Guard: a backend system that any creative sharing platform could plug into to classify submitted content, score confidence in that classification, surface a transparency label to users, and handle appeals from creators who believe they've been misclassified.

## Contents

- [Architecture Narrative](#architecture-narrative-the-journey-of-one-submission)
- [Architecture Diagram](#architecture-diagram)
- [Running it locally](#running-it-locally)
- [API endpoints](#api-endpoints)
- [Detection signals — and why these two](#detection-signals--and-why-these-two)
- [Confidence scoring — and why this approach](#confidence-scoring--and-why-this-approach)
- [Transparency labels — the three variants](#transparency-labels--the-three-variants)
- [Audit log](#audit-log)
- [Appeals](#appeals)
- [Rate limiting](#rate-limiting)
- [Known limitations](#known-limitations)
- [Spec reflection](#spec-reflection)
- [AI usage](#ai-usage)

## Architecture Narrative: The Journey of One Submission

This is the complete path a single piece of text takes from the moment a creator hits "post" to the label a reader eventually sees — and what happens if the creator disagrees with that label.

### 1. The submission arrives — API Layer (ingestion endpoint)

A creator on the host platform (say, a writing site) submits a story. The platform's backend forwards that text to Provenance Guard through a single POST endpoint (e.g. `POST /submissions`). This endpoint is the front door: it does no analysis itself. Its job is to validate the request (is there actually text? is it within length limits? is the caller authenticated?), assign the submission a unique ID, and hand it inward. It immediately returns that ID so the platform isn't left waiting on the slow part.

### 2. The text is recorded — Datastore (submissions table)

Before anything is judged, the raw submission is written to persistent storage with a status of `pending`. This guarantees nothing is lost, gives us something to attach results to later, and creates the audit trail an appeal will eventually need. Every later component reads from and writes back to this same store.

### 3. The text is judged — Classifier

The heart of the system. The classifier takes the text and decides which class it belongs to — `human`, `ai`, (and optionally `uncertain`/`mixed`). This is where my actual model lives: either a prompted LLM classifier or a fine-tuned model, the same head-to-head setup I used in TakeMeter. It outputs two things: a predicted label and the raw signal behind that prediction (e.g. class probabilities).

### 4. The judgment is scored — Confidence Scorer

A raw label alone ("AI") is dangerous because it hides how sure the system is. The confidence scorer turns the classifier's raw signal into a single 0–1 confidence number. This is what lets the platform treat a 0.92 "AI" differently from a 0.55 "AI." It's a distinct step because the decision logic — including thresholds for what counts as "high confidence" vs. "borderline" — should be reasoned about and tuned separately from the model itself.

### 5. The result becomes something a human can read — Label / Transparency Mapper

The classifier speaks in `human` / `ai` + `0.55`. Readers need plain language. This component maps the (label, confidence) pair onto a user-facing transparency label — for example:

- high-confidence human → 🟢 "High confidence this was human-created"
- high-confidence AI → 🟡 "Shows signals of AI generation"
- low confidence either way → ⚪ "Inconclusive — not enough signal to label"

Keeping this mapping separate means the platform can reword labels or change thresholds without retraining anything. The full result (label, confidence, transparency text) is written back to the datastore and the submission's status flips to `classified`.

### 6. The label is served — API Layer (results endpoint)

The platform fetches the verdict through a GET endpoint (e.g. `GET /submissions/{id}`) and renders the transparency label under the creator's post. This is the label the user finally sees. The flow above is the happy path — everything downstream of here exists because that path is sometimes wrong.

### 7. The creator pushes back — Appeals Handler

A creator who wrote every word but got flagged `ai` clicks "Appeal." A POST endpoint (e.g. `POST /submissions/{id}/appeals`) records their statement, creates an appeal record linked to the original submission, and flips the submission's status to `under_review`. False positives are the central risk of any detection system, so this is a first-class part of the architecture, not an afterthought.

### 8. The appeal is resolved — Review / Resolution flow

The appeal is routed to a moderator (or a re-evaluation step) who upholds or overturns the label. The resolution is written back to the datastore — updating the transparency label the reader sees and closing the appeal. This closes the loop: every label is contestable, and the audit trail from step 2 onward is what makes a fair review possible.

### One-line summary of the chain

API ingestion → Datastore → Classifier → Confidence Scorer → Label Mapper → Datastore → API results endpoint → (if contested) Appeals Handler → Review/Resolution → Datastore.

The guiding principle throughout — straight from the brief — is that Provenance Guard only **informs and labels; it never deletes or punishes**. The host platform decides what to do with the label.

## Architecture Diagram

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

## Rate limiting

The `POST /submit` endpoint is rate-limited with Flask-Limiter, keyed per client (IP). The limits are tiered so a real writer working on their own pieces never hits them, while a script flooding the system is stopped quickly. Each submission also triggers a paid LLM call (Signal 1 via Groq), so the caps double as a cost ceiling.

| Window | Limit | Reasoning |
|---|---|---|
| Per minute | **5** | A writer iterating on one piece (edit → resubmit to see how the label changes) might fire a few requests in quick succession; 5/min absorbs that burst without friction. No human meaningfully submits more than this per minute — a flooding script attempting hundreds/min is cut off here. |
| Per hour | **50** | Covers a long, intensive revision session (many edit-and-resubmit cycles), which is already well beyond casual use. Bounds sustained automated abuse that paces itself under the per-minute cap. |
| Per day | **200** | A generous ceiling for a prolific power user or a small team sharing one key, while still catching a runaway client grinding away all day and capping daily LLM spend. |

Exceeding any tier returns **HTTP 429 (Too Many Requests)**. The limits are configurable without code changes via the `SUBMIT_RATE_LIMIT` environment variable (Flask-Limiter syntax, e.g. `"5 per minute;50 per hour;200 per day"`).

These numbers are starting points chosen to be defensible against real writer behavior and abuse, not tuned against production traffic; they should be revisited once real usage data exists. (Flask-Limiter uses in-memory storage by default — fine for a single-process demo, but a production deployment would point it at a shared store like Redis so limits hold across workers.)

**Rate-limit test** — a burst of rapid `POST /submit` requests from one client. Once the per-minute cap is reached, every further request gets `429`:

```text
202
202
429
429
429
429
429
429
429
```

## Running it locally

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
echo "GROQ_API_KEY=your_key_here" > .env      # Signal 1 calls Groq
.venv/bin/python -m provenance_guard.app       # serves on http://localhost:8000
```

> On macOS, port 5000 is taken by the AirPlay Receiver, so the app defaults to **8000** (override with the `PORT` env var).

```bash
curl -s -X POST http://localhost:8000/submit \
  -H "Content-Type: application/json" \
  -d '{"text": "The sun dipped below the horizon...", "creator_id": "test-user-1"}' \
  | python3 -m json.tool
```

## API endpoints

| Method & path | Purpose |
|---|---|
| `POST /submit` | Ingest text (`text` + `creator_id`), run both signals, score, label, log. Returns the verdict. |
| `GET /submissions/<id>` | Fetch the stored verdict for a submission. |
| `POST /appeal` | File an appeal (`content_id` + `creator_reasoning`); flips status to `under_review` and logs it. |
| `GET /log` | Most recent audit-log entries as JSON (documentation/grading visibility). |
| `GET /health` | Liveness check. |

## Detection signals — and why these two

The classifier rests on **two signals that are different *in kind*** so they fail in different ways. (Implementation: [`signals/groq_attribution.py`](provenance_guard/signals/groq_attribution.py), [`signals/burstiness.py`](provenance_guard/signals/burstiness.py).)

**Signal 1 — LLM attribution (Groq-prompted classifier).** The text is sent to an instruction-tuned model that returns a *structured* assessment: an `ai_likelihood ∈ [0,1]` plus the indicators behind it. **Why this signal:** a capable model has absorbed the stylistic fingerprint of likelihood-maximizing generation (uniform fluency, generic phrasing, missing concrete detail) and can read it *holistically and in context* — it can tell formulaic ad copy from a deliberately plain human poem in a way a single statistic can't. **Blind spots:** it's a judgment, not a measurement (no calibrated probability of its own, can be confidently wrong); it's prompt- and model-version-sensitive; it depends on a paid network call; and it may carry demographic bias (e.g. over-flagging non-native English).

**Signal 2 — Burstiness (sentence-length variation).** A model-free statistic: the coefficient of variation of sentence length. Human prose is *bursty* (a long winding clause, then a three-word punch); AI prose trends toward uniform rhythm. **Why this signal:** it's cheap, local, and — crucially — has a *completely different failure mode* from Signal 1, so it provides a genuine second opinion rather than a correlated one. **Blind spots:** heavy editing (Grammarly) flattens human burstiness; AI told to "vary sentence length" can fake it; it needs several sentences to mean anything.

**Why pair them rather than rely on one:** an input that fools the LLM (a paraphrased "humanized" AI draft) may still look statistically uniform, and an input that fools burstiness (a lightly edited human piece) may still read as human to the LLM. Two different lenses catch more than one lens used twice. A third, fully model-independent statistical signal — **perplexity** — is implemented in [`signals/perplexity.py`](provenance_guard/signals/perplexity.py) and kept as an optional future leg.

## Confidence scoring — and why this approach

(Implementation: [`scorer.py`](provenance_guard/scorer.py).)

A bare label ("AI") is dangerous because it hides *how sure* the system is. The scorer turns the two signal scores into one probability and a confidence:

```
p_ai = sigmoid(w0 + w1·x1 + w2·x2)        confidence = max(p_ai, 1 − p_ai)
```

**Why logistic regression** rather than a hand-picked weighted average: it outputs a probability natively, its weights can be *learned* from labeled data, and it stays inspectable — you can read off which signal carries more weight. Signal 1 is weighted higher than Signal 2 because the LLM judgment is the stronger evidence; burstiness mostly nudges within a band.

**Why these thresholds** (`p_ai ≥ 0.70 → ai`, `≤ 0.30 → human`, else `uncertain`): the **uncertain band is deliberately wide** because the costliest error is a confident *false "AI"* on a real creator. When the signals disagree or are weak, the system should say "I don't know," not guess. `confidence = max(p_ai, 1−p_ai)` means a confidence of 0.6 reads as a near-coin-flip — it lands in the uncertain band by construction.

**Two real examples** (scores from Milestone 4 testing — different inputs, noticeably different confidence):

| Submission (excerpt) | Signal 1 (Groq) | Signal 2 (burst) | `p_ai` | Confidence | Label |
|---|---|---|---|---|---|
| *"In today's fast-paced world, effective communication is crucial. It fosters strong relationships. It drives meaningful outcomes…"* | 0.80 | 0.80 | **0.953** | **0.95** | 🟡 Likely AI |
| *"Not sure how I feel about the move yet. Some days it feels right. Other days, less so."* | 0.40 | 0.70 | **0.475** | **0.53** | ⚪ Uncertain |

The first is high-confidence (both signals agree the text is uniform and generic); the second is genuinely borderline and the system *declines to commit* — exactly the variation the design is meant to produce, not a constant.

**What I'd change for a real deployment:** (1) **fit the weights on labeled data** instead of the current hand-picked placeholders; (2) **calibrate** the probabilities (Platt scaling, measured with a reliability diagram + Expected Calibration Error) so a reported 0.6 really means 60%; (3) **per-genre thresholds**, since uniform technical/reference writing is naturally low-burstiness regardless of author; (4) **add the independent perplexity signal** so a single paraphrasing attack can't degrade everything at once; (5) **special-case short inputs**, which don't give either signal enough to work with.

## Transparency labels — the three variants

The label mapper ([`labeler.py`](provenance_guard/labeler.py)) renders one of exactly three variants. Each has an icon, a primary line, and a secondary line; the AI/human variants interpolate the confidence as a percentage so the label never overstates certainty.

**High-confidence AI** (`p_ai ≥ 0.70`):
> 🟡 **Shows signals of AI generation**
> Our automated check is {confidence}% confident this text was largely AI-generated. This is a signal, not a verdict — the creator can appeal.

**High-confidence human** (`p_ai ≤ 0.30`):
> 🟢 **High confidence this was human-created**
> Our automated check is {confidence}% confident this text was written by a person. No AI-generation signals stood out.

**Uncertain** (`0.30 < p_ai < 0.70`):
> ⚪ **Inconclusive — not enough signal to label**
> Our automated check couldn't confidently tell whether this was human- or AI-written. No claim is being made either way.

Example rendered AI label (confidence 95%): `🟡 Shows signals of AI generation — Our automated check is 95% confident this text was largely AI-generated. This is a signal, not a verdict — the creator can appeal.`

## Audit log

Every submission writes one structured JSON-Lines entry ([`audit.py`](provenance_guard/audit.py)) capturing **both signal scores alongside the combined confidence**; appeals append an `appeal_filed` entry that embeds the original classification being contested. Surfaced via `GET /log`. Three real entries:

```json
{"content_id": "71e23b7f...", "timestamp": "2026-06-28T03:31:08.292Z", "creator_id": "alice", "signal_1_score": 0.8, "signal_2_score": 0.80, "p_ai": 0.953, "confidence": 0.953, "attribution": "ai",        "status": "classified"}
{"content_id": "791e2176...", "timestamp": "2026-06-28T03:31:08.839Z", "creator_id": "bob",   "signal_1_score": 0.2, "signal_2_score": 0.27, "p_ai": 0.057, "confidence": 0.943, "attribution": "human",     "status": "classified"}
{"content_id": "b9bb95f3...", "timestamp": "2026-06-28T03:31:09.397Z", "creator_id": "carol", "signal_1_score": 0.3, "signal_2_score": 0.70, "p_ai": 0.310, "confidence": 0.690, "attribution": "uncertain", "status": "classified"}
```

## Appeals

`POST /appeal` accepts a `content_id` and the creator's `creator_reasoning`. It flips the submission's status to `under_review`, creates an appeal record, and logs the appeal **alongside a snapshot of the original classification** (attribution, `p_ai`, confidence, both signal scores, label text) so a reviewer sees both the verdict and the objection. It does **not** re-classify or change the label — resolution is a separate human step. The original verdict is never silently altered: Provenance Guard only informs and labels.

## Known limitations

These are honest failure modes tied to *how the signals work*, not generic "needs more data":

- **Repetitive, simple-vocabulary verse (poetry, song lyrics, nursery rhymes) is mislabeled `ai`.** Uniform short lines give *low burstiness* (Signal 2 reads it as machine-uniform), and the plainness leads the LLM (Signal 1) to read it as generic/templated. Both signals lean the same way, so a genuinely human poem like *"Row, row, row your boat"* gets flagged. This is a direct consequence of burstiness measuring *only* structural variation and the LLM keying on plainness — both proxies the form happens to trip.
- **Non-native / ESL and formulaic human writing is over-flagged.** Templated prose uses common, high-frequency constructions → low burstiness, and LLM detectors are documented to over-flag non-native English. The two failures compound into a false `ai` — a real fairness risk, since it systematically penalizes a group of writers.
- **"Humanized" / paraphrased AI text slips through as `human`.** A paraphrase pass re-introduces burstiness *and* can fool the LLM the same way it fools a human reader — defeating both signals at once. This is the most exploitable hole, and the reason appeals are first-class.
- **Very short submissions** don't give burstiness enough sentences or the LLM enough text; the result is unreliable and should bias toward `uncertain`.

## Spec reflection

**One way the spec guided the implementation.** Writing the detailed design up front in [`planning.md`](planning.md) — exact thresholds (0.30/0.70), the three label strings verbatim, and an architecture diagram whose arrows are *labeled with what passes between components* — made the build deterministic and verifiable. The label mapper and scorer share a single `classify()` so thresholds have one source of truth, the audit-entry schema came straight from the diagram's payload labels, and every milestone could be checked against the written spec (e.g. the label function was asserted against the exact headlines).

**One way the implementation diverged.** The spec originally defined **Signal 1 as perplexity under a reference model (GPT-2)**. In practice I switched it to a **Groq-prompted LLM classifier**, because true per-token perplexity requires a model exposing token log-likelihoods (a heavy `torch`/GPT-2 dependency), while the project was already set up with Groq — and the architecture narrative's step 3 explicitly allowed "a prompted LLM classifier." I repositioned perplexity as an optional future signal and updated planning.md to match. (The appeal endpoint also diverged — `POST /appeal` with `creator_reasoning` rather than the path-based `statement` route — to match the diagram and a later instruction.)

## AI usage

This project was built with an AI coding assistant. Specific instances where I directed it, reviewed the output, and revised:

1. **First signal + endpoint.** I directed the AI to generate the Flask skeleton and Signal 1. It first produced a **GPT-2 perplexity** implementation faithful to the spec. I **overrode** that choice in favor of a Groq-prompted classifier — true perplexity needed `torch`/GPT-2 (hundreds of MB) and the repo was already wired for Groq — and had it restructure the signal behind a swappable interface, keeping the perplexity module as an optional leg.
2. **Second signal + scorer.** I asked it to generate burstiness and the confidence scorer *and verify the thresholds against the spec*. It produced working code with **hand-picked placeholder weights**; my review caught that a firm `human` verdict required *both* signals to strongly agree (uniform human writing got pushed to `ai`). Rather than silently accept it, I flagged this as a **calibration limitation** and recorded it in Known Limitations and the "what I'd change" list.
3. **Appeal endpoint.** The AI's first version used a `statement` field with a mandatory ownership (`creator_id`) check, per the original planning doc. I **revised** it to the simpler spec given later — `content_id` + `creator_reasoning`, ownership optional — and specifically directed it to log the appeal *alongside the original classification snapshot* in the audit entry, which it did not do in the first pass.
4. **Rate limits.** The AI initially applied an arbitrary `30 per minute`. I directed it to choose **defensible, tiered limits tied to real writer behavior** and document the reasoning; it produced the 5/min · 50/hr · 200/day tiers and the README table.
