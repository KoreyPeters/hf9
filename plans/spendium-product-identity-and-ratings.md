# Spendium — Product Identity, Receipt Pipeline, and Ratings

**Status:** active plan. Supersedes `receipt-parsing-design-draft.md` entirely.
Reconciles `spendium-purchase-data-architecture.md` and `local_only/product-rating-plan.md`,
which conflicted on retention, resolution ownership, and where matching happens.

---

## The goal this plan is measured against

Thousands of players upload receipt images. From the very many surface forms a single
product can take on a receipt, the system must reach a canonical product record — as often
as possible, for as many players as possible, with as little admin escalation as possible —
and it must get better at this over time without human curation.

Every decision below is justified against that sentence.

---

## Settled decisions

### 1. Retention — 30-day player-linked window, then anonymise

The two source documents disagreed: the architecture doc specified a hard seven-day delete;
the product-rating plan is built throughout on a 30-day window. Three things settle it for 30:

- The product-rating plan is roughly seven weeks newer.
- The published privacy policy (`templates/spendium/privacy.html:138`) already commits to
  retaining purchase records "while your account is active", with player-controlled deletion.
  A 30-day window sits well inside a commitment already made publicly; the seven-day rule was
  materially stricter than what players were told.
- The seven-day window's stated purpose was "fraud detection and points dispute resolution".
  It was written before rating existed as a requirement, so it was never a considered
  rejection of a longer window.

The reconciliation is cleaner than it first appears, because the product-rating plan does not
ask for deletion at 30 days. It says the player's "personal connection to it is severed" —
that is anonymisation, which is exactly the architecture doc's Layer 1 → Layer 2 transition,
at a different number. The two-layer design survives intact.

**Layer 1 (transactional, player-linked, 30 days).** Full purchase record: store, line items,
amounts, timestamps, match state. Supports points, disputes, fraud checks, rating, and
disambiguation. At day 30 a scheduled task nulls the player FK and stamps `anonymised_at`.

**Layer 2 (analytical, anonymous, permanent).** Written at anonymisation time, not at
processing time — see below. All line items from one purchase share a randomly generated
UUID token with no derivation from any player identifier, preserving basket analysis.

**Change from the architecture doc:** the OLAP write moves from *processing time* to
*anonymisation time*. As written, an anonymous row created at processing time under an
unlinkable token could never absorb the player's later disambiguation corrections — the
analytical layer would permanently record product identities the player had already fixed.
Writing at day 30 means the row carries the corrected, settled identity. This costs nothing;
the analytical layer has no real-time consumer.

**Hard constraint neither source doc mentions:** the privacy policy commits twice
(`privacy.html:53`, `:139`) to deleting receipt images **within 24 hours of processing**.
This must be an explicit pipeline step, and it forecloses any design that re-reads the
original image later. Only a perceptual hash survives, for duplicate detection.

### 2. Resolution — the purchasing player, never the community

The superseded draft routed ambiguous items to Polium for community voting. That is wrong on
two counts: it imports Spendium product-identity work into the ethical-voting game, and it
ignores that the buyer is a strictly better source. The product-rating plan's argument stands:
"the player held the product. Their description is the highest-confidence signal available
anywhere in the system."

- **During the 30-day window** — the purchasing player resolves, via disambiguation prompts.
- **After the window closes** — unresolved records accumulate evidence passively from future
  matching purchases and cross-player confirmations. No admin involvement.
- **Admin queue** — contested merges only, where multiple players have flagged a conflict.

### 3. Matching — a separate stage after Gemini, structured as a cascade

Gemini interprets; it does not match. Confidence is a property of the match against our
catalogue, not of Gemini's reading of the receipt — Gemini can be entirely correct about what
a product is and still match weakly, simply because no equivalent record exists yet.

The decisive argument is retro-matching, and it follows directly from the 24-hour image
deletion. Once the image is gone, `raw_text` is the only durable artifact of the purchase. If
matching happens inside the Gemini call, a match is a one-shot event welded to an image we no
longer hold — an item that failed to match in July can never be re-matched in December, however
much the catalogue has grown. If matching is a separate stage over stored `raw_text`, it can be
replayed indefinitely, in batch, at zero API cost. **That property is what makes the system
compound rather than merely accumulate**, and in-prompt matching cannot provide it at any price.

