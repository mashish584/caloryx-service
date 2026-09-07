# AI Meal Assistant — Known Issues & Deferred Work

Tracks everything each chunk of the AI Meal Assistant build (see the roadmap in
`documents/CaloryX-AI-Meal-Assistant-PRD.md` and the chunk-by-chunk plan history) deliberately
scoped out rather than guessing at — thresholds with no real data to calibrate against, policy
content pending review, infra decisions bigger than one chunk, or small races accepted on
purpose. Nothing here was missed silently; each item was named in its own chunk's plan at the
time. Update this file as items get resolved or new ones are found — it is the durable record,
not the chunk-by-chunk plan file.

Status legend: 🔴 needs a decision/real work before production · 🟡 accepted tradeoff, revisit if
conditions change · ⚪ blocked on infra/data this environment doesn't have.

## Cross-cutting

- 🔴 **Nothing has ever run against a real Postgres database or a real OpenAI API call.** This
  dev environment has no live DB and no working model budget. Every chunk's plan named specific
  behaviors as "worth confirming by hand" once real infra exists — none of those manual checks
  have actually been performed. This is the single largest standing risk across the whole
  feature; treat every "verified" claim in the plan history as "verified against mocks," not
  against production conditions.

## Concurrency & data races (accepted, documented)

- 🟡 **One-open-draft-per-user** is enforced app-level (transactional lazy-expiry
  check-then-insert), not a DB-level partial unique index. Small race window on concurrent
  multi-device draft creation. Revisit if/when the project adopts `prisma migrate`.
- 🟡 **AI quota window reset race**: two concurrent *first-ever* requests in a freshly-expired
  quota window can both reset to `count=1` instead of one reaching `count=2`, undercounting by
  one unit. Same posture as the draft race above — a usage cap, not a security boundary.

## Escalation & confidence thresholds (no data to calibrate against yet)

- 🔴 **T1→T2 router only escalates on zero parsed phrases**, never a partial match, even though
  the PRD's own framing (§7.1) implies escalating on coverage below a threshold. Needs real
  shadow-mode data (T1 vs. T2 run in parallel against real traffic) to calibrate honestly.
- 🔴 **No real calibrated `parse_confidence` (§7.2)** anywhere — every threshold in the pipeline
  (T1→T2 router, T2→T3 escalation, quantity-ladder defaults) is an honest guess, not tuned
  against real correction data, because no production traffic has ever existed to correct.
- 🔴 **No "ask instead of assume" clarification rule** for the quantity-resolution ladder — it
  always silently assumes a quantity and tags it `est.`, never blocks on a clarifying question.
  Needs real correction-rate data to calibrate an impact threshold.
- 🔴 **No T3 escalation for conversational edits** — `_envelope_confidence` isn't a meaningful
  signal for edit intents that validly have empty `items[]` (e.g. `SET_SLOT`/`REMOVE_ITEM`).
  Needs an edit-appropriate confidence signal design, which needs real data to validate.
- ⚪ **No L3 semantic/embedding cache** — needs its own infrastructure decision (e.g. `pgvector`)
  bigger than "add a cache tier"; never started.
- 🔴 **Cost circuit breaker only trips on consecutive failures, never a cost spike.** A
  spike-based trigger needs a real $ budget number and spend-tracking, neither of which this
  codebase has ever modeled.

## Wellbeing safeguards — the biggest open item in the roadmap

- 🔴 **`_WELLBEING_FLAG_REPLY` and `WELLBEING_RESOURCES` are placeholder content, not reviewed
  or approved.** The PRD explicitly requires clinical/trust-and-safety sign-off before this ships
  for real — none has happened. Do not treat the current copy as launch-ready.
- 🔴 **`WELLBEING_RESOURCES` is a US-only helpline list** (National Alliance for Eating
  Disorders). The PRD itself calls out that this is unacceptable given India is a primary
  market — a locale-aware resource list is required before launch and doesn't exist yet.
- 🔴 **Wellbeing detection never runs on the open-draft/edit path.** Expressing distress mid-edit
  ("actually delete all of this, I don't deserve to eat") is not checked at all today.
- 🟡 **`WELLBEING_CHECK_ALL_MESSAGES` ships off by default** — the broader keyword-net check over
  every T1-successful message only runs if someone explicitly enables the setting.

## Feature gaps named and deferred

- 🔴 "Break into ingredients" (showing the AI's own proposed component breakdown for an
  estimated dish, on explicit opt-in) was never built.
- 🟡 Editing/removing an estimated-dish item **by name** conversationally doesn't work — only by
  id via the structured endpoints, and `PATCH` on one is explicitly rejected (not mishandled).
- 🟡 `DIARY_QUERY` has no real date-range parsing — any trend-shaped question deep-links to
  Insights instead of answering inline.
- ⚪ No per-request locale threaded through message processing anywhere yet — `CompositeFood`'s
  own `locale` field and the cache-key formula's `locale` component are both currently no-ops.

## Privacy, retention & compliance

- 🔴 **PII redaction only covers structured PII** (emails, phone-shaped digit runs) before a
  provider call. Free-text names and health mentions are **not** redacted — needs either an NER
  model or a real product decision, neither of which exists.
- 🟡 `purge_chat_messages` has no scheduler wired up (none exists in this repo) — must be run by
  hand or via an external cron; nothing runs it automatically today.
- ⚪ **Provider zero-retention/no-training contract terms** with the LLM vendor — not started,
  contractual/legal work.
- ⚪ **Encryption & residency, and DPDP Act 2023 / GDPR compliance** — not started, legal/infra
  work outside application code.
- 🟡 Cascade deletion (chat/drafts/operations/parse events) is structurally correct — every
  relevant `onDelete` relation is `Cascade` — but has never been exercised end-to-end, since no
  account-deletion endpoint exists anywhere in the app yet.

## Operational discipline (manual, not automated)

- 🟡 `NORMALIZATION_VERSION`/`PARSER_VERSION`/`NUTRITION_ENGINE_VERSION`/`CatalogVersion` all
  require a human to remember to bump them after a relevant code or catalog change — nothing
  auto-detects the need for a bump.

## Evaluation & CI

- 🔴 **Zero LLM-tier (T2/T3) eval coverage.** The golden set and safety set are restricted to
  deterministic tiers only, since this environment has no live OpenAI budget. Real model
  behavior on descriptive/composite/wellbeing-ambiguous input is entirely untested.
- 🟡 Golden set is ~27 cases and the safety set ~15, versus the PRD's "few hundred" — an honest
  starter, not a finished evaluation program.
- 🔴 **No real CI gate exists.** No `.github/workflows` (or any CI config) exists in this repo —
  the eval set only runs when someone runs `pytest` by hand; nothing enforces it on a PR.
- 🟡 No p50/p90/cuisine-segmented nutrition-MAE reporting — just one flat aggregate number,
  appropriate for a currently-deterministic-only set but not what the PRD's own metric asks for.
