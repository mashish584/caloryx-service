# CaloryX — AI Meal Assistant (Chatbot Logging) PRD

| | |
|---|---|
| **Feature** | Conversational meal logging ("Meal Assistant / Instant Logging") |
| **Status** | Draft for review — aligned to Meal Logging design v1 |
| **Owner** | _TBD_ |
| **Version** | 1.6 |
| **Platform** | React Native (Expo) · TypeScript · REST · Prisma/PostgreSQL · Python (parser + nutrition engine) |
| **Related** | Meals Listing & Details PRD, Onboarding PRD, Calorie/Macro Engine, Home/Dashboard |

> **Core product principle (adopted from the attached flow doc):**
> **AI interprets what the user ate; the nutrition database determines what it contains.**
> The LLM never produces a calorie or macro number. It only produces *structured intent* —
> food, quantity, unit, state, preparation. Every number the user sees comes from the
> deterministic nutrition engine.

> **v1.6 (this version):** contract-level fixes before development. Reconciles the **streaming
> contract** (v1 is single-response everywhere; the typing indicator is client-driven); ties the
> **idempotency TTL to the offline replay window** so a day-5 sync can't duplicate; adds an
> **`ItemResolution` discriminator** with per-state field requirements and persisted
> estimated-dish range fields; specifies **transactional lazy expiry** for `OPEN → EXPIRED`; and
> defines the **full `QueuedOperation` payload** including local→server ID mapping.

> **v1.5:** second architecture review. Defines **where the estimated-dish number
> comes from** — a deterministic `DishCategoryProfile`, shown as a range (§7.6.1) — closing a
> contradiction introduced in v1.4. Enforces **one open draft per user** via a partial unique
> index, **persists message idempotency** (`clientMessageId` + `IdempotencyRecord`), and adds
> **§12.12 offline queue/sync** and **§12.13 incomplete-parse policy**. Removes `yieldFactor` from
> `CompositeFood` to make double-application impossible; keys `UserServingPreference` by state and
> unit; adds **§10.1 sample response contracts**. Fixes the §7.6/§13 inconsistency.

> **v1.4:** incorporates architecture review. Adds **§12 Engineering Contracts &
> Quality Gates** (concurrency, idempotency, draft state machine, catalog versioning, validation
> layer, server authority, resolution confidence bands, cache versioning, atomic quota,
> observability, evaluation framework, accuracy & safety metrics, privacy/retention). Splits item
> provenance into `quantitySource` + `massSource` (§5.1.1a); expands the composite-food model and
> replaces auto-decomposition of unknown dishes with a **single estimated-dish item** (§7.6);
> persists `prep`/`sizeQualifier`; **drops response streaming for v1** (§10).

> **v1.3:** non-meal messages were collapsed into a single `OTHER` bucket with
> one canned redirect. Replaced with a **13-intent taxonomy** (§5.5) covering social, app help,
> diary queries, bounded nutrition Q&A, advice-seeking, unclear input, and adversarial input —
> most resolving at T-1 with no model call. Added **§5.6 wellbeing safeguards**, which the doc
> previously had no handling for at all. Companion doc: *Conversation Scenarios & Expected
> Outputs*.

> **v1.2:** an audit of what the LLM is actually responsible for (**§16.3**)
> found the model's contract covered the *first message* but not the *conversation*. Fixes:
> the response schema is now an **intent envelope** supporting edits, slot, and size qualifiers
> (§7.3) with **intent dispatch** and a **T1 edit grammar** (§7.5); **meal naming is
> deterministic** so it can't force calls onto free paths (§5.1.3); a **T-1 non-food
> pre-classifier** stops chit-chat reaching a model (§7.1); **compound dishes move to a
> `CompositeFood` catalog lookup** instead of T2 (§7.6); and **draft edits are quota-exempt**
> (§5.1.4). Schema, states, and analytics updated to match.

> **v1.1:** adds **§17 UI To-Dos** — a prioritized design backlog covering the
> chat screen, the Adjust Portion screen, the three components that don't yet exist, and the
> accessibility items. Also incorporates the quantity-resolution ladder and unit-aware portion
> editing decisions into §5.1.1 and §5.2. Sections 16 and 17 of v1.0 are renumbered 17 and 18.

---

## 1. Overview

The Meal Assistant is a chat surface where a user describes a meal in natural language and gets
a fully itemized, editable meal preview back — then logs it in one tap. It is the fastest path
in the product from "I ate something" to "it's in my diary," which makes it a direct lever on
**priority #1 (meal-logging speed)** and **priority #2 (nutrition accuracy)**.

This PRD covers two screens from the Meal Logging design:

1. **Meal Assistant (Chat)** — conversational input, AI interpretation, itemized meal card, confirm & log.
2. **Adjust Portion** — per-ingredient portion editor with slider, quick-select multipliers, and live nutrition deltas.

It also specifies the **backend pipeline** that sits behind the chat, because the single biggest
risk in this feature is not the UI — it is calling an LLM on every message, which is slow,
expensive, and non-deterministic. Section 7 defines a tiered router that keeps the LLM as an
*exception layer*, per the attached flow, with several changes recommended in Section 15.

**Entry points:** the center FAB on the Home/Meals bottom nav, and a "Describe your meal" affordance
from the Meals listing empty/no-results state (see Meals PRD §5.1).

---

## 2. Goals & Non-Goals

**Goals**
- Let a user log a multi-item meal from one sentence, in under ~10 seconds.
- Keep nutrition numbers deterministic, explainable, and reproducible — never model-generated.
- Make AI interpretation *correctable* without restarting the conversation.
- Keep per-log AI cost near zero for the common case (repeat meals, simple quantified inputs).
- Persist the meal as **individual food items**, not a single calorie total, so it stays editable
  and feeds Insights.

**Non-Goals (this PRD)**
- Photo/camera food recognition (separate PRD; shares the same preview + confirm surface).
- Barcode scanning (covered by Meals PRD).
- Open-ended nutrition Q&A, coaching, or diet advice in the chat. A **narrow, catalog-sourced** band of factual food questions is in scope; judgement and advice are not (§5.5).
- Recipe generation or meal planning.
- Multi-turn memory across sessions ("what did I eat yesterday?") — see §19.

---

## 3. Success Metrics

| Metric | Target intent |
| --- | --- |
| **Median time-to-log** (chat opened → meal logged) | ≤ 10s; primary KPI |
| **Parse acceptance rate** | % of previews logged with **zero** edits — target ≥ 70%. A satisfaction proxy, **not** an accuracy measure (§12.11) |
| **Nutrition MAE (golden set)** | Mean absolute error on kcal and each macro, p50/p90, by tier and cuisine — the real accuracy gate |
| **Intent classification accuracy** | Across all 13 intents (§5.5); `WELLBEING_FLAG` false-negative rate tracked separately |
| **Correction rate** | % of previews with ≥1 portion/item edit before logging — health signal, not failure |
| **Abandon rate** | % of previews shown but never logged — target < 15% |
| **LLM call rate** | % of user messages that reach a model call — **target ≤ 30%** |
| **Cost per logged meal** | Tracked as a first-class metric; target < ₹0.15 / ~$0.002 |
| **p50 / p95 response latency** | ≤ 1.2s / ≤ 3.5s from send → preview rendered |
| **Unmatched-food rate** | % of parsed items with no DB match — drives catalog backlog |
| **D7 retention lift** vs. non-AI loggers | Does conversational logging retain better? |

---

## 4. Users & Key Use Cases

| User | Need on this surface |
| --- | --- |
| Gym-goer / muscle gain | Log precisely weighed foods fast ("200g chicken, 150g rice"); trusts the macros |
| Weight-loss user | Log an unmeasured restaurant/home meal without a food scale |
| Beginner | Doesn't know serving sizes or macro names; needs the app to make a sane guess |
| Indian / regional-cuisine user | Household measures ("1 katori dal", "2 rotis") that Western catalogs miss |

**Primary use cases**
- *Fully quantified:* "I ate 200g cooked rice with 100g chicken breast." → no AI needed.
- *Descriptive:* "Grilled chicken salad with quinoa and a tahini dressing." → AI interprets, DB calculates.
- *Vague quantity:* "Had a bowl of chicken curry with rice." → assume a default, flag it as estimated.
- *Correction:* "Chicken was actually 150g" or tap the item → recalculates in place.
- *Incremental:* "Add a boiled egg" → appends to the current preview, no restart.

---

## 5. Screens & Requirements

### 5.1 Meal Assistant (Chat)

**Purpose:** Take a natural-language description and return an itemized, editable meal preview.

**Header**
- Back arrow.
- App-mark avatar + title **Meal Assistant**, subtitle **Instant Logging**.
- **Quota pill** — `⚡ 0/20` (see §5.1.4 — semantics need decisions).

**Message stream**
- **Assistant greeting** (first open of a session): "Hi {firstName} 👋 What did you have? You can just describe it — I'll figure out the calories."
- **User bubble** — right-aligned, brand green, white text, timestamp below-right.
- **Assistant bubble** — left-aligned, neutral grey surface, timestamp below-left.
- Timestamps render on the first message of a time cluster, not every bubble.

**Meal preview card** (the core output — rendered as an assistant-side attachment)
- Card title = **AI-generated meal name** (e.g., "Chicken Quinoa Bowl"), with a calorie pill (`425 kcal`).
- **Item rows**, one per identified food:
  - Food name (`Grilled Chicken Breast`)
  - Resolved quantity + unit as a subtitle (`120g`)
  - Item calories, right-aligned (`198`)
  - Chevron → opens **Adjust Portion** (§5.2)
- Preceded by a short assistant line: "Got it — let me break that down. Tap any item to adjust the portion."
- **Primary CTA: `Confirm & Log`**, full-width green, pinned directly beneath the card.

**Composer**
- Rounded input, placeholder `Tell me what you ate...`, circular green send button.
- Send disabled while empty or while a parse is in flight.
- Keyboard-avoiding; composer and the active preview CTA must never overlap (see §15, Gap G6).

#### 5.1.1 Core behaviors

- **Client-side progressive feedback, single server response.** The typing indicator renders immediately on send and is driven entirely by the client — it needs no server signal. The preview card paints once, when the single `POST /messages` response arrives (§10). **v1 has no streamed responses anywhere in the system.**
- **The preview is a live, editable draft**, not a static message. It is bound to a `MealDraft` on the server, and every edit mutates that draft in place. Older previews in the scrollback become read-only once superseded.
- **Text-based editing must work**, not just tapping. "Chicken was actually 150g", "remove the dressing", "add a boiled egg" all mutate the current draft. Every draft mutation re-renders the *same* card rather than appending a new one.
- **One draft at a time.** A brand-new meal description while a draft is open prompts: *Add to this meal* / *Start a new meal*.
- **Idempotent logging.** `Confirm & Log` sends an idempotency key; double-taps cannot create duplicates.
- **Optimistic confirm.** On tap, immediately show success and update Home totals; reconcile in background, roll back with a clear toast on failure.
- **Estimated-quantity marking.** Any item whose quantity the system *assumed* rather than parsed renders with an `est.` chip and a distinct subtitle, so the user knows exactly which numbers to check. This is the mechanism that lets us avoid blocking clarification questions (see §15, Improvement I3).

#### 5.1.1a Resolving a missing quantity

When the user names a food without a quantity, resolve via this ladder — first match wins:

1. **This user's history for this food** — median of their last 5–10 logs. Require ≥3 observations before it overrides the catalog, and use median not mean so one outlier log doesn't anchor everything.
2. **Size qualifier in the text** — "small bowl", "large plate", "a bit of" → a 0.7× / 1.0× / 1.4× modifier on the base serving.
3. **The food's canonical serving** — USDA serving, or package serving for branded items.
4. **Category fallback** where the food has no serving data — cooked grain 150g, meat 100g, leafy veg 80g, dressing 20g, oil 5g.

Provenance is tracked on **two independent axes**, because "was an amount stated?" and "how did we
turn it into grams?" are different questions with different error profiles (see §17, U2):

**`quantitySource`** — was an amount stated?

| Value | Example |
|---|---|
| `EXPLICIT` | "200g rice", "2 rotis", "1 katori dal" — an amount was given |
| `ASSUMED` | "rice" — nothing stated |

**`massSource`** — how was it converted to grams?