Consequences for the Gemini call: no per-request catalogue context block, and `catalogue_id` /
`match_type` come out of the response schema. It becomes pure extraction.

### 4. Build order — identity layer first

`spendium/models.py` currently contains one model, `SpendiumWaitlist`. There is no Store,
Purchase, or Product. Both source documents argue foundation-first themselves: the product
plan opens §1 with "the foundation everything else rests on. A rating is meaningless without a
stable, deduplicated entity to attach it to," and §2 depends on the pipeline having already run.

This means the first work delivers no receipt scanning. Everything downstream writes into the
catalogue, so building the catalogue last means building everything twice.

---

## The matching cascade

Four tiers. Each handles only what the tier above could not.

### Tier 0 — Exact alias lookup (deterministic)

**This is where convergence comes from, and it is the single most important mechanism in the
plan.** The source document was ambiguous about the search key — §1 describes aliases as "all
known receipt strings", while the improvement section describes matching "similar Gemini
output". These are different systems and only one converges:

- Storing *Gemini's interpretations* as aliases stores paraphrases of `canonical_name` against
  a record already called that. Near-zero information gain. The hard fact — that this retailer
  prints `TP-COLG-250` — is never captured, every receipt re-runs the same probabilistic
  inference, and run-to-run model variance produces inconsistent prompts for identical input.
  **This does not converge.**
- Storing *raw receipt strings scoped by retailer* makes
  `(Shoppers Drug Mart, "TP-COLG-250") → prod_a1b2` an exact hash lookup: deterministic, O(1),
  no embeddings, confidence 1.0, and model variance bypassed entirely on the hot path.

So: **the primary alias key is the normalised raw receipt string, scoped to the retailer**,
with an unscoped global tier as weaker fallback. Gemini's interpreted name is a *secondary*
key used only at Tier 1.

The economics this produces are the reason the goal is achievable:

> Each distinct `(retailer, raw_string)` pair needs to be confirmed **once, by one player,
> ever.** Player 1 confirms it; players 2 through 10,000 never see a prompt for it.

A fixed confirmation cost amortised across the whole player base, against unbounded future
volume. Because retail purchasing is heavily power-law distributed, the head of each retailer's
catalogue is confirmed quickly, and per-receipt prompt rate decays steeply before settling into
a long, low tail. Admin escalation is near zero by construction, because players resolve, not admins.

### Tier 1 — Fuzzy match on interpreted name and alias pool

For items with no exact alias hit. Normalised-token and trigram similarity (`rapidfuzz`)
against canonical names and the accumulated alias pool, producing a score that maps to
strong / weak / no-match bands we own and tune.

**We are not adding a vector database.** Both source documents assume vector search and cosine
thresholds; the project has SQLite, no embedding library, no FTS, and `icontains` search
(`polium/views.py:126`). Tier 0 carries the load, and fuzzy matching will go a long way on the
residual before embeddings are justified. Defer embeddings until metrics show fuzzy matching is
the binding constraint.

### Tier 2 — Targeted Gemini adjudication (the second call)

For the residual that survives Tiers 0 and 1: no alias hit, no strong fuzzy match.

A **second, narrowly scoped Gemini call** takes just those few line items, each with its top-K
catalogue candidates, and asks the model to adjudicate. This is the superseded draft's
in-prompt idea applied at the right scope — to the handful of genuinely hard cases rather than
to every line item on every receipt.

It is cheap (a few items, no image, text only), it plays to what the model is actually good at
(world knowledge disambiguating `PC BLK LBL COFF 300G` against a candidate list), and every item
it resolves is an item that would otherwise have become a player prompt. It converts the
system's most expensive resource — player attention — into an API call costing fractions of a cent.

Adjudication returns a chosen candidate or an explicit "none of these". A confident choice here
enters as a **provisional** alias (see below), not an authoritative one — the model is a strong
signal, not a confirming witness.

### Tier 3 — Player prompt (budgeted)

What remains goes to the purchasing player. Two rules, both departures from the source plan:

**Budget prompts per receipt — 3 to 5 maximum.** The source plan prompts on *every* weak match.
A 40-line grocery receipt under loose thresholds could show fifteen icons, and players will
ignore all of them — the plan warns of exactly this risk in its strong-match section
("train players to ignore disambiguation prompts everywhere"). Because unresolved items remain
rateable, ignoring prompts is free for the player and costly for us.

