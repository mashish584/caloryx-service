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

- 🔴 **Nothing has ever *written* to a real Postgres database or made a real OpenAI API call.**
  One exception, added in Chunk 9a: read-only probes (`pg_available_extensions`, `pg_extension`,
  `version()`) were run against the configured Prisma Postgres to confirm pgvector availability.
  No `prisma db push` has been run from here, and no write of any kind. Every chunk's plan named specific
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

## Semantic resolution (Chunk 9a built the infrastructure; 9b reads it)

- ⚪ **No embeddings exist yet.** Chunk 9a ships the columns, the provider wrapper and
  `manage.py backfill_food_embeddings`, but the backfill has never been run — it costs real money
  and needs a key this environment doesn't have. `SEMANTIC_RESOLUTION_ENABLED` is off, and Chunk
  9b's resolution changes are meaningless until the catalog is actually embedded.
- 🟡 **Open Food Facts is excluded from the backfill by default.** It is the largest source by
  far and the least valuable to embed (`_SOURCE_PRIORITY` already ranks it last so plain text
  lands on generic data). `--source open_food_facts` includes it. If branded-dish matching ever
  becomes a goal (PRD §19 lists it as out of scope), this decision is the thing to revisit.
- 🟡 **The HNSW indexes are not declared in `schema.prisma`.** Prisma's `@@index(type:)` supports
  Hash/Gist/Gin/SpGist/Brin only — there is no Hnsw — so unlike `Food_name_trgm_idx` they are
  created by `manage.py ensure_vector_index` in raw SQL, which means a later `prisma db push` can
  see them as drift and drop them. Re-running the command is the fix; it is idempotent. Revisit
  if/when the project adopts `prisma migrate`.
- 🟡 **`EMBEDDING_DIMENSIONS` and the schema's `vector(1536)` are set independently** and cannot
  read each other. `backfill_food_embeddings` compares them against `pg_attribute` before writing
  anything, so a mismatch fails loudly rather than after thousands of paid embeddings — but the
  duplication itself remains.
- 🟡 **A semantic match can never auto-resolve.** `_semantic_band` caps the semantic arm at
  MEDIUM (§12.6's "confirmable"), never HIGH, and a lexical MEDIUM is never displaced by a
  semantic one. This is a deliberate ceiling, not a threshold to tune — revisit only with real
  accuracy data, and only as a product decision about silent auto-resolution.
- 🟡 **The "dal" / "Dal Chawal" property is enforced lexically, not by the floor.**
  `_is_fragment_of` rejects a semantic composite match whose query is a strict word-subset of the
  dish name. That keeps the property deterministic and testable without a real model, but it is a
  blunt rule: a genuine dish whose common shorthand happens to be a strict subset of its own name
  can only be reached by a curator adding it as an alias (which is the §7.6-sanctioned path, and
  it correctly outranks the guard).
- 🔴 **Every semantic similarity floor is uncalibrated** (food resolution, composite matching, and
  Chunk 10's L3 cache). There is no production traffic in this environment to fit them against,
  and §7.4's only guidance is "gate this behind a high threshold", which is not a number. Same
  posture as the T1→T2 router and `T3_ESCALATION_CONFIDENCE_THRESHOLD` above.
- 🟡 **`matchScore` is now band-source-dependent.** Chunk 9b returns the cosine similarity as the
  score when the band came from the semantic arm, and the lexical score otherwise. The two are
  different scales and no column distinguishes them, so any aggregate over `MealDraftItem
  .matchScore` now mixes them. A nullable `matchSource` column (LEXICAL/SEMANTIC) would fix it and
  would also make "how often does the semantic arm fire?" answerable — deliberately not added in
  9b to keep it to one schema change (9a's), but it is the obvious next migration.
- 🟡 **Query embeddings are not memoised.** One message naming three foods that all miss lexically
  makes three embedding calls. Accepted because the semantic arm only runs when the lexical match
  was *not* already HIGH, so the common path spends nothing — but a per-request memo (or Chunk
  10's L3 cache) would remove the rest.
- ⚪ **The golden set cannot demonstrate a successful semantic rescue.** With no live embedding
  model, `tests/test_eval_golden_set.py` asserts only *invariance* — that turning the flag on
  cannot change an answer the deterministic tiers already get right, whether the provider is down,
  the catalog unembedded, or a confidently-wrong neighbour returned. The cases §12.6 cares about
  most (misspellings, regional foods where several entries score closely) still have no passing
  assertion behind them. Same scope boundary Chunk 8e drew for T2/T3.
- 🟡 **The T-1 pre-classifier stays keyword-only.** §7's own diagram specifies "keyword +
  embedding" there too, but `chatparser` is deliberately Django-free and cannot reach a provider
  or the DB. Neither Chunk 9 nor 10 addresses it.

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