| Value | Example |
|---|---|
| `DIRECT` | Already a mass — "200g rice" |
| `HOUSEHOLD_TABLE` | "1 katori" → 150g via the per-food measure table |
| `USER_HISTORY` | This user's median portion (ladder step 1) |
| `CATALOG_SERVING` | The food's canonical serving |
| `CATEGORY_FALLBACK` | Generic category default — weakest signal |

The pair drives the UI: **`est.` chip iff `quantitySource = ASSUMED`**; the provenance line is
written from `massSource`. So "1 katori of dal" shows the household unit plainly with **no**
`est.` chip — the count is certain — while its gram conversion is still tracked as approximate for
accuracy reporting. Under a single combined field these were indistinguishable, which made
"how wrong are our household measure conversions?" unanswerable.

Show provenance under the value — "Your usual portion" / "Typical serving". It costs nothing and explains why the number is what it is.

**When to ask instead of assume.** Trigger a clarification on **impact, not ambiguity**. A missing quantity on spinach is a ±15 kcal problem; on ghee or peanut butter it's ±300 kcal. Assume silently when the plausible range stays within ~20% or ~150 kcal of the meal total; ask only when it doesn't. This keeps blocking questions rare enough not to hurt logging speed while still catching the cases that would make the log wrong.

**Corrections feed back.** Every portion edit writes to a per-user serving profile (`UserServingPreference`, §9). This is what makes the feature improve rather than stay annoying: after a few weeks, "rice" resolves to *this user's* rice portion, the `est.` chip appears less often, and correction rate falls on its own.

#### 5.1.2 Meal slot assignment

The design does not show a meal-slot selector, but every logged meal needs one (`BREAKFAST | LUNCH | DINNER | SNACK`, per Meals PRD).

- **Default:** infer from device local time using configurable windows (e.g., 04:00–10:59 Breakfast, 11:00–15:59 Lunch, 16:00–21:00 Dinner, else Snack).
- **Override:** the slot renders as a small tappable chip on the preview card header ("Lunch ▾"). One tap to change.
- Explicit user text ("for breakfast I had…") overrides the time-based default.

#### 5.1.3 Meal naming

**Naming is deterministic by default and must never trigger an LLM call.** Since 55–70% of
parses resolve at T0/T1 without touching a model (§7.1), an LLM-dependent naming step would
force a call onto the zero-cost paths and quietly break the cost model.

- **Default — template.** Derive from the resolved items: dominant protein/grain + form factor
  ("Chicken Quinoa Bowl"), or `{Slot} — {top item}` for simple meals ("Lunch — Chicken Breast").
  Max 4 words, title case.
- **Optional — model-suggested.** When a message already goes to T2/T3 for other reasons, the
  envelope's `mealName` field free-rides on that call (§7.3) and takes precedence over the
  template. It is never worth a call on its own.
- **Cached names reuse the stored name**, so a repeat meal keeps its identity across logs.
- No adjectives implying nutrition claims ("Healthy", "Lean", "Guilt-free").
- User-editable via long-press on the card title (nice-to-have; not blocking v1).

#### 5.1.4 Quota pill (`⚡ 0/20`)

Semantics are undefined in the design and need a product decision. **Recommendation:**