**Prioritise by global resolution value, not per-player confidence.** The source plan sorts
disambiguations "lowest confidence first". That is the wrong sort key. A raw string blocked on
500 pending line items across the player base is worth roughly 500× a one-off. Rank by:

1. Count of unresolved line items across all players sharing this raw string
2. Purchase volume of the candidate product
3. Confidence gap between top candidates (genuine ambiguity beats near-certainty)

### Fallback — unverified record, still rateable

Anything unresolved creates an unverified record using the interpreted name, flagged by
`confidence_source`. All products are rateable regardless of match confidence; the
disambiguation prompt is an invitation, never a gate.

---

## Alias integrity

### Two confirmations before authority

Raw-string aliasing is powerful precisely because it is deterministic — which means a single
wrong confirmation propagates that error to every future receipt containing the string, with
high confidence and no prompt. The source plan has no reversal mechanism; its self-correction
argument ("a garbled record tends to sit orphaned") detects *unused* records, not
confidently-wrong ones. At thousands of players with one-tap confirmation, mis-taps are certain.

- **Provisional** — created by first player confirmation or Tier 2 adjudication. Used for
  matching, but items matched this way stay eligible for prompting at reduced priority.
- **Authoritative** — promoted after a second independent confirmation from a different player.
  Suppresses prompting entirely.
- **Demoted** — a contradicting confirmation drops an alias back to provisional and reopens it.
  Repeated contradiction flags it for the admin merge queue.

This is the one failure mode that gets *worse* as the system grows more confident, so it needs
the mechanism from the start rather than as a later hardening pass.

### Auto-clustering of unverified duplicates

Two players independently creating "Colgate Bright Whitening 250ml" and "Colgate Toothpaste
Whitening 250 mL" splits ratings across two records. The source plan routes merges to admins —
the one queue it promises stays small. At scale, duplicate creation can outpace that assumption.

So: before creating a new unverified record, match the proposed name against **existing
unverified records** as well as verified ones, and auto-merge above a high similarity bar when
both sides are unverified. Only verified-record merges and contested cases reach a human.

### Ratings follow merges

The data model has `merged_into`, but nothing states that ratings attached to a retired record
roll up to the canonical one. If they do not, every merge silently destroys rating data — and
merges are meant to be routine. Rating aggregation must resolve `merged_into` transitively.

---

## Retro-matching

A scheduled task re-runs Tiers 0 and 1 over stored `raw_text` for all unresolved line items,
including those past their 30-day window. This is the compounding mechanism: a catalogue
improvement made for one player silently clears backlog for every player, forever, with no image
access and no player involvement.

Retro-matching may only *fill* an unmatched item or *strengthen* a provisional match. It must
never overwrite a player-confirmed identity, and it must never alter settled points.

---

## Data model

New app: `spendium` gains real models. Follow existing project patterns — `SqidMixin` on
anything public-facing, business logic in models and service modules, thin views.

**Product identity**
- `Manufacturer` — name, sqid, lifecycle
- `ProductCategory` — taxonomy for products (distinct from `surveys.Category`, which scopes
  survey criteria)
- `Product` — `canonical_name`, `status` (verified/unverified/retired), `merged_into` (FK self),
  `upc`, `manufacturer` FK, `category` FK, `confidence_source`
  (gemini/upc_lookup/admin/player), `reformulated_at` (nullable, deferred), sqid
- `ProductAlias` — `product` FK, `store` FK (nullable for global aliases), `raw_text_normalised`
  (indexed), `source`, `status` (provisional/authoritative/demoted), `confirmation_count`,
  `created_at`. Unique on `(store, raw_text_normalised)`.

**Purchase**
- `Store` — brand-level per the existing Stage 1 audit, sqid, lifecycle
- `Purchase` — `player` FK (**nullable**, nulled at anonymisation), `store` FK, `purchased_at`,
  `total`, `image_phash`, `anonymised_at`, `purchase_token` (UUID, set at anonymisation)
- `PurchaseLineItem` — `purchase` FK, `raw_text`, `interpreted_name`, `product` FK (nullable),
  `match_tier`, `match_confidence`, `quantity`, `unit_price`, `line_total`,
  `disambiguation_state`

**Ratings** reuse the existing generic survey engine — `SurveyResponse` and `PointTransaction`
already attach to arbitrary models via GenericForeignKey, so `Product` becomes a survey subject
with no changes to the survey app. `compute_rating` should work as-is. Criteria are a governance
output; the engine must receive whatever the membership decides, with founder-set opening
criteria explicitly temporary.

**Response model note:** the source plan lists "receipt reference" on the rating response. If
purchases anonymise at day 30, a rating holding a receipt FK would resurrect the player link.
Ratings keep the *product* reference; the receipt reference must be dropped or anonymised.

---

## Metrics

The system's entire premise is that it improves. Neither source document has a metrics section,
and without these there is no way to distinguish convergence from stagnation.

- **Alias hit rate** — % of line items resolved at Tier 0, trended per retailer. The headline
  convergence metric.
- Tier distribution per receipt (how much each tier is carrying)
- Prompt rate per receipt, and prompt *completion* rate — the fatigue detector
- New unverified records per 100 line items — should fall
- Duplicate auto-merge rate — should stay flat, not grow
- Alias demotion rate — the poisoning detector
- Tier 2 adjudication accuracy, sampled against later player confirmations

---

## Abuse controls

Salvaged from the superseded draft, rewritten for the actual stack. The draft specified
Firestore and BigQuery ML; the project runs SQLite with Litestream and a custom Cloud Tasks
decorator, and has no such dependencies. These are SQLite-native and adequate:

1. **Receipt image perceptual-hash dedup** at submission — cheapest high-value check, catches
   lazy abuse immediately. Hashes are retained even though images are deleted at 24 hours.
2. **Submission velocity check** — hold beyond N receipts per hour.
3. **High-value receipt hold** — configurable threshold, pending state plus reviewer notice.
4. **Negative line item suppression** — no points awarded or subtracted.

The draft's percentile-based risk tiering and isolation-forest model are deferred until real
player data exists; its own recommendation was 4–6 weeks of observation before activating
tier-based controls. Its decision to treat buy-and-return fraud as out of scope stands.

Receipt scanning remains members-only, per the draft.

---

## Open questions

- **Prompt budget** — is 3–5 per receipt right? Needs calibration against real completion rates.
- **Tier 0/1 thresholds** — the strong/weak/noise-floor bands need a labelled test set built
  from real receipts before they can be set meaningfully.
- **Product line vs SKU granularity** — §1 specifies product-line level with size variants
  collapsing, but the canonical name format includes size and receipts carry it. Decide whether
  size is part of identity; it changes alias pooling and match thresholds.
- **Two k-thresholds, not one** — the architecture doc sets k-anonymity at "minimum 10
  purchases" before publishing; the product plan leaves minimum *responses* open. Different
  units, both needed: one gates the anonymity guarantee, the other gates rating display.
- **Cold start per retailer** — first player at a new chain faces a wall of prompts, front-loaded
  onto early adopters. Seed from open product databases (Open Food Facts, UPC Item DB) before
  launch? The prompt budget caps the damage but does not remove it.
- **Points for rating** — bonus on top of purchase points, or tied to them?
- **Unverified rating weighting** — are ratings without a receipt anchor allowed at all?

---

## Todo steps

### Phase 1 — Product identity layer

- [ ] Create `Manufacturer`, `ProductCategory`, `Product`, `ProductAlias` models in `spendium`
- [ ] Add `SqidMixin` to `Product` and register salts in `settings.SQID_SALTS`
- [ ] Unique constraint on `ProductAlias(store, raw_text_normalised)`; index the raw column
- [ ] Write `normalise_raw_text()` — case, whitespace, punctuation, common receipt noise
- [ ] Migrations
- [ ] Django admin for all four models, including the merge action
- [ ] Model-level tests for alias status transitions and `merged_into` resolution

### Phase 2 — Store and purchase models

- [ ] Create `Store` (brand-level, sqid, lifecycle) and admin
- [ ] Create `Purchase` (nullable player FK, `image_phash`, `anonymised_at`, `purchase_token`)
- [ ] Create `PurchaseLineItem` with match state fields
- [ ] Migrations and admin
- [ ] `anonymise_purchase` task — nulls player FK, sets token, writes Layer 2 row at day 30
- [ ] Schedule the anonymisation task at write time via the existing `@task` / `enqueue` system
- [ ] Tests: anonymisation severs the player link and preserves line-item content