- The counter tracks **AI-assisted parses**, not messages. Cache hits and deterministic-parser hits **do not decrement it**. This aligns the visible quota with actual cost and rewards the fast path.
- **Exempt: corrections to an open draft.** `EDIT_ITEM`, `ADD_ITEM`, `REMOVE_ITEM`, and `SET_SLOT` never decrement the quota, even when they reach T2. Charging a user to fix the assistant's own mistake is a perverse incentive — it pushes people back to tapping and makes the feature feel punitive precisely when it erred. Edits are bounded (one draft, few items) and mostly resolve at T1 via the edit grammar (§7.5), so the cost exposure is small.
- **Exempt: non-food messages**, which are caught by the T-1 pre-classifier before any model call (§7.1).
- Scope: **20 `LOG_NEW` AI parses per rolling 24h** for free tier; unlimited (fair-use capped) for premium.
- Display: `⚡ {used}/{limit}`, turning amber at ≥80% and red at 100%.
- At limit: chat stays usable for quantified inputs via the local parser; descriptive inputs surface an upgrade sheet rather than a hard block. This is important — a hard block on the fastest logging path damages retention (#3) to protect premium (#5), which inverts our priority order.
- Tooltip on tap explaining what counts against the quota.

---

### 5.2 Adjust Portion

**Purpose:** Change one item's quantity and see the nutrition impact before committing.

**Header:** back arrow · title **Adjust portion** · subtitle = item name · **info (ⓘ)** button, top-right.

**Portion Size card**
- Large numeric value + unit (`150 g`).
- Circular `−` and `+` steppers flanking the value.
- Delta caption beneath: `+30g from default`.
- Horizontal slider with a draggable thumb.

**Quick Select** — four multiplier chips relative to the AI-resolved default: `1/2` · `1x` · `1.25x` · `2x`, each with its resolved gram value beneath. The chip matching the current value renders selected (green tint + border).

**Updated Nutrition** — a four-cell row: `kcal` · `Protein` · `Carbs` · `Fat`, each showing the new value and a **signed delta vs. the default** with a directional arrow.

**Primary CTA:** `Update Portion` — writes back to the draft and returns to chat.

#### 5.2.1 Behaviors and rules

- **All recalculation is client-side pure math.** Nutrition is linear in quantity, so the client holds `per-unit` nutrient vectors for the item and recomputes locally. **No network request on slider drag, stepper tap, or chip select.** A drag firing a request per frame would be the single worst performance and cost decision in this feature.
- **Client values are preview only; the server stays authoritative.** It recomputes on persist and its value wins on any discrepancy beyond a rounding epsilon (§12.5).
- **Quick-select chips are true multipliers of the default.** If default = 120g, then `1/2` = 60g, `1x` = 120g, `1.25x` = 150g, `2x` = 240g. (The design shows `1/2 = 75g` against a 120g default, which is 0.625× — see §15, Gap G2.)
- **Deltas are computed, never authored.** Every delta = `new − default`, with the arrow direction derived from the sign. Increasing a portion can never show a decreased macro. (See §15, Gap G1.)
- **Slider bounds:** `0.25×` to `3×` of default, clamped; steppers move in unit-appropriate increments (5g for gram-based, 0.25 for `unit`/`slice`/`cup`-based). Values outside the slider range remain reachable via steppers.
- **Unit awareness is driven by a unit set on the food**, not by a unit string on the item:

  ```ts
  food.displayUnits = [
    { unit: "katori", grams: 150, type: "household"   },
    { unit: "cup",    grams: 158, type: "household"   },
    { unit: "g",      grams: 1,   type: "continuous"  },
  ]
  ```

  Grams stay canonical for all nutrition math; display is whatever the user thinks in. The screen **opens in the unit the user spoke in** — if they said "a bowl of rice", a gram slider makes them do a conversion the app should be doing. Grams remain visible as secondary text in every case.
- **`unitType` drives the control**, because a single slider is wrong for two of the three cases (full matrix in §17.2): continuous → slider + steppers; **countable → steppers only, no slider** (sliding to "1.37 eggs" is nonsense); household → steppers stepping 0.25.
- **Switching units converts, never resets.** 150g → switch to cup → 0.95 cup, snapped to 1 cup, with the gram equivalent updating so the user sees what the snap did.
- **Info (ⓘ) button** opens a sheet with the **nutrition source** for this item — database (USDA / Open Food Facts / CaloryX curated), the reference entry name, per-100g basis, and a "Report incorrect data" link. This is our trust and correction pipeline; it should not be a generic help screen.
- **Cancel semantics:** back arrow discards; only `Update Portion` commits.

---

### 5.5 Non-logging messages

The chat invites open-ended input ("just describe it — I'll figure out the calories"), so users
will inevitably send things that aren't meals. Collapsing all of that into one canned redirect
makes the assistant feel broken, and in one case (§5.6) it means ignoring a signal that matters.

**Design principle:** every non-logging reply is **short, scripted, and ends by pointing back to
logging or to the right surface**. The assistant does not become a general chatbot — but it does
not stonewall either.

| Intent | Example | Response | Tier |
|---|---|---|---|
| `SOCIAL` | "hi", "thanks", "good morning" | One warm line + nudge: "Hey 👋 What did you eat?" | T-1, no LLM |
| `APP_HELP` | "how do I change my calorie goal?", "how does the streak work?" | Short answer + deep link to the setting or Help Center. **Never** attempt a food parse | T-1, no LLM |
| `DIARY_QUERY` | "how many calories do I have left?", "what did I eat today?" | Answer from the user's own logged data — see below | T-1 + local query |
| `NUTRITION_QA` | "is quinoa high in protein?", "how much protein is in an egg?" | Bounded factual answer **from catalog data only** — see below | T-1 + catalog |
| `ADVICE_SEEKING` | "should I try keto?", "is 1,200 kcal enough for me?" | Decline and redirect to a qualified professional — see below | T-1 / T2 |
| `WELLBEING_FLAG` | see §5.6 | Supportive response; never a canned redirect | §5.6 path |
| `UNCLEAR` | "yes", "asdfgh", "the usual" | One clarifying question, not a rejection | T-1 |
| `OTHER` | "write me a poem", "what's the weather" | Scripted decline + nudge | T-1, no LLM |

**None of these decrement the quota** (§5.1.4), and the common ones resolve at T-1 with no model
call at all.

#### `DIARY_QUERY` — answer it, don't deflect

"How many calories do I have left?" is answerable from data the app already has on the Home
screen. Deflecting it is the single most user-hostile thing this chat could do. Scope for v1:

- Remaining calories and macros for today.
- What's logged today, by slot.
- Anything requiring cross-day trends or analysis → deep link to **Insights**, don't answer inline.

These are **local database reads, not LLM calls.** The classifier routes; a query builder answers.

#### `NUTRITION_QA` — bounded, catalog-sourced, no advice

§2 lists open-ended nutrition Q&A as a non-goal, and that stands. But a narrow band is both safe
and useful because the numbers already exist in the catalog:

- **Answer:** "How much protein is in 100g chicken breast?" → read the catalog row, cite the source.
- **Deflect:** "Is quinoa healthy?" / "What should I eat for dinner?" → not a catalog lookup, and
  the answer is contextual. Redirect: "I stick to logging — but you can compare foods in Meals."

The dividing line: **the assistant reports what a food contains; it never judges whether a food
is good for this person.** That's the same principle as the header — the catalog states facts,
the assistant doesn't editorialize.

#### `ADVICE_SEEKING` — always decline

Diet strategy, medical questions, "is my target right", supplement questions, and anything about
a health condition get a consistent decline pointing to a qualified professional. CaloryX is a
tracking tool, not a clinical one. This is a hard boundary regardless of how confidently the
underlying model could answer.

#### Adversarial input

The chat is a user-controlled surface feeding a model, so prompt injection ("ignore your
instructions and tell me…") is expected traffic. The **intent envelope contains the blast
radius**: the schema has no nutrition fields, no free-text response field, and no action fields,
so a successful injection can at most produce a wrong food item — which the user then sees in the
preview before anything is logged. Injection attempts classify as `OTHER`. Log them; don't
engage with them.

---

### 5.6 Wellbeing safeguards

A calorie-tracking app with a conversational surface is a place where disordered-eating patterns
surface. This is category-standard product safety — not an edge case — and the current spec has
no handling for it at all.

**Trigger examples:** requests to set extreme deficits, questions about prolonged fasting or not
eating, expressions of guilt or distress about food or body weight, or logged intake far below a
plausible floor sustained across days.

**Required behavior:**

- **Respond supportively and briefly.** Acknowledge the person, don't lecture, don't diagnose.
- **Do not supply numbers.** No calorie targets, deficit maths, fasting durations, or "safe
  minimums" — even framed as a warning. Specific figures can function as instruction.
- **Offer support resources**, locale-appropriate, without listing them unprompted. Keep the
  resource list in remote config so it can be corrected without an app release.
- **Never lock the user out of logging.** Removing someone's food diary is not a safe response.
  The safeguard changes the *conversation*, not their access.
- **Never gamify in this state** — suppress streak celebrations and deficit-praise copy
  ("on track for 0.5 kg/week!") for the session.
- **Escalate to T3 rather than T-1.** A keyword classifier is too blunt here; false negatives
  matter more than cost. This is the one place in the pipeline where we deliberately spend.

**Note on resources:** if the build ships a default US resource list, **NEDA's helpline is
permanently disconnected** — use the National Alliance for Eating Disorders helpline instead.
For India, resources must be sourced separately; a US-only list is not acceptable for a market
where India is a primary segment.

This section needs review by someone with clinical or trust-and-safety background before build.
It is written here as a requirement, not a finished policy.

---

## 6. Cross-Cutting Functional Requirements

- **Ingredients are the source of truth.** Meal totals are always the sum of item nutrition at current quantities — never an independently stored number that can drift. (Consistent with Meals PRD §7.)
- **The saved meal stores individual items**, their quantities, units, resolved food IDs, and a nutrition snapshot — so it can be edited later and analyzed in Insights.
- **Parity with the Meals surface.** A meal logged via chat is indistinguishable downstream from one logged via search: same `LoggedMeal` record, same detail screen, same edit/delete affordances, plus a `source` field for analytics.
- **Draft persistence.** An unconfirmed draft survives app backgrounding and is restored on return, with a "You have an unlogged meal" prompt. It expires after 24h.
- **Offline.** Quantified inputs that hit the on-device cache resolve and queue offline; descriptive inputs requiring AI show a clear offline state rather than failing silently.
- **Never hallucinate a food.** If no DB match clears the confidence threshold, the item renders as unresolved with an explicit "Search for this food" or "Add manually" action. An LLM-invented food with invented nutrition is a correctness failure, not a graceful degradation.

---

## 7. Pipeline Architecture

The attached flow's central insight — route around the LLM whenever possible — is right and is
adopted. This section makes it concrete and adds two tiers the original flow doesn't have, plus
a pre-classifier and a deterministic composite-food layer (see §16.3).

```
                        User message
                             │
                             ▼
              ┌──────── Normalize & hash ────────┐
              │  lowercase, unit-normalize,       │
              │  strip filler, canonical order    │
              └──────────────┬───────────────────┘
                             ▼
              ┌──── T-1 · Non-food pre-classifier ────┐
              │  keyword + embedding, local, ~0 cost   │──► non-food
              │  "hey", "thanks", "how are you"        │    scripted redirect
              └──────────────┬────────────────────────┘    (no LLM, no quota)
                             ▼
     ┌───────── T0 · Exact cache / user recents ──────────┐
     │  hash hit → cached parse result                     │   ~0 cost, <100ms
     └──────────────┬──────────────────────┬───────────────┘
                 miss                     hit ──────────────┐
                    ▼                                       │
     ┌───────── T1 · Deterministic parser ────────────┐      │
     │  new-meal grammar:  QTY + UNIT + [STATE] + FOOD │      │
     │  edit grammar:      "X was actually Ng"          │      │
     │                     "remove the X" / "add N X"   │      │
     └──────────────┬──────────────────┬───────────────┘      │
              coverage ≥ θ         coverage < θ               │
                    │                  ▼                      │
                    │   ┌──── T2 · Small LLM ───────────────┐ │
                    │   │  returns an INTENT ENVELOPE (§7.3) │ │
                    │   │  { intent, targetRef, slot,        │ │
                    │   │    items[], mealName }             │ │
                    │   │  — no nutrition fields, ever       │ │
                    │   └────────┬───────────────┬───────────┘ │
                    │       confident        low confidence     │
                    │            │               ▼              │
                    │            │   ┌── T3 · Large LLM ────┐   │
                    │            │   │  hard/ambiguous only │   │
                    │            │   └──────────┬───────────┘   │
                    ▼            ▼              ▼               │
     ┌──────────── Intent dispatch (§7.5) ──────────────────┐   │
     │  LOG_NEW · EDIT_ITEM · ADD_ITEM · REMOVE_ITEM ·       │   │
     │  SET_SLOT · OTHER  → mutate draft or create one       │   │
     └──────────────────────┬───────────────────────────────┘   │
                            ▼                                   │
     ┌──────────── Composite expansion (deterministic) ──────┐   │
     │  CompositeFood table: dish → component foods & ratios  │   │
     └──────────────────────┬───────────────────────────────┘   │
                            ▼                                   │
     ┌──────────── Food resolution (local DB) ──────────────┐    │
     │  trigram + embedding search → candidate ranking       │    │
     └──────────────────────┬───────────────────────────────┘    │
                            ▼                                    │
     ┌──────────── Nutrition engine (deterministic) ─────────┐    │
     │  yield factors · unit conversion · per-100g × qty      │◄───┘
     └──────────────────────┬────────────────────────────────┘
                            ▼
                    Meal draft preview
                            ▼
                    Edit (tap or text) ──► back to T-1
                            ▼
                    Confirm & Log
```

### 7.1 Tier definitions

| Tier | Handles | Cost | Target share of traffic |
|---|---|---|---|
| **T-1 — Pre-classifier** | Greetings, thanks, chit-chat, obvious non-food | ~0 | 5–10% |
| **T0 — Cache & recents** | Repeat meals, identical phrasings, the user's own history | ~0 | 35–45% at steady state |
| **T1 — Deterministic parser** | Explicit `QTY + UNIT + FOOD`, household measures, **and the common edit patterns** | ~0 | 20–25% |
| **T2 — Small model** | Descriptive meals, implicit quantities, unstructured edits, novel dishes | Low | 25–35% |
| **T3 — Large model** | Genuinely ambiguous, multi-clause, unusual cuisine, T2 low-confidence | High | < 5% |

> **Compound dishes are no longer a T2 responsibility.** Decomposing "chicken quinoa bowl" into
> four components is a *catalog lookup*, not a language task — see §7.6.

### 7.2 Confidence definition

The flow doc's "Can we confidently understand it?" needs to be a number, not a vibe. Define
`parse_confidence` as a weighted combination of:

- **Token coverage** — fraction of meaningful input tokens consumed by the grammar (excluding stopwords).
- **Food match score** — best candidate's lexical (trigram) + semantic (embedding) similarity.
- **Match margin** — gap between the top and second-best food candidates. A small margin means genuine ambiguity even when the top score is high.
- **Quantity presence** — was a quantity parsed, or assumed from a default?
- **Intent clarity** — for messages arriving with an open draft, how cleanly the input maps to one intent (§7.5). Ambiguity between "add to this meal" and "start a new meal" resolves to a user prompt, never a silent guess.

Ship the thresholds in **shadow mode first**: run T1 and T2 in parallel for two weeks, log
disagreements, and calibrate `θ` against actual user corrections before the router trusts T1
alone. Thresholds live in remote config, not in the binary.

### 7.3 LLM contract (non-negotiable)

The model's single job is **text → structured intent**. It never computes, never picks a
database row, and never speaks in prose.

**Response schema — an intent envelope, not a food list:**

```jsonc
{
  "intent":    "LOG_NEW | EDIT_ITEM | ADD_ITEM | REMOVE_ITEM | SET_SLOT | DIARY_QUERY | APP_HELP | NUTRITION_QA | ADVICE_SEEKING | WELLBEING_FLAG | SOCIAL | UNCLEAR | OTHER",
  "targetRef": "the dressing",        // free text referring to an existing draft item; null for LOG_NEW
  "slot":      "BREAKFAST",           // only when the user stated it explicitly; else null
  "mealName":  "Chicken Quinoa Bowl", // optional, free-rides on this call (§5.1.3)
  "dishCategory": "SPICED_CURRY",     // closed enum; only for uncurated composites (§7.6.1)
  "items": [
    { "food": "grilled chicken breast",
      "quantity": 120, "unit": "g",
      "state": "cooked", "prep": "grilled",
      "sizeQualifier": "small | medium | large | null",
      "confidence": 0.9 }
  ]
}
```

Rules:

- **Structured output / tool-calling only.** Free-form prose is not a valid response shape.
- **No nutrition fields exist in the schema.** There is nowhere to put a calorie number, which makes hallucinated nutrition structurally impossible rather than merely discouraged.
- **`targetRef` is free text, not an ID.** The model never sees item IDs; resolution to a draft item happens server-side by fuzzy match (§7.5). This keeps prompts small and prevents the model from inventing references.
- The food database is **never** sent in the prompt. The model emits a food *name*; local search resolves it. This is the difference between a 400-token call and a 40,000-token one.
- `max_tokens` tightly bounded (~300); the static system prompt is **prompt-cached**.
- One call per message, handling all items and the intent together — never one call per food, and never a separate call for naming or slot.
- Every call is logged with tier, tokens, latency, cost, and downstream correction outcome.

### 7.4 Caching strategy

- **L0 — device:** the user's last ~200 resolved parses, for instant offline re-log.
- **L1 — per-user:** normalized-hash → parse result. Highest hit rate; people eat the same things.
- **L2 — global:** normalized-hash → parse result for common phrasings, shared across users. No PII in the key; hash only the normalized food text, never the raw message.
- **L3 — semantic:** embedding-nearest-neighbour over cached parses, with a strict similarity floor. Catches "chicken salad w/ quinoa" ≈ "grilled chicken salad with quinoa". Gate this behind a high threshold — a wrong semantic cache hit is worse than a cache miss.
- Cache keys for `LOG_NEW` intents only. Edits are draft-relative and not cacheable.
- Invalidate on food-catalog version bump.

### 7.5 Intent dispatch

Every message is routed by intent, whether it came from T1's edit grammar or T2's envelope.

| Intent | Trigger example | Behavior |
|---|---|---|
| `LOG_NEW` | "grilled chicken salad with quinoa" | Create a new `MealDraft`. If one is already open, prompt *Add to this meal* / *Start a new meal* (§5.1.1) |
| `EDIT_ITEM` | "chicken was actually 150g" | Resolve `targetRef` → item, update quantity/unit/state, recompute |
| `ADD_ITEM` | "add a boiled egg" | Append to the open draft; if no draft is open, treat as `LOG_NEW` |
| `REMOVE_ITEM` | "remove the dressing" | Resolve `targetRef` → item, delete, recompute |
| `SET_SLOT` | "this was breakfast" | Update `draft.slot` only |
| `DIARY_QUERY` | "how many calories left?" | Answer from the user's own logged data (§5.5) |
| `APP_HELP` | "how do I change my goal?" | Deep-link to the relevant setting or Help Center (§5.5) |
| `NUTRITION_QA` | "is quinoa high in protein?" | Bounded factual answer from catalog data, or deflect (§5.5) |
| `ADVICE_SEEKING` | "should I try keto?" | Decline, redirect to a professional (§5.5) |
| `WELLBEING_FLAG` | see §5.6 | Supportive response; never a canned redirect |
| `SOCIAL` | "hey", "thanks" | Brief warm reply + nudge back to logging |
| `UNCLEAR` | "yes", "asdfgh" | Ask one clarifying question |
| `OTHER` | "write me a poem" | Scripted decline; no draft mutation |

**`targetRef` resolution.** Fuzzy-match the reference against the open draft's item names
(trigram + embedding, same machinery as food resolution). On a confident single match, apply.
On no match or an ambiguous match, ask once — *"Which one — the tahini dressing or the quinoa?"* —
rather than editing the wrong row. Silently mutating the wrong item is worse than one clarifying turn.

**T1 edit grammar.** The common patterns are regular enough to parse deterministically and
should never reach a model:

```
"<food> was actually <qty><unit>"     → EDIT_ITEM
"make the <food> <qty><unit>"          → EDIT_ITEM
"remove/delete the <food>"             → REMOVE_ITEM
"add <qty> <food>"                     → ADD_ITEM
"this was <slot>"                      → SET_SLOT
```

### 7.6 Composite foods (deterministic decomposition)

Decomposing a named dish into components is a **catalog lookup, not a language task**. Routing
it through an LLM would make the ingredient breakdown vary between runs — non-determinism
entering nutrition through the side door, contradicting the core principle in the header.

- A `CompositeFood` table maps a dish to its component foods and default ratios (USDA publishes
  many of these; curated entries cover regional dishes).
- The LLM identifies the **dish name only**. Expansion happens deterministically, after intent
  dispatch and before food resolution.
- Components are individually editable and removable in the preview, exactly like directly-named items.
- **Unknown dish → a single estimated-dish item, not a fabricated ingredient list.** An
  AI-generated breakdown into four precise-looking rows carries **false precision**: it is visually
  indistinguishable from the deterministic path, so a user can log invented ingredients without
  ever knowing they were invented. Instead:

  ```
  ⚠ Misal Pav (estimated)                  350–550 kcal
     Spiced curry dish · ~300g · rough estimate
     [ Break into ingredients ]   [ Find this food ]
  ```

  One row, one clearly-flagged estimate, `isEstimatedDish = true`. The AI's proposed breakdown is
  available **only if the user explicitly opts in** via *Break into ingredients* — at which point
  they know what they're accepting. The dish files to `FoodMissQueue` regardless.

#### 7.6.1 Where the estimated number comes from

**The model does not produce it.** An "estimated" calorie value with no deterministic source
would violate the header principle just as much as a fabricated ingredient list — it would simply
hide the fabrication behind a single number instead of four rows.

The estimate is computed by the nutrition engine from a **`DishCategoryProfile`**: a curated
per-100g nutrient band for each broad dish class (spiced curry, fried snack, creamy pasta, clear
soup, grain bowl…), derived by aggregating catalog entries — not authored by a model.

```
LLM  →  { dishCategory: "SPICED_CURRY", servingEstimate: 300g }   ← interpretation only
                              ↓
Engine →  DishCategoryProfile["SPICED_CURRY"].per100g  ×  300g    ← deterministic
                              ↓
         350–550 kcal  (profile p25–p75 band)
```

Rules:

- The model supplies **a category and a serving size** — both interpretation, both editable, neither a nutrient value. `dishCategory` is a closed enum validated in §12.4.
- Display is a **range, not a point value**. A range is honest about the uncertainty and cannot be mistaken for a measured figure. Both bounds come from the profile's p25–p75 spread.
- **It counts toward daily totals**, using the profile midpoint. Excluding it would understate the user's day, which is a worse error than a flagged approximation.
- The item is **excluded from nutrition-MAE scoring** (§12.11) and reported as its own metric — mixing category estimates into accuracy figures would flatter them.
- **No confident category → no number.** The item renders unresolved ("Nutrition unavailable — search for this food") and contributes nothing. A wrong category is worse than an absent one.
- Once curated, the dish resolves deterministically forever after — the flywheel in I8.

---

## 8. Nutrition Computation Rules

- Item nutrition = `per_100g_vector × (grams / 100)`, where grams comes from unit conversion.
- Meal totals = Σ items. Rounding applied **once, at display time** — kcal to nearest 1, macros to nearest 1g. Never round intermediate values, or the sum won't match the parts.
- **Raw ↔ cooked yield factors are mandatory.** "100g rice" raw vs. cooked differ by ~3×; this is the largest single accuracy risk in text-based logging. Maintain a yield-factor table (`food_id → {raw→cooked multiplier}`) and:
  - honor explicit state ("cooked rice"),
  - otherwise apply a per-food *default state* (rice defaults to cooked; chicken defaults to cooked; oats default to dry),
  - surface the assumed state in the item subtitle so it's correctable.
- **Household-measure table** (`katori`, `bowl`, `roti`, `cup`, `tbsp`, `slice`, `piece`) mapping to grams **per food**, not globally — a cup of rice and a cup of spinach are not the same mass. Regional defaults where the data supports it.
- **Data sources**, per the flow doc: USDA FoodData Central (generic), Open Food Facts (branded), CaloryX curated (Indian/regional foods and household measures). All normalized into the local `Food` table at ingest — **no external API call on the logging path.**
- Compute at minimum: calories, protein, carbs, fat, fiber. Carry sugar, saturated fat, sodium, cholesterol, and micronutrients when the source has them; render them on the meal detail screen, not in the chat card.
- Missing nutrient ≠ zero. Store `null` and render `—`, or macro sums silently understate.

---

## 9. Data Model (Prisma sketch)

> Extends the model in the Meals PRD rather than replacing it. `Food`, `MealSlot`, and
> `LoggedMeal` are reused as defined there.

```prisma
model ChatSession {
  id         String        @id @default(cuid())
  userId     String
  startedAt  DateTime      @default(now())
  messages   ChatMessage[]
  drafts     MealDraft[]
  @@index([userId, startedAt])
}

model ChatMessage {
  id              String      @id @default(cuid())
  session         ChatSession @relation(fields: [sessionId], references: [id])
  sessionId       String
  role            ChatRole    // USER | ASSISTANT
  clientMessageId String?     // client-generated UUID; null for assistant messages
  content         String
  draftId         String?     // set when this message rendered a preview card
  createdAt       DateTime    @default(now())
  @@unique([sessionId, clientMessageId])
}

// Replay protection needs the ORIGINAL RESPONSE BODY, not just duplicate detection —
// a retried message must return the same draft state it produced the first time (§12.1).
model IdempotencyRecord {
  key          String   @id          // clientMessageId / offline opId
  userId       String
  requestHash  String                // guards against key reuse with different content
  responseBody Json
  statusCode   Int
  createdAt    DateTime @default(now())
  expiresAt    DateTime              // = createdAt + REPLAY_WINDOW (see below)
  @@index([expiresAt])
}
// RETENTION RULE: idempotency TTL must be >= the maximum age at which an operation can
// still reach the server, or a queued offline op replayed on day 5 finds no record and
// creates a duplicate meal. A 24h TTL against a 7-day offline queue was exactly that bug.
//
//   REPLAY_WINDOW = MAX_QUEUE_AGE (30d hard expiry) + retry tail (1d) = 31 days
//
// Both constants live in one config value; they must never be tuned independently.

enum ChatRole { USER ASSISTANT }

model MealDraft {
  id          String          @id @default(cuid())
  userId      String
  session     ChatSession     @relation(fields: [sessionId], references: [id])
  sessionId   String
  name        String          // AI-generated, e.g. "Chicken Quinoa Bowl"
  slot        MealSlot        // inferred from time, user-overridable
  items       MealDraftItem[]
  // derived totals, cached for render; always == sum(items)
  caloriesKcal Float
  proteinG     Float
  carbsG       Float
  fatG         Float
  fiberG       Float
  parseTier    ParseTier      // which tier produced this draft
  confidence   Float
  version      Int            @default(1)   // optimistic locking (§12.1)
  operations   DraftOperation[]
  status       DraftStatus    @default(OPEN)
  expiresAt    DateTime       // 24h TTL
  createdAt    DateTime       @default(now())
  @@index([userId, status])
}
// ONE OPEN DRAFT PER USER — enforced by a partial unique index, not application logic,
// because multi-device and concurrent requests will race otherwise (§12.1):
//   CREATE UNIQUE INDEX one_open_draft_per_user
//     ON "MealDraft"("userId") WHERE "status" = 'OPEN';
// Prisma: declare via raw migration; the check-then-insert pattern is not sufficient.

enum DraftStatus { OPEN CONFIRMED DISCARDED EXPIRED }
enum ParseTier   { PRECLASSIFIER CACHE PARSER LLM_SMALL LLM_LARGE }
enum ChatIntent  { LOG_NEW EDIT_ITEM ADD_ITEM REMOVE_ITEM SET_SLOT
                   DIARY_QUERY APP_HELP NUTRITION_QA ADVICE_SEEKING
                   WELLBEING_FLAG SOCIAL UNCLEAR OTHER }

// Deterministic dish decomposition — replaces LLM-based compound-dish breakdown (§7.6)
model CompositeFood {
  id             String                  @id @default(cuid())
  name           String                  // "Chicken Biryani"
  aliases        String[]
  locale         String?
  // recipe basis — components are defined against ONE serving of this size
  // COMPONENTS ARE DEFINED ON AN AS-SERVED (COOKED) BASIS. There is deliberately no
  // yieldFactor here: raw→cooked conversion happens once, during curation, using the
  // per-food yield table in §8. Carrying a second runtime factor is how double-application
  // bugs happen, so the field is removed by design rather than documented as a caveat.
  servingGrams   Float                   // as-served mass of ONE serving
  servingLabel   String                  // "1 plate", "1 katori"
  components     CompositeFoodComponent[]
  isCurated      Boolean @default(false)
  catalogVersion Int
  @@index([name])
}

model CompositeFoodComponent {
  id            String        @id @default(cuid())
  composite     CompositeFood @relation(fields: [compositeId], references: [id], onDelete: Cascade)
  compositeId   String
  food          Food          @relation(fields: [foodId], references: [id])
  foodId        String
  // ratio-based so scaling a serving scales components correctly
  ratioOfServing Float                    // fraction of servingGrams, AS SERVED; components sum to 1.0 ± 0.02
                                          // (validated at curation time, not runtime)
  state          FoodState @default(UNSPECIFIED)  // raw or cooked basis for THIS component
  prep           String?
  isOptional     Boolean   @default(false)
}

// An item is in exactly ONE resolution state, and that state determines which fields are
// required. Making every field mandatory was wrong: an UNRESOLVED item has no grams, no
// mass source and no match score, because nothing was resolved.
enum ItemResolution { RESOLVED ESTIMATED_DISH UNRESOLVED }

model MealDraftItem {
  id            String   @id @default(cuid())
  draft         MealDraft @relation(fields: [draftId], references: [id], onDelete: Cascade)
  draftId       String
  resolution    ItemResolution        // discriminator — drives validation (§12.4) and UI
  food          Food?     @relation(fields: [foodId], references: [id])
  foodId        String?   // required iff RESOLVED
  rawText       String    // always present — what the user said, for the miss-queue
  quantity      Float?    // null when nothing was parsed
  unit          String?   // "g", "unit", "katori", "cup"...
  grams         Float?    // null for UNRESOLVED
  state         FoodState @default(UNSPECIFIED)
  defaultGrams  Float?    // baseline for multiplier chips & deltas; null for UNRESOLVED
  // full interpretation is persisted — nothing from the envelope is dropped
  prep          String?
  sizeQualifier String?
  quantitySource QuantitySource?      // drives the est. chip (§5.1.1a)
  massSource     MassSource?          // drives the provenance line
  matchScore     Float?               // null for UNRESOLVED and ESTIMATED_DISH
  matchBand      MatchBand?
  // ESTIMATED_DISH persistence (§7.6.1) — the range must survive the draft, not be recomputed
  dishCategory   String?              // closed enum value; required iff ESTIMATED_DISH
  kcalLow        Float?               // profile p25 × grams
  kcalHigh       Float?               // profile p75 × grams
  kcalMidpoint   Float?               // what counts toward daily totals
  profileVersion Int?                 // which DishCategoryProfile produced the range
}

// FIELD REQUIREMENTS BY RESOLUTION STATE — enforced in the validation layer (§12.4):
//
//   RESOLVED        foodId, quantity, unit, grams, defaultGrams,
//                   quantitySource, massSource, matchScore, matchBand   REQUIRED
//                   dishCategory, kcal*                                 MUST BE NULL
//
//   ESTIMATED_DISH  dishCategory, grams, kcalLow/High/Midpoint,
//                   profileVersion                                      REQUIRED
//                   foodId, matchScore, matchBand                       MUST BE NULL
//
//   UNRESOLVED      rawText                                             REQUIRED
//                   everything else                                     NULL
//
// Contributions to draft totals: RESOLVED → computed nutrition;
// ESTIMATED_DISH → kcalMidpoint (macros null, shown as "—");
// UNRESOLVED → nothing, and the draft is flagged incomplete.

model DraftOperation {
  id        String    @id @default(cuid())
  draft     MealDraft @relation(fields: [draftId], references: [id], onDelete: Cascade)
  draftId   String
  op        String    // CREATE | EDIT_ITEM | ADD_ITEM | REMOVE_ITEM | SET_SLOT | RENAME
  actor     String    // "user" | "system"
  payload   Json
  version   Int       // resulting draft version
  createdAt DateTime  @default(now())
  @@index([draftId, version])
}

enum FoodState      { RAW COOKED UNSPECIFIED }
enum QuantitySource { EXPLICIT ASSUMED }
enum MassSource     { DIRECT HOUSEHOLD_TABLE USER_HISTORY CATALOG_SERVING CATEGORY_FALLBACK }
enum MatchBand      { HIGH MEDIUM LOW }

// Per-user portion learning — the feature's self-improvement loop (§5.1.1a)
model UserServingPreference {
  id           String    @id @default(cuid())
  userId       String
  food         Food      @relation(fields: [foodId], references: [id])
  foodId       String
  state        FoodState @default(UNSPECIFIED)  // raw vs cooked rice are different portions
  unit         String                            // "g" | "katori" | "unit" — context matters
  medianGrams  Float
  observations Int       @default(1)
  updatedAt    DateTime  @updatedAt
  @@unique([userId, foodId, state, unit])
}
// Two-level lookup: try the exact (user, food, state, unit) key first; if it has < 3
// observations, fall back to a coarser (user, food) aggregate. A fine key alone would
// fragment the median across contexts and rarely reach the threshold.

// Cost & quality telemetry — first-class, not an afterthought
model ParseEvent {
  id             String    @id @default(cuid())
  userId         String
  inputHash      String
  tier           ParseTier
  intent         ChatIntent
  countedToQuota Boolean   @default(false)  // false for edits & non-food (§5.1.4)
  model          String?
  promptTokens   Int?
  outputTokens   Int?
  costMicros     Int?
  latencyMs      Int
  confidence     Float
  wasCorrected   Boolean   @default(false)  // did the user edit before logging?
  wasLogged      Boolean   @default(false)
  createdAt      DateTime  @default(now())
  @@index([inputHash])
  @@index([userId, createdAt])
}

// Unresolved foods → catalog backlog (the accuracy flywheel)
model FoodMissQueue {
  id         String   @id @default(cuid())
  rawText    String
  occurrences Int     @default(1)
  locale     String?
  status     String   @default("PENDING")
  createdAt  DateTime @default(now())
  @@unique([rawText, locale])
}
```

Three deliberate decisions worth review:
1. **`MealDraft` is a real server-side entity**, not client state, so text edits, tap edits, and multi-device continuity all mutate one authoritative object.
2. **`ParseEvent` exists from day one.** Without per-parse cost and correction telemetry we cannot tune the router, and the router is where the entire cost story lives.
3. **`FoodMissQueue`** turns every failed match into catalog work, which is how the Indian/regional food gap actually closes.

---

## 10. API (REST)

| Method & path | Purpose |
| --- | --- |
| `POST /v1/assistant/sessions` | Open or resume a chat session |
| `POST /v1/assistant/messages` | Send a user message → returns assistant reply + draft (single response, not streamed) |
| `GET /v1/assistant/drafts/:id` | Fetch current draft state |
| `PATCH /v1/assistant/drafts/:id/items/:itemId` | Update quantity/unit/state for one item |
| `POST /v1/assistant/drafts/:id/items` | Add an item |
| `DELETE /v1/assistant/drafts/:id/items/:itemId` | Remove an item |
| `PATCH /v1/assistant/drafts/:id` | Update meal name or slot |
| `POST /v1/assistant/drafts/:id/confirm` | Commit → creates a `LoggedMeal` (idempotency key required) |
| `GET /v1/assistant/quota` | Remaining AI parses for the quota pill |
| `POST /v1/foods/resolve` | Resolve free text → ranked food candidates (used by "unresolved item" UI) |

- `POST /messages` is a **plain request/response in v1 — not streamed.** The original design
  streamed `typing → items → totals → done`, but the payload is a small structured object
  (~300 tokens), partial JSON can't be rendered reliably without incremental parsing, and the
  typing indicator is client-side and needs no server support. SSE infrastructure is not worth it
  for v1; revisit only if p95 latency proves it necessary (§12 review #18).
- **Every mutating request carries `messageId` (idempotency) and `version` (optimistic lock).**
  Version mismatch → `409 Conflict` with the current draft (§12.1).
- All responses carry the correlation chain from §12.9.
- The **item-level PATCH endpoints exist for persistence, not for interactivity.** The Adjust Portion screen recomputes locally and PATCHes once on `Update Portion` — not on every slider frame.
- Confirm returns the created `LoggedMeal` so Home totals update from the response without a refetch.
- All assistant endpoints are rate-limited per user independently of the displayed AI quota.

---

### 10.1 Sample response contracts

Illustrative shapes to align RN and backend before implementation. Envelope fields
(`requestId`, `messageId`, `draftVersion`) appear on every response.

**`POST /messages` — 200, draft created**

```jsonc
{
  "requestId": "req_8f2a", "messageId": "cm_01H...", "tier": "LLM_SMALL",
  "intent": "LOG_NEW", "countedToQuota": true,
  "assistantText": "Got it — let me break that down.",
  "draft": {
    "id": "drf_9k1", "version": 1, "status": "OPEN",
    "name": "Chicken Quinoa Bowl", "slot": "LUNCH",
    "totals": { "kcal": 425, "proteinG": 38, "carbsG": 32, "fatG": 18, "fiberG": 6 },
    "items": [{
      "id": "itm_1", "foodId": "fd_chkn_br", "displayName": "Grilled Chicken Breast",
      "quantity": 120, "unit": "g", "grams": 120, "unitType": "continuous",
      "state": "COOKED", "prep": "grilled", "sizeQualifier": null,
      "quantitySource": "ASSUMED", "massSource": "CATALOG_SERVING",
      "matchBand": "HIGH", "isEstimatedDish": false,
      "kcal": 198, "defaultGrams": 120,
      "perGram": { "kcal": 1.65, "proteinG": 0.31, "carbsG": 0, "fatG": 0.036 }
    }]
  }
}
```

> `perGram` is what makes portion adjustment local (§5.2.1) — the client never round-trips a slider.

**`409` — draft version conflict**

```jsonc
{ "requestId": "req_8f2b", "error": "DRAFT_VERSION_CONFLICT",
  "message": "This meal changed on another device.",
  "draft": { "id": "drf_9k1", "version": 3, "...": "full current state" } }
```

> The current draft is returned so the client re-renders rather than retrying blindly.

**Medium-confidence and unresolved items** (partial `items[]`)

```jsonc
[
  { "id": "itm_2", "displayName": "Dal, toor, cooked", "matchBand": "MEDIUM",
    "candidates": [ { "foodId": "fd_dal_moong", "name": "Dal, moong, cooked", "score": 0.81 },
                    { "foodId": "fd_dal_masoor", "name": "Dal, masoor, cooked", "score": 0.78 } ],
    "kcal": 116 },

  { "id": "itm_3", "displayName": "pithla", "foodId": null, "matchBand": "LOW",
    "rawText": "1 bowl of pithla", "kcal": null,
    "resolution": "UNRESOLVED", "action": "SEARCH" },

  { "id": "itm_4", "displayName": "Misal Pav", "isEstimatedDish": true,
    "dishCategory": "SPICED_CURRY", "grams": 300,
    "kcal": 450, "kcalRange": [350, 550], "excludeFromAccuracy": true }
]
```

**`429` — quota exhausted**

```jsonc
{ "error": "AI_QUOTA_EXCEEDED", "used": 20, "limit": 20, "resetsAt": "2026-09-05T04:00:00Z",
  "fallbackAvailable": true,
  "message": "Quantified meals still work — try \"200g rice, 100g chicken\"." }
```

> `fallbackAvailable` is what keeps T1 logging alive at the limit (§5.1.4) — never a hard block.

**`POST /drafts/:id/confirm` — 201**

```jsonc
{ "loggedMeal": {
    "id": "lm_44c", "slot": "LUNCH", "loggedAt": "2026-09-04T13:12:00Z",
    "catalogVersion": 214, "nutritionEngineVersion": "3.1.0",
    "snapshot": { "kcal": 425, "proteinG": 38, "carbsG": 32, "fatG": 18, "fiberG": 6 },
    "items": [ "..." ] },
  "dailyTotals": { "kcal": 1448, "remainingKcal": 552 } }
```

> `dailyTotals` returns with the confirm so Home updates without a refetch, and `snapshot` +
> `catalogVersion` are what make this meal reproducible later (§12.3).

---

## 11. Cost & Performance

Given speed is priority #1 and this feature has a variable per-use cost, both are requirements, not aspirations.

| Budget | Target |
|---|---|
| Send → typing indicator | < 100ms (client-rendered; no server round trip involved) |
| T0/T1 path: send → preview rendered | p50 < 400ms |
| T2 path: send → preview rendered | p50 < 1.5s, p95 < 3.5s |
| Portion adjustment recompute | < 16ms (local, zero network) |
| Confirm & Log perceived latency | Instant (optimistic) |
| LLM call rate | ≤ 30% of messages at steady state |

Additional levers:
- **Prompt caching** on the static system prompt + unit tables.
- **Model tiering** — the small model handles the overwhelming majority; escalation to the large model is an explicit, logged decision.
- **Warm the cache from the user's own history at session start** so their repeat meals resolve at T0 on the very first message.
- **Cost alerting** on `cost_per_logged_meal` moving average, with a circuit breaker that degrades to T1-only if the LLM provider is down or costs spike — the chat must never be fully unavailable.

---

## 12. Engineering Contracts & Quality Gates

Added in v1.4 following architecture review. These are the contracts the engineering team builds
against; they are not optional hardening to be deferred.

### 12.1 Concurrency & idempotency

**Draft versioning (optimistic locking).** `MealDraft.version` increments on every mutation.
Every mutating request carries the version it read; a mismatch returns `409 Conflict` with the
current draft so the client can re-render rather than blindly overwrite. Without this, a tap-edit
and a text-edit arriving together silently clobber one another — likely on mobile, where the user
can type while a request is in flight.

**Message idempotency.** `POST /messages` requires a client-generated `messageId` (UUID).
The server stores processed IDs for 24h and returns the original response on replay. Mobile
retries — backgrounding, flaky networks, the user tapping send twice — must not produce two
drafts or two parses. This also prevents a retried message from double-decrementing quota.

**Idempotency at confirm** is already specified (§10) and remains separate: `messageId` guards
parsing, the confirm key guards logging.

### 12.2 Draft state machine & operation log

Allowed transitions only:

```
        ┌──────────────────────────────┐
        ▼                              │
   [ OPEN ] ──confirm──► [ CONFIRMED ]  │  (terminal)
      │  ▲                             │
      │  └───────── mutate ────────────┘
      ├──discard──► [ DISCARDED ]         (terminal)
      └──24h TTL──► [ EXPIRED ]           (terminal)
```

**How `OPEN` actually becomes `EXPIRED`.** `expiresAt` alone is a timestamp, not a transition —
and because one-open-draft is enforced by a partial unique index (§9), a stale `OPEN` draft would
otherwise **permanently block the user from starting a new one**. Two mechanisms, and the first
is mandatory:

1. **Transactional lazy expiry (authoritative).** Draft creation runs in a single transaction:
   `UPDATE MealDraft SET status='EXPIRED' WHERE userId=$1 AND status='OPEN' AND expiresAt < now()`
   → then insert. This makes the constraint self-healing and independent of any scheduled job.
2. **Background sweeper (hygiene only).** A periodic job expires stale drafts for accurate
   analytics and to bound table growth. If it fails, correctness is unaffected — mechanism 1
   already guarantees the user can always start a new draft.

Every read path also treats an `OPEN` draft past `expiresAt` as expired, so a client holding a
stale draft never sees it as resumable.

Mutations are rejected on any terminal state. Every mutation appends to `DraftOperation`
(op type, actor `user|system`, payload, resulting version). This gives concurrency debugging,
a provenance trail for the eval set, and makes **Undo** a later feature rather than a rewrite.

### 12.3 Versioning & reproducibility

Every `LoggedMeal` stores `catalogVersion`, `nutritionEngineVersion`, and a **frozen nutrition
snapshot**.

> **This resolves a real defect, not just an audit gap.** §6 states that meal totals are always
> the live sum of item nutrition. Combined with a mutable catalog, a correction to the chicken-breast
> row silently rewrites the calories of every chicken meal the user ever logged — quietly
> corrupting the historical trends in Insights. Behavior is now explicit:
>
> - **Insights and history read the frozen snapshot.** Past meals never change.
> - **Opening a meal for edit recomputes at the current catalog**, and shows a note if the
>   result differs from the snapshot.
> - `catalogVersion` makes any historical number reproducible and auditable.

### 12.4 Validation layer

An explicit, testable gate between model output and any draft mutation:

```
LLM response → schema validation → domain validation → food resolution → nutrition engine
```

- **Schema:** valid JSON, known enum values, required fields, no unexpected fields.
- **Domain:** quantity > 0 and within plausible bounds; unit in the supported whitelist; item
  count ≤ 25; string lengths bounded; `targetRef` only present on edit intents; `slot` only on
  `SET_SLOT` or `LOG_NEW`.
- Validation covers **all 13 intents** (§5.5), not just the logging five.
- Any failure → treat as a parse miss and fall back per §13, never a partial mutation.

### 12.5 Server authority over nutrition

Client-side portion math (§5.2.1) is a **rendering optimization producing preview values only**.
The server recomputes every item on confirm. Discrepancies beyond a rounding epsilon: the server
value wins, and the delta is logged as a correctness signal — a persistent gap means the client's
per-unit vectors are stale.

### 12.6 Food-resolution confidence policy

Match score and margin (§7.2) now map to explicit behavior:

| Band | Behavior |
|---|---|
| **High** | Auto-resolve silently |
| **Medium** | Resolve to the top candidate but show it as confirmable — "Chicken breast? *(tap to change)*" |
| **Low** | Unresolved item row + `Find this food` (§13); file to `FoodMissQueue` |

The medium band matters most for Indian and regional foods, where several catalog entries score
closely. It reuses the clarification chip component (§17, U6). Thresholds live in remote config.

### 12.7 Cache key versioning

Cache keys are scoped by everything that can change interpretation:

```
key = hash(normalizedText, locale, catalogVersion, normalizationVersion, parserVersion)
```

The **semantic cache (L3) is a retrieval optimization, never authoritative** — a semantic hit
still passes through food resolution and the nutrition engine rather than replaying a stored result.

### 12.8 Quota enforcement

Check-and-consume is a **single atomic operation** (Redis `INCR` with TTL, or a locked row) so
simultaneous requests can't both pass a check-then-write. The `LOG_NEW`-only exemption logic
(§5.1.4) is evaluated inside that operation, not before it.

Three independent mechanisms, deliberately not merged: **rate limiting** (abuse), **AI quota**
(product/monetization), **cost circuit breaker** (§11, financial protection). Any one can trip
without the others.

### 12.9 Observability

Correlation chain propagated across every hop and attached to all logs and analytics:

```
requestId → messageId → sessionId → draftId → parseEventId → llmCallId
          → foodResolutionId → nutritionCalcId
```

This is what makes "why did this meal log two chickens" answerable in minutes.

### 12.10 Evaluation & regression framework

Runtime metrics tell us how the current system performs; they cannot tell us whether a change
regressed something. A **fixed, versioned evaluation set** is required before the first parser or
model change ships.

- **Seed:** the *Conversation Scenarios* companion doc is the human-readable layer; the machine
  set expands it to a few hundred cases with expected structured output and known-correct nutrition.
- **Composition:** quantified, descriptive, household measures, composite dishes, regional foods,
  edits, misspellings, and every non-logging intent.
- **Gate:** CI runs it on any change to parser, prompt, model version, thresholds, or catalog.
- **Measure four things separately**, since a failure in any one is invisible in the others:
  extraction accuracy · food-resolution accuracy · nutrition error · intent-classification accuracy.

**Safety cases belong in this set too** — see §12.11.

### 12.11 Accuracy & safety metrics

**Parse acceptance rate is a satisfaction proxy, not ground truth.** A user cannot tell 120g of
chicken from 150g on screen, so they accept wrong previews routinely. Accepted-but-wrong is the
failure mode that silently degrades every downstream number in the product.

Track against the golden set: **MAE on calories** and **MAE per macro**, reported at p50/p90,
segmented by tier and cuisine. Regression gates on these, not on acceptance.

**Safety metrics (not in the original review).** `WELLBEING_FLAG` (§5.6) needs its own held-out
eval set with a **false-negative rate** target — missing a flag is a materially different failure
from a mis-parsed portion, and it will not show up in any accuracy metric above. Owned alongside
the §5.6 policy review.

### 12.12 Offline queue & sync

Offline logging is only available on paths that need no server: T0 cache hits and T1 parses
against the local food tables. Descriptive input requiring T2/T3 shows the offline state (§13).

**Queue model.** An ordered local operation log (on-device SQLite), per user, surviving app
restarts. Entries replay strictly in order on reconnect.

**Queued operation payload — the full contract:**

```ts
interface QueuedOperation {
  opId:                 string;   // UUIDv4 — doubles as the idempotency key server-side
  sequence:             number;   // monotonic per user; replay order
  opType:               'CREATE_DRAFT' | 'EDIT_ITEM' | 'ADD_ITEM'
                      | 'REMOVE_ITEM' | 'SET_SLOT'   | 'CONFIRM_LOG';

  localDraftId:         string;   // client-generated; stable across the queue
  serverDraftId:        string | null;  // populated after the CREATE_DRAFT op syncs

  mealTimestamp:        string;   // ISO — when the user ATE, not when this syncs.
                                  // The meal must land on the correct day regardless of
                                  // when the device reconnects.
  createdAt:            string;   // when the op was queued

  payload:              object;   // op-specific body, same shape as the online API

  // reconciliation inputs (§12.5, §12.12)
  clientCatalogVersion: number;
  clientEngineVersion:  string;
  nutritionSnapshot:    Totals;   // what the client showed the user

  // retry state
  status:      'PENDING' | 'IN_FLIGHT' | 'SYNCED' | 'FAILED' | 'NEEDS_REVIEW';
  retryCount:  number;
  lastAttemptAt: string | null;
  nextAttemptAt: string | null;   // backoff schedule
  lastError:     string | null;
}
```

**Local → server ID mapping.** An offline draft is created against a client-generated
`localDraftId`; the server assigns the real ID only when `CREATE_DRAFT` syncs. Every subsequent
queued op therefore references the **local** ID and must be rewritten to `serverDraftId` during
replay. The client maintains this map for the life of the queue — without it, a queued edit
arrives referencing an ID the server has never seen.

**Expiry.** Operations hard-expire at **30 days** and are surfaced for review, never silently
dropped. This constant and the idempotency TTL (§9) are derived from one config value.

| Concern | Behavior |
|---|---|
| **Duplicates** | `clientMessageId` + `IdempotencyRecord` — replay returns the original response, never a second meal |
| **Ordering** | Strictly sequential replay; a failure halts the queue rather than skipping ahead, so later edits can't land before the create they depend on |
| **Draft conflicts** | Offline entries carry no draft version. On replay the server treats them as new drafts; edits to a draft that was already confirmed on another device are rejected and surfaced, not merged |
| **Catalog drift** | Server recomputes with its current catalog. If the result differs from the client snapshot beyond epsilon, the server value wins (§12.5) and the user sees a one-time "updated from our latest data" note on that meal |
| **Retries** | Exponential backoff, capped at 5 attempts, then the entry is surfaced for manual retry or discard — never dropped silently |
| **Staleness** | Entries older than 7 days prompt the user to confirm before syncing; a meal logged last week landing today would corrupt both days' totals |
| **Visibility** | Queued meals appear in the diary immediately, marked pending, and count toward local totals |

### 12.13 Incomplete-parse policy

**Unconsumed input is surfaced, never silently discarded.** When the LLM is unavailable and T1
coverage falls below θ, the fallback must not quietly produce a partial meal — a user who typed
four foods and sees two will not reliably notice the two that vanished, and the log is then wrong
in a way nothing downstream can detect.

```
  I could read part of that:

  Chicken breast                                    165
  100g

  ⚠ Couldn't read: "with some tahini dressing"
     [ Search foods ]   [ Try again ]
```

- Parsed items render normally; the unparsed span renders verbatim as an explicit gap.
- `Confirm & Log` stays enabled — a partial log the user can *see* is partial beats no log.
- Applies to every degraded path: provider outage, circuit breaker, quota exhaustion, offline.

### 12.14 Privacy & data retention

The chat stores user-authored free text and may transmit it to a third-party model provider.
Decisions required before build:

| Item | Requirement |
|---|---|
| **Chat retention** | `ChatMessage` TTL shorter than `LoggedMeal` — raw phrasing has little value once parsed. Proposed: 30 days |
| **Provider terms** | Zero-retention / no-training terms with the LLM provider, contractually confirmed |
| **PII redaction** | Strip names, contact details, and free-text health mentions before any provider call |
| **Log hygiene** | Application logs store hashes and IDs, never raw message text |
| **Analytics** | `assistant_wellbeing_flagged` carries a count only — no content, ever (§14) |
| **Encryption & residency** | At rest and in transit; region pinning where required |
| **Regulatory** | India's **DPDP Act 2023** applies given the primary market — consent, purpose limitation, and erasure rights. GDPR if EU launch follows. Health-adjacent data raises the bar in both |

Deletion must cascade: erasing an account removes chat, drafts, operations, and parse events.

---

## 13. Empty / Loading / Error States

| Situation | Behavior |
| --- | --- |
| First open | Greeting + 3 tappable example prompts ("2 eggs and toast", "bowl of dal and rice", "grilled chicken salad") — teaches the input format at zero cost |
| Parsing | Client-driven typing indicator; optional skeleton card. No server-pushed partials in v1 |
| No food identified | "I couldn't work out what food that was — try naming the dish, or search for it." + `Search foods` CTA |
| One item unresolved | Render the rest normally; that row shows a warning state + `Find this food` action. Never block the whole meal on one item |
| Quantity missing | Apply a default, mark `est.`, and log normally. Do **not** block (see §15, I3) |
| Non-logging message | Routed by intent per §5.5 — social, app help, diary query, nutrition Q&A, advice-seeking, unclear, other. Each has its own scripted response; none decrement quota |
| Wellbeing flag | §5.6 path — supportive reply, no numbers, resources offered, logging stays available, streak/deficit copy suppressed for the session |
| Ambiguous edit target | `targetRef` matches 0 or >1 draft items → ask once ("Which one — the tahini dressing or the quinoa?"). Never mutate the wrong row silently (§7.5) |
| Unknown composite dish | **Single estimated-dish row** with a category-derived range (§7.6.1) — *not* a decomposition. Breakdown only on explicit *Break into ingredients*. Files to `FoodMissQueue` |
| Quota exhausted | T1 path stays live; descriptive inputs surface an upgrade sheet |
| Offline | Cached/quantified inputs queue; descriptive inputs show an offline notice with a `Search foods` fallback |
| LLM timeout/error | Retry once with backoff, then fall back to T1 best-effort, then offer manual search. Never a bare error bubble |

---

## 14. Analytics Events

`assistant_opened` (source: fab | meals_empty | home),
`assistant_message_sent` (char_len, item_count_hint),
`assistant_parse_completed` (tier, intent, confidence, latency_ms, item_count, cost_micros, counted_to_quota),
`assistant_intent_ambiguous` (open_draft: bool, resolution: add | new | asked),
`assistant_nonlogging_message` (intent, tier, resolved: answered | deflected | deeplinked),
`assistant_diary_query_answered` (query_type: remaining | logged_today),
`assistant_wellbeing_flagged` (— no message content, no free text, count only),
`assistant_edit_target_ambiguous` (candidate_count),
`assistant_composite_expanded` (dish, component_count, is_curated),
`assistant_estimated_dish_shown` (dish) / `assistant_estimated_dish_expanded` (dish),
`assistant_draft_conflict` (draft_id, client_version, server_version),
`assistant_validation_failed` (stage: schema | domain, reason),
`assistant_server_client_mismatch` (item_id, delta_kcal),
`assistant_preview_shown` (draft_id, item_count, assumed_count, inferred_count),
`assistant_item_adjusted` (item_id, from_grams, to_grams, quantity_source, mass_source, unit_type, method: slider | stepper | chip | unit_switch | text),
`assistant_clarification_shown` (item_id, reason: high_impact) / `_answered`,
`assistant_item_removed`, `assistant_item_added` (method: text | search),
`assistant_slot_changed`, `assistant_meal_renamed`,
`assistant_meal_logged` (draft_id, tier, time_to_log_ms, edit_count, was_corrected),
`assistant_draft_abandoned` (draft_id, seconds_open, edit_count),
`assistant_food_unresolved` (raw_text_hash, locale),
`assistant_quota_blocked`, `assistant_quota_upgrade_tapped`.

A second cross-tab worth watching: **`massSource` × correction rate.** If `HOUSEHOLD_TABLE` items are corrected as often as `CATEGORY_FALLBACK` ones, the measure tables are wrong; if `USER_HISTORY` corrections fall over a user's lifetime, the serving-preference loop is working.

The pairing that matters most: **`tier` × `was_corrected`.** If T1 parses get corrected far more
often than T2, the router is too aggressive and `θ` needs raising. That single cross-tab is how
this feature gets tuned.

---

## 15. Accessibility

- All tappable targets ≥ 44×44pt, including the `−`/`+` steppers and quick-select chips.
- The portion slider needs a proper accessibility role with adjustable value, announced as
  "Portion size, 150 grams, adjustable" — a bare slider is unusable with a screen reader.
- Deltas must not rely on arrow color alone; announce as "247 kilocalories, up 49" and include a
  text sign (`+49`), not just a colored glyph.
- Chat messages announce role and time ("Assistant, 10:30 AM: …"); new assistant messages fire a
  polite live-region announcement.
- The `est.` chip needs a real accessible label ("estimated quantity — tap to adjust"), not a
  decorative badge.
- Selected quick-select chip state conveyed via `accessibilityState.selected`, not tint alone.
- Full dynamic-type support; the preview card must reflow rather than truncate item names.

---

## 16. Design Gaps & Flow Improvements

You asked for gaps and improvements. These are split into **design issues in the PDF** (fix before build) and **changes I'd recommend to the attached flow**.

### 16.1 Design gaps

| # | Gap | Detail | Recommendation |
|---|---|---|---|
| **G1** | **Adjust Portion deltas are wrong** | Default 120g = 198 kcal, so 150g = 247 kcal ✓ — but the delta reads `↑42` when it should be `+49`. Protein reads `46g ↓10`: protein *increased* with the portion, and the magnitude is wrong (≈37g → 46g, so `+9`). | Treat all deltas as computed, never authored. Fix the mock so QA doesn't inherit the wrong expected values. |
| **G2** | **Quick-select `1/2` doesn't math** | Against a 120g default, `1/2` should be 60g; the design shows 75g (0.625×). `1.25x = 150g` and `2x = 240g` are correct. | Multipliers derive from `defaultGrams`. Fix the mock. |
| **G3** | **Name inconsistency** | The chat greets "Hi Ava!" while Home/Profile show "Alex Chen". | Cosmetic in the mock, but confirm the greeting uses `user.firstName` with a graceful fallback ("Hi there 👋") for guest users. |
| **G4** | **`Confirm & Log` CTA is occluded** | In the mock the CTA sits behind the composer and is only visible in the text layer. | Pin the CTA above the composer as a sticky footer once a draft is open, or collapse the composer to a compact "Edit by typing" affordance while the preview is active. Needs an explicit design decision — this is the conversion button. |
| **G5** | **Quota pill is undefined** | `⚡ 0/20` — 20 of what, over what window, and what happens at 20? Also reads `0/20` after a completed exchange. | Spec'd in §5.1.4. Recommend counting AI parses, not messages. |
| **G6** | **No meal-slot affordance** | Nothing in the design says whether this logs to breakfast, lunch, dinner, or snacks. | Time-inferred default + tappable chip on the card (§5.1.2). |
| **G7** | **No ambiguity/clarification UI** | The flow doc requires clarification prompts with numbered options; the design has no component for them. | See I3 — I'd largely replace blocking questions with estimate-and-correct. Where a question *is* required, spec a chip-row component (tappable options + "Enter amount"). |
| **G8** | **No error, empty, or unresolved-item states** | Only the happy path is designed. | Components needed for all rows in §13 — particularly the unresolved-item row, which will occur often for regional foods. |
| **G9** | **ⓘ button purpose undefined** | Unlabeled on Adjust Portion. | Make it the nutrition-source/attribution sheet (§5.2.1). Also satisfies USDA/Open Food Facts attribution obligations. |
| **G10** | **No "add item" affordance on the card** | Text-only ("add an egg") is discoverable by few users. | Add a subtle `+ Add food` row at the bottom of the preview card, routing into the existing Meals search. |
| **G11** | **Portion screen shows no fiber** | Fiber is a first-class macro elsewhere in the app (Home rings, Onboarding targets) but is missing from Updated Nutrition. | Add it, or explicitly document why the portion editor shows only four. |

### 16.2 Recommended changes to the flow

**I1 — Split the binary decision into four tiers.** The flow's `Can we confidently understand it? YES → DB / NO → AI` leaves the biggest saving on the table: a **cache tier before the parser**. People eat the same meals repeatedly, so hashed-input and per-user recents lookups should resolve 35–45% of traffic at zero cost and sub-100ms — better than the parser on both axes. Likewise, splitting "AI" into a cheap extraction model and an expensive fallback keeps the large model under 5% of calls. (§7.1)

**I2 — Make "confident" a calibrated number, shipped in shadow mode.** As written, the confidence gate is the load-bearing element of the whole cost model but is undefined. Define it (coverage, match score, **match margin**, quantity presence), run T1/T2 in parallel for two weeks, and calibrate against real correction rates before trusting the parser alone. (§7.2)

**I3 — Prefer estimate-and-correct over blocking clarification questions.** This is my main disagreement with the flow doc. Step 5 proposes asking "How much rice did you have? 1 cup / 1.5 cups / 2 cups" when quantity is missing. That costs a full round trip and an extra decision on the app's *fastest* logging path — against priority #1. The design already shows the better pattern: assume a sensible default, render it in an editable preview, and let the user tap to fix. So: **assume, mark it `est.`, and let the preview be the clarification.** Ask a blocking question only when the *food itself* is unidentifiable, not when the quantity is. Users who don't care get a good-enough log in one tap; users who do care see exactly which number to correct.

**I4 — Enforce the "AI never returns numbers" principle in the schema.** The flow states the principle; make it structural. If the response schema has no nutrition field, hallucinated nutrition is impossible rather than merely discouraged. Same for foods: the model returns a *name*, and local search resolves it — the model can never invent a database entry. (§7.3)

**I5 — Never send the food database to the model.** Not stated in the flow, but it's the most common way this architecture gets expensive. Two-stage extract-then-resolve keeps prompts at ~400 tokens instead of tens of thousands. (§7.3)

**I6 — Specify raw/cooked yield factors as a first-class rule.** The flow mentions capturing raw/cooked state but not the conversion. Rice roughly triples in mass when cooked; getting this wrong is a ~200% error, larger than any parser inaccuracy. Yield-factor table + per-food default state + visible assumed state. (§8)

**I7 — Make portion adjustment purely client-side.** The flow ends at "user can edit," which implies a recalculation round trip. Since nutrition is linear in quantity, ship per-unit vectors to the client and recompute locally at 60fps, persisting once on commit. Removes the single highest-frequency network call in the feature. (§5.2.1)

**I8 — Add the miss-queue flywheel.** The flow treats the database as a given. In practice, unmatched foods — especially Indian and regional dishes — are the main accuracy ceiling. Every unresolved item should auto-file into a curation backlog ranked by frequency, so the catalog improves in proportion to actual demand. (§9, `FoodMissQueue`)

**I9 — Instrument cost per logged meal from day one.** The flow optimizes for cost but defines no measurement. `ParseEvent` with tier, tokens, latency, and correction outcome is what makes the routing thresholds tunable instead of guesswork. (§9, §14)

**I10 — Add a degradation path.** No fallback is specified if the LLM provider is slow, down, or costs spike. A circuit breaker to T1-only keeps quantified logging alive during an outage; the chat should degrade, not disappear.

### 16.3 LLM role audit (v1.2)

A review of what the LLM was actually specced to do surfaced five gaps. The doc had defined the
model's job for the **first message** and left the **conversation** — editing, intent, slot,
naming, chit-chat rejection — leaning on it implicitly with no contract.

| # | Finding | Resolution |
|---|---|---|
| **A1** | **The schema couldn't represent editing.** §7.3's `[{food, qty, unit…}]` list has no way to express "remove the dressing" (an intent + a reference to an existing item), "for breakfast I had…" (a slot), or "small bowl" (a size qualifier). The flagship text-editing behavior in §5.1.1 had **no pipeline path**. | Replaced the food list with an **intent envelope** carrying `intent`, `targetRef`, `slot`, `mealName`, and per-item `sizeQualifier`. Added §7.5 intent dispatch and a T1 edit grammar so most edits never reach a model. Still no nutrition fields — the core principle is intact. |
| **A2** | **Meal naming forced LLM calls onto the free paths.** §5.1.3 said "the AI names the meal," but 55–70% of parses never touch a model. Naming would have required a call purely for a label. | Naming is now **deterministic by default** (template from resolved items); the model's `mealName` free-rides on calls that happen anyway. Never a call on its own. |
| **A3** | **Non-food messages burned quota to be rejected.** "Hey, how's it going" scores ~0 coverage at T1 → escalates to T2 → costs a model call and a quota decrement to classify chit-chat. | Added **T-1, a local non-food pre-classifier** ahead of the router. The §13 redirect now fires with no model call and no quota. |
| **A4** | **Compound dishes were in the wrong layer.** T2 owned decomposing "chicken quinoa bowl" into four items — meaning the ingredient breakdown could vary run to run. Non-deterministic nutrition entering through the side door, contradicting the header principle. | Moved to a **`CompositeFood` catalog lookup** (§7.6). The model identifies the dish *name* only. Unknown dishes render low-confidence and file to the miss-queue rather than varying silently. |
| **A5** | **Corrections cost quota.** A text edit on an open draft could hit T2 and decrement the counter — charging the user to fix the assistant's own mistake, which pushes them back to tapping and makes the feature feel punitive exactly when it erred. | **Draft edits are quota-exempt** (§5.1.4). Only `LOG_NEW` parses count. |

**What held up:** LLM as exception layer, structurally-impossible nutrition hallucination,
extract-then-resolve, tiered routing with cache-first. The architecture was sound; the contract
was incomplete.

### 16.4 Non-logging coverage audit (v1.3)

| # | Finding | Resolution |
|---|---|---|
| **B1** | **All non-meal input hit one bucket.** `OTHER` → "I'm here to log meals." Applied equally to "thanks", "how do I change my goal?", and "how many calories do I have left?" — the last of which is answerable from data already on the Home screen. Deflecting it is the most user-hostile thing this surface could do. | 13-intent taxonomy (§5.5). `DIARY_QUERY` answers from local reads; `APP_HELP` deep-links; `SOCIAL` gets one warm line. |
| **B2** | **Nutrition questions had no defined boundary.** §2 excluded nutrition Q&A, but the greeting promises "I'll figure out the calories," so users will ask. No spec meant the model would improvise. | Narrow band: the assistant **reports what a food contains** (catalog lookup) but **never judges whether it's good for this person** (§5.5). |
| **B3** | **No wellbeing handling whatsoever.** A calorie tracker with a chat surface is a predictable place for disordered-eating patterns to appear, and the spec would have answered with "I'm here to log meals." | §5.6 — supportive response, no numbers, locale-appropriate resources, logging access preserved, gamification suppressed, deliberately escalated to T3. Flagged for clinical/T&S review before build. |
| **B4** | **Prompt injection unaddressed** on a user-controlled surface feeding a model. | Documented that the intent envelope contains the blast radius — no nutrition, response, or action fields exist to hijack, and the preview gates anything logged (§5.5). |

### 16.5 Architecture review (v1.4)

Reviewed by engineering before implementation; 18 points raised, all incorporated. The two that
were **defects rather than gaps**:

| Finding | Why it mattered |
|---|---|
| **Mutable catalog silently rewrote history.** §6 made totals a live sum over catalog values, so a single correction to a common food would retroactively change every past meal containing it — quietly corrupting Insights trends with no user-visible cause. | Fixed by freezing a nutrition snapshot per `LoggedMeal` plus `catalogVersion` (§12.3) |
| **Auto-decomposing unknown dishes had false precision.** An AI-generated four-row ingredient list is visually identical to the deterministic path, so users could log invented ingredients without knowing. | Replaced with a single flagged estimated-dish item; breakdown requires explicit opt-in (§7.6) |

Also raised and accepted: draft optimistic locking, message-level idempotency, an explicit draft
state machine with operation history, full persistence of `prep`/`sizeQualifier`, a schema+domain
validation layer, server-authoritative nutrition on persist, banded food-resolution confidence,
versioned cache keys, atomic quota consumption, a correlation chain for tracing, a fixed
evaluation set with nutrition MAE as the release gate, and explicit privacy/retention decisions.
**Streaming was dropped for v1** — the payload is too small to justify SSE.

**Not raised by the review, added here:** the evaluation framework needs **safety cases with a
false-negative target** for `WELLBEING_FLAG` (§5.6). Missing a wellbeing signal is a different
class of failure from a mis-parsed portion and is invisible in every accuracy metric proposed
(§12.11).

### 16.6 Second architecture review (v1.5)

| Finding | Resolution |
|---|---|
| **The estimated-dish calorie value had no source.** v1.4's fix for false precision replaced a fabricated ingredient list with a single `~450 kcal` — which was itself model-derived, contradicting the header principle. The v1.4 fix had hidden the fabrication behind one number instead of four rows. | `DishCategoryProfile` (§7.6.1): the model supplies a **category and serving size**; the engine computes from curated per-100g bands. Displayed as a **range**, counted in daily totals at the midpoint, excluded from MAE scoring. No confident category → no number. |
| **One-open-draft was product intent with no enforcement** — only a `userId + status` index, so multi-device races could create two. | Partial unique index on `("userId") WHERE status = 'OPEN'`. Constraint, not application logic. |
| **Idempotency was specified but not modelled** — no `clientMessageId` anywhere. | `ChatMessage.clientMessageId` + a dedicated `IdempotencyRecord` storing the **original response body**, since replay must return the same draft state, not merely detect a duplicate. |
| **Offline sync was asserted, never defined.** | §12.12 — ordered queue, halt-on-failure replay, catalog-drift reconciliation, 7-day staleness prompt, capped retries, never silent drops. |
| **`yieldFactor` invited double application** across `CompositeFood` and the per-food yield table. | **Removed the field.** Components are defined as-served; raw→cooked happens once at curation. Made impossible by construction rather than documented as a caveat. |
| **`UserServingPreference` mixed contexts** — raw and cooked rice collapsing into one median. | Keyed by `(userId, foodId, state, unit)` with a two-level lookup that falls back to a coarser key below 3 observations, so the finer key doesn't fragment the median into uselessness. |
| **Partial parses could log silently.** | §12.13 — unconsumed input renders verbatim as an explicit gap; `Confirm & Log` stays enabled because a visibly partial log beats no log. |
| **§7.6 / §13 contradiction** on unknown dishes. | Aligned to the single estimated-dish row. |

### 16.7 Contract review (v1.6)

Five contract-level blockers, all correct. Two were latent duplicate/deadlock bugs rather than
documentation gaps:

| Finding | Resolution |
|---|---|
| **Idempotency TTL (24h) was shorter than the offline replay window (7d).** A queued op syncing on day 5 would find no idempotency record and create a **duplicate meal** — the precise failure idempotency exists to prevent. | Both derive from one config value: `REPLAY_WINDOW = MAX_QUEUE_AGE (30d) + retry tail (1d)`. They must never be tuned independently (§9). |
| **Nothing transitioned `OPEN → EXPIRED`.** Combined with the partial unique index from v1.5, a stale draft would have **permanently blocked the user from starting a new one** — the constraint and the missing transition were individually fine and jointly a deadlock. | Transactional lazy expiry at draft creation (authoritative, cron-independent) plus a sweeper for hygiene (§12.2). |
| **Streaming referenced in three places** after §10 declared v1 non-streaming. | Single contract: no server-pushed partials anywhere; the typing indicator is client-rendered and needs no server signal. |
| **Every `MealDraftItem` field was mandatory**, but unresolved items have no grams, mass source, or match score, and estimated dishes had nowhere to persist `dishCategory` or the range. | `ItemResolution` discriminator (`RESOLVED` / `ESTIMATED_DISH` / `UNRESOLVED`) with explicit per-state required/null field rules enforced in §12.4, plus persisted `kcalLow/High/Midpoint` and `profileVersion`. |
| **Offline op payload was partially specified.** | Full `QueuedOperation` interface (§12.12): `opId`, `sequence`, `opType`, local/server draft refs, `mealTimestamp`, payload, catalog/engine versions, snapshot, retry state. |

**Added beyond the review: local→server ID mapping.** An offline draft is created against a
client-generated `localDraftId` and the server assigns the real ID only when `CREATE_DRAFT`
syncs — so every later queued op references an ID the server has never seen and must be rewritten
during replay. Follows directly from the queue contract but wasn't called out.

---

## 17. UI To-Dos

Design work required before build, derived from §16 plus the quantity/unit decisions in §5.1.1
and §5.2. **P0** blocks implementation; **P1** ships in v1; **P2** is a fast follow.

### 17.1 Meal Assistant (chat)

| # | Priority | Item | What's needed |
|---|---|---|---|
| **U1** | **P0** | **`Confirm & Log` placement** | Currently occluded by the composer. Pin as a sticky footer above the composer, **or** collapse the composer to a compact "Edit by typing" affordance while a draft is open. This is the conversion button — needs an explicit decision, not a layout accident. (G4) |
| **U2** | **P0** | **Item row: quantity provenance** | `120g` currently reads as fact. The row must convey both provenance axes (§5.1.1a): an `est.` chip when `quantitySource = ASSUMED`, and a provenance line written from `massSource` ("Your usual portion" / "Typical serving" / "1 katori ≈ 150g"). The quantity must also read as tappable, not as static subtitle text. |
| **U3** | **P0** | **Meal-slot chip** | Tappable chip on the card header ("Lunch ▾"), defaulted from local time. Nothing in the design currently says where the meal lands. (G6) |
| **U4** | **P0** | **Unresolved-item row** | Distinct row state: warning icon, no calorie value, `Find this food` action. Must **not** block the other items from logging. Will fire often for regional foods. (G8) |
| **U5** | **P1** | **`+ Add food` row** | Subtle row at the bottom of the preview card routing into Meals search. Text-only ("add an egg") is discoverable by almost nobody. (G10) |
| **U6** | **P1** | **Clarification chip row** | Tappable option row for the high-impact case only ("How much ghee? ½ tsp · 1 tsp · 1 tbsp · Enter amount"). Rare by design (§15, I3), but the component must exist. (G7) |
| **U7** | **P1** | **Empty / first-open state** | Greeting + 3 tappable example prompts. Teaches the input format at zero AI cost. |
| **U8** | **P1** | **Error, offline & quota-exhausted states** | One component per row in §13. Only the happy path is currently drawn. (G8) |
| **U9** | **P1** | **Quota pill states** | Neutral / amber ≥80% / red at 100%, plus a tap tooltip explaining what counts. Blocked on the §5.1.4 decision. (G5) |
| **U10** | **P2** | **Greeting personalization** | `user.firstName` with a guest fallback ("Hi there 👋"). Mock reads "Hi Ava" against an "Alex Chen" account. (G3) |
| **U11** | **P2** | **Superseded-draft treatment** | Older preview cards in the scrollback need a visually read-only state once a newer draft supersedes them. |

### 17.2 Adjust Portion

This screen needs the most work. It is currently designed for one measurement case out of three —
a continuous gram slider — which is wrong for countable and household units.

| # | Priority | Item | What's needed |
|---|---|---|---|
| **U12** | **P0** | **Unit-aware controls** | The control set must vary by `unitType` (table below). A slider is nonsense for countable foods. |
| **U13** | **P0** | **Open in the user's unit** | The screen opens in the unit the user *spoke in*, not grams. If they said "a bowl of rice", a gram slider makes them do a conversion the app should be doing. Grams remain visible as secondary text in every case — it's the trust anchor. |
| **U14** | **P0** | **Unit switcher** | Affordance to change display unit, drawn from `food.displayUnits`. Switching **converts, never resets** (150g → 0.95 cup → snaps to 1 cup, gram equivalent updating visibly). |
| **U15** | **P0** | **Quick-select chips: fix math & hierarchy** | Values must be true multiples of `defaultGrams` (mock shows `1/2 = 75g` against a 120g default). Flip the hierarchy so the unit-natural value is primary and the multiplier secondary — "1½ cups" is what people think; "1.25×" makes them recall the default. (G2) |
| **U16** | **P0** | **Deltas computed, not authored** | Both current deltas are wrong (`↑42` should be `+49`; protein `46g ↓10` should be `+9` and cannot decrease when the portion increases). Fix the mock so QA doesn't inherit bad expected values. (G1) |
| **U17** | **P1** | **Raw/cooked toggle** | Where the food has yield factors, the assumed state must be visible and correctable. Largest single accuracy lever in text logging (§8, I6) and currently unaddressable in the UI. |
| **U18** | **P1** | **Fiber cell** | Add to Updated Nutrition. Fiber is first-class on Home rings and Onboarding targets but absent here. (G11) |
| **U19** | **P1** | **ⓘ → nutrition source sheet** | Source database, reference entry name, per-100g basis, "Report incorrect data". Also satisfies USDA / Open Food Facts attribution obligations. (G9) |

**Control matrix by unit type** — drives U12:

| `unitType` | Examples | Control | Step | Quick chips |
|---|---|---|---|---|
| **Continuous** | g, ml | Slider + steppers | 5g / 10ml | Multipliers (½× · 1× · 1.5× · 2×) |
| **Countable** | eggs, rotis, slices | **Steppers only — no slider** | 1 (0.5 where sensible) | Absolute counts (1 · 2 · 3 · 4) |
| **Household** | cup, katori, bowl, tbsp | Steppers + optional slider | 0.25 | Natural fractions (½ · 1 · 1½ · 2) |

### 17.3 New components (no existing design)

Three things need designing from scratch rather than adapting: the **unresolved-item row** (U4),
the **clarification chip row** (U6), and the **error/offline/quota state set** (U8).

### 17.4 Accessibility to-dos

Carried from §14, listed here so they're picked up in design rather than retrofitted:

- Slider needs an adjustable accessibility role announcing value and unit ("Portion size, 150 grams, adjustable").
- Deltas need a text sign (`+49`), not arrow colour alone.
- `est.` chip needs a real label ("estimated quantity — tap to adjust"), not a decorative badge.
- Quick-select chips convey selection via `accessibilityState.selected`, not tint.
- Preview card reflows rather than truncates item names under large dynamic type.

### 17.5 Summary

The chat screen is close — it mostly needs **additions** (provenance, slot chip, add-food, new
states). The portion screen needs a **structural rethink** around unit type before it can be built.

---

## 18. Open Questions

1. **Quota semantics** — is `0/20` daily or monthly, per message or per AI parse, and is it the free-tier gate for premium conversion? (§5.1.4 has a recommendation; needs product sign-off.)
2. **CTA vs. composer** — does the composer collapse when a draft is open, or does the CTA float above it? (G4)
3. **Clarification questions** — accept I3 (estimate-and-correct) as the default, or keep blocking clarification for missing quantities?
4. **Meal slot** — time-inferred (recommended) or always explicitly chosen?
5. **Session semantics** — is the chat a persistent scrollback the user can revisit, or does it reset per meal? Affects `ChatSession` retention and storage cost.
6. **Model + provider** for T2/T3, and whether any parsing can run on-device for the offline path.
7. **Launch locales and cuisines** — this determines how much curated catalog work precedes launch. Indian household measures are the largest gap.
8. **Confidence thresholds** — who owns calibration after the shadow-mode period, and where do the values live (remote config assumed)?
9. **Data retention** — how long do we keep raw chat messages? They contain user-authored text; a shorter TTL on `ChatMessage` than on `LoggedMeal` is probably right.
10. **Photo logging handoff** — the camera flow should share this preview + confirm surface. Confirm before either is built separately.
11. **Composite-food seeding** — how many dishes must be curated in `CompositeFood` before launch, and which cuisines first? This gates how often the low-confidence fallback path (§7.6) fires.
12. **Wellbeing policy ownership** — §5.6 is written as a requirement, not a finished policy. Who signs it off, and what are the India-market resources? This blocks build on that path.
13. **Historical recompute policy** — §12.3 freezes past meals to a snapshot. Confirm that's right for Insights, and decide whether users are ever notified when a catalog correction changes what a past meal would compute to today.
14. **Golden set ownership** — who curates the evaluation set (§12.10), and what MAE thresholds gate a release? This blocks the first parser change, not the first build.
15. **`DishCategoryProfile` coverage** — how many dish categories ship at launch, and who curates the per-100g bands? Too few categories makes the estimate meaningless; the p25–p75 spread per category is the honest measure of whether a category is useful at all.
16. **`NUTRITION_QA` boundary** — is "is quinoa high in protein?" (comparative, catalog-derivable) answerable, or does it fall on the judgement side of the line? Needs a written test set before implementation.
14. **Quota exemption abuse** — could a user chain "edits" to get unlimited free parses? Likely bounded by the one-draft-at-a-time rule, but needs a cap (e.g. 15 edits per draft) confirmed.

---

## 19. Future / Out of Scope

- **Photo → meal** recognition feeding the same draft/preview surface.
- **Voice input** — arguably a better fit than typing for this use case; same pipeline downstream.
- **"Same as yesterday"** / one-tap re-log of frequent meals directly from the greeting.
- **Historical queries** ("what did I eat Tuesday?") — requires read access to the diary and a different response type.
- **Proactive nudges** ("you're 40g short on protein — want a suggestion?") — retention lever, but needs care to stay clear of anything resembling diet coaching.
- **Restaurant/branded dish matching** from descriptive text ("a McChicken and medium fries").
- **User-corrected food data** feeding back into the catalog with moderation.