### Phase 3 — Matching cascade, Tiers 0 and 1

- [ ] `spendium/matching.py` — `match_line_item(raw_text, interpreted_name, store)`
- [ ] Tier 0: exact alias lookup, retailer-scoped then global
- [ ] Add `rapidfuzz` dependency
- [ ] Tier 1: fuzzy score against canonical names and alias pool; strong/weak/noise bands
- [ ] Build a labelled fixture set of real receipt strings for threshold tuning
- [ ] Tests running entirely offline, no API calls

### Phase 4 — Gemini extraction

- [ ] Add Vertex AI dependency and GCP config via `python-decouple`
- [ ] `spendium/extraction.py` — single multimodal call, pure extraction schema
  (no `catalogue_id`, no `match_type`)
- [ ] Post-extraction arithmetic checks (line sum vs subtotal, subtotal + tax vs total,
  plausible datetime, negative line flags)
- [ ] Retry at high resolution when `image_quality` is degraded/poor and arithmetic fails
- [ ] **Delete the image within 24 hours** — task scheduled at processing time; retain phash only
- [ ] Tests against recorded fixture responses, no live calls in CI

### Phase 5 — Tier 2 targeted adjudication

- [ ] `adjudicate_residuals(line_items, candidates)` — second text-only Gemini call
- [ ] Batch only Tier-0/1 misses; attach top-K candidates per item
- [ ] Schema allows explicit "none of these"
- [ ] Confident adjudications write **provisional** aliases only
- [ ] Track adjudication accuracy against later player confirmations
- [ ] Tests against fixture responses

### Phase 6 — Player disambiguation

- [ ] Disambiguation prompt UI on the purchase view (Datastar SSE partial, per existing patterns)
- [ ] Implement the prompt budget (3–5 per receipt)
- [ ] Implement global prioritisation ranking
- [ ] Three prompt states: weak match, no match, free-text entry
- [ ] Free text runs through the same matching cascade — never writes the catalogue directly
- [ ] Suppress candidates below the noise floor
- [ ] Tests for budget enforcement and ranking order

### Phase 7 — Alias integrity

- [ ] Confirmation flow: provisional → authoritative on second independent confirmation
- [ ] Contradiction demotes to provisional and reopens for prompting
- [ ] Repeated contradiction flags for the admin merge queue
- [ ] Auto-cluster new unverified records against existing unverified records
- [ ] Auto-merge above the high-similarity bar when both sides are unverified
- [ ] Rating aggregation resolves `merged_into` transitively
- [ ] Tests: poisoning scenario, merge rollup preserves ratings

### Phase 8 — Retro-matching

- [ ] Scheduled task re-running Tiers 0/1 over unresolved line items
- [ ] Guard: never overwrite player-confirmed identity, never alter settled points
- [ ] Tests: catalogue growth resolves historical backlog

### Phase 9 — Ratings

- [ ] Register `Product` as a survey subject via the existing generic engine
- [ ] Confirm `compute_rating` works unchanged for products
- [ ] Founder-set opening criteria, flagged temporary in the UI
- [ ] Criteria versioning so responses under different question sets stay distinguishable
- [ ] Product rating display, response count, trend
- [ ] Minimum response threshold before public display
- [ ] Ratings keep product reference only — no receipt FK

### Phase 10 — Action Centre

- [ ] Aggregate page: hot products, unresolved disambiguations, unrated products
- [ ] Navbar badge for genuinely new items, cleared on visit
- [ ] Email rules: onboarding sequence, then high-priority triggers only, max one per week

### Phase 11 — Metrics and abuse controls

- [ ] Instrument alias hit rate, tier distribution, prompt and completion rates
- [ ] Instrument new-record, auto-merge, and demotion rates
- [ ] Perceptual-hash dedup at submission
- [ ] Submission velocity check
- [ ] High-value receipt hold
- [ ] Negative line item suppression
- [ ] Gate receipt scanning to members

### Phase 12 — Documentation

- [ ] Update `spendium-purchase-data-architecture.md` — 30-day window, OLAP write at
  anonymisation time
- [ ] Update `local_only/todo.md` — product ratings no longer Stage 5 deferred
- [ ] Confirm the privacy policy still matches actual behaviour before launch
