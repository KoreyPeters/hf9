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

### 4b. Receipt capture is a published commitment, not just a feature

Added after Phase 6, when it became clear the plan had no way for a player to
create a receipt or see one. The privacy policy already promises both — visible
purchase history, deletion "via account settings", and export on request — so
these views are obligations rather than conveniences. See Phase 6b.

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

For items with no exact alias hit. Split into two stages, because scoring the whole catalogue
per line item does not scale: with a seeded catalogue of ~200k products and ~30 line items per
receipt, naive scoring is ~6M string comparisons per receipt, growing linearly with the
catalogue forever.

**Stage A — narrow (FTS5).** An FTS5 virtual table over canonical names and the alias pool.
Query with the interpreted name, take the top ~200 by BM25 rank. Indexed, C-speed, and BM25
supplies IDF weighting for free — "colgate" dominates the ranking while "toothpaste" and "ml"
contribute almost nothing. FTS5 is compiled into the bundled SQLite (verified: 3.50.4), so this
adds no dependency. The virtual table tracks `Product` via an external-content table with
triggers; Litestream replicates it normally.

**Stage B — score (rapidfuzz).** Score the ~200-candidate shortlist to produce final confidence
and the strong / weak / noise bands. ~6k comparisons per receipt rather than ~6M, and the cost
is bounded by shortlist size rather than catalogue size, so it stays flat as the catalogue grows.

The division of labour matters: FTS5 decides which records are *plausible*, rapidfuzz decides
*how good* the match is. Confidence remains ours to tune, which is the whole point of
separate-stage matching.

Supporting optimisations: batch all line items of a receipt so `rapidfuzz.process.cdist`
vectorises scoring; pre-filter by brand token against `Manufacturer`, since Gemini emits
`[Brand] [Product Name] [Variant]` and the leading token is usually the brand.

Note this cost profile *decreases* over time. At cold start the catalogue is seeded-large and
the alias pool is empty, so everything falls to Tier 1. As aliases accumulate, Tier 0 absorbs
the majority and Tier 1 volume drops. This is a launch-window problem and it is self-limiting.
Latency budget is generous regardless — receipt processing is a background task, not a request
cycle.

**We are not adding a vector database.** Both source documents assume vector search and cosine
thresholds; the project has SQLite and `icontains` search (`polium/views.py:126`). Tier 0 carries
the load and FTS5 + rapidfuzz covers the residual. Defer embeddings until metrics show fuzzy
matching is the binding constraint. If FTS5 were ever unavailable, the fallback is a
`ProductToken` table with document-frequency weighting — the same blocking strategy, hand-rolled.

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
  `manufacturer` FK, `category` FK, `confidence_source` (gemini/upc_lookup/admin/player),
  `reformulated_at` (nullable, deferred), sqid
- `ProductUpc` — `product` FK, `upc`. One-to-many: UPCs are SKU-level, so a product line with
  five sizes carries five UPCs. A singular field on `Product` breaks the moment UPC data is
  imported.
- `ProductAlias` — `product` FK, `store` FK (nullable for global aliases), `raw_text_normalised`
  (indexed), `source`, `status` (provisional/authoritative/demoted), `confirmation_count`,
  `created_at`. Unique on `(store, raw_text_normalised)`.

**Granularity — product line, not SKU.** `Product` is the product line and is the rating
subject. `canonical_name` follows `[Brand] [Product Name] [Variant]` and **excludes size** —
the superseded draft's format embedded `[Size]`, which is incompatible with collapsing size
variants into one record.

The reasoning: HF criteria measure ethical properties (labour, sourcing, environmental impact),
and a 250ml and 100ml tube of the same toothpaste are ethically identical. Rating at SKU level
would ask the same question repeatedly and split responses across records, so each variant takes
several times as long to clear the display threshold — fewer products publicly rated, for fewer
players. Collapsing concentrates responses and gets products rated sooner. It also removes an
entire class of disambiguation: the system never asks "250ml or 100ml?", and `TP-COLG-250` and
`TP-COLG-100` become two aliases converging on one record.

`[Variant]` is retained — Bright Whitening vs Cavity Protection is a real distinction that
manufacturers name distinctly, and is exactly where the naming-pressure mechanism applies.
Size is packaging, not identity. Observed size stays on `PurchaseLineItem` (implicit in
`raw_text` and `unit_price`) and remains available to Layer 2 basket analytics. No
`ProductVariant` model for MVP.

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

## Resolved decisions

**Granularity** — product line, size excluded from canonical name, `upc` becomes one-to-many.
See the data model section above.

**Cold-start seeding** — seed the catalogue, scoped. Import Open Food Facts filtered to
products available in Canada, grocery/pharmacy/household categories only, as `unverified` with
`confidence_source=upc_lookup`. Do **not** seed the alias table.

Seeding does nothing for Tier 0 — open databases supply product names and UPCs, never
`TP-COLG-250`. Its value is elsewhere: against an empty catalogue *every* first purchase
produces no match at all, which means maximum new-record creation, maximum duplicate
fragmentation, and the largest possible admin merge queue. Seeding converts "no match, create a
record" into "weak match, confirm this" — a better prompt and, more importantly, fragmentation
avoided. Crowd-sourced name inconsistency is acceptable; the alias mechanism is what resolves it.

**Two k-thresholds** — both exist, measuring different things:
- *Rating display* — reuse `SurveyConfig.min_survey_threshold` (default 5, admin-editable,
  `surveys/ratings.py:68`). Same mechanism and semantics as Polium; no new concept.
- *k-anonymity on published aggregates* — 10 purchases, higher for sensitive categories
  (health, personal care). A privacy guarantee, not a statistical-meaningfulness one, so it
  stays separate.

**Points for rating** — a bonus on top of purchase points, **never a prerequisite**. Gating
purchase points on rating would withhold points the player has already earned, and it
interacts badly with the prompt budget: we would be gating rewards on an action we deliberately
rate-limit.

**Unverified ratings** — allowed, weighted lower, and **never sufficient alone** to clear the
display threshold. Receipt-anchored ratings are what make the data defensible when manufacturers
dispute it; excluding unverified ratings entirely would cut off non-player contribution and a
growth path.

---

## Open questions

Both remaining questions are calibration, not design. Neither can be answered before real data
exists, and neither should be a constant in code — follow the existing
`SurveyConfig.min_survey_threshold` pattern and make them admin-editable config so they can be
tuned against live behaviour without a deploy. The open question then reduces to picking a
starting value.

- **Prompt budget** — starting value 3–5 per receipt. Calibrate against prompt *completion*
  rate, not prompt count.
- **Tier 0/1 thresholds** — the strong / weak / noise-floor bands need a labelled fixture set
  built from real receipt strings before they mean anything. Build the fixture set in Phase 3;
  treat the initial bands as placeholders.

---

## Todo steps

### Phase 1 — Product identity layer

- [x] Create `Manufacturer`, `ProductCategory`, `Product`, `ProductUpc`, `ProductAlias` models
- [x] `Product.canonical_name` excludes size; document the format on the model
- [x] Add `SqidMixin` to `Product` and register salts in `settings.SQID_SALTS`
- [x] Unique constraint on `ProductAlias(store, raw_text_normalised)`; index the raw column
- [x] Write `normalise_raw_text()` — case, whitespace, punctuation, common receipt noise
- [x] Migrations
- [x] Django admin for all five models, including the merge action
- [x] Model-level tests for alias status transitions and `merged_into` resolution

### Phase 1b — Catalogue seeding

- [x] Management command importing Open Food Facts, filtered to Canada-available products in
  grocery/pharmacy/household categories
- [x] Import as `status=unverified`, `confidence_source=upc_lookup`; populate `ProductUpc`
- [x] Strip size tokens from imported names to match the canonical format
- [x] Do not write alias rows
- [x] Idempotent re-run; report counts created/updated/skipped

*Reads a downloaded JSONL dump rather than the API: Open Food Facts asks that bulk work
go through their published exports, and their search endpoint returned 503 when probed.
Get the dump from https://world.openfoodfacts.org/data.*

### Phase 2 — Store and purchase models

- [x] Create `Store` (brand-level, sqid, lifecycle) and admin
- [x] Create `Purchase` (nullable player FK, `image_phash`, `anonymised_at`, `purchase_token`)
- [x] Create `PurchaseLineItem` with match state fields
- [x] Migrations and admin
- [x] `anonymise_purchase` task — nulls player FK, sets token, writes Layer 2 row at day 30
- [x] Schedule the anonymisation task at write time via the existing `@task` / `enqueue` system
- [x] Tests: anonymisation severs the player link and preserves line-item content

### Phase 3 — Matching cascade, Tiers 0 and 1

- [x] `spendium/matching.py` — `match_line_item(raw_text, interpreted_name, store)`
- [x] Tier 0: exact alias lookup, retailer-scoped then global
- [x] Add `rapidfuzz` dependency
- [x] FTS5 virtual table over canonical names and alias pool
  *(standalone rather than external-content: the searchable text is derived — canonical
  name plus every alias — not a column-for-column mirror. Kept in step by signals, with
  `rebuild_product_index` for bulk imports, which bypass signals.)*
- [x] Tier 1 Stage A: BM25 narrowing to ~200 candidates
- [x] Tier 1 Stage B: rapidfuzz scoring of the shortlist; strong/weak/noise bands
- [x] Batch line items per receipt
  *(no `cdist`: it requires numpy, and the shortlist is already capped, so a full receipt
  is ~6k comparisons at about 12ms. Measured before deciding. `process.extract` does the
  same work in C without the dependency.)*
- [ ] ~~Brand-token pre-filter against `Manufacturer`~~ — **built, then removed.**
  It compared only the query's first token against the whole manufacturer name, so it fired
  for single-word brands like Heinz and never for President's Choice or Kirkland Signature —
  absent precisely where store-brand matching is hardest, and biased toward the easy cases
  BM25 already handles. Revisit only with real data showing it is needed.
- [x] Move thresholds and prompt budget into admin-editable config, per
  `SurveyConfig.min_survey_threshold`
- [x] Build a labelled fixture set of real receipt strings for threshold tuning
- [x] Verify FTS5 index survives a Litestream restore
- [x] Tests running entirely offline, no API calls

### Phase 4 — Gemini extraction

- [x] Add Vertex AI dependency and GCP config via `python-decouple`
- [x] `spendium/extraction.py` — single multimodal call, pure extraction schema
  (no `catalogue_id`, no `match_type`)
- [x] Post-extraction arithmetic checks (line sum vs subtotal, subtotal + tax vs total,
  plausible datetime, negative line flags)
- [x] Retry at high resolution when `image_quality` is degraded/poor and arithmetic fails
- [x] **Delete the image within 24 hours** — task scheduled at processing time; retain phash only
- [x] Tests against recorded fixture responses, no live calls in CI

### Phase 5 — Tier 2 targeted adjudication

- [x] `adjudicate_residuals(line_items, candidates)` — second text-only Gemini call
- [x] Batch only Tier-0/1 misses; attach top-K candidates per item
- [x] Schema allows explicit "none of these"
- [x] Confident adjudications write **provisional** aliases only
- [x] Track adjudication accuracy against later player confirmations
- [x] Tests against fixture responses

### Phase 6 — Player disambiguation

- [x] Disambiguation prompt UI on the purchase view (Datastar SSE partial, per existing patterns)
- [x] Implement the prompt budget (3–5 per receipt)
- [x] Implement global prioritisation ranking
- [x] Three prompt states: weak match, no match, free-text entry
- [x] Free text runs through the same matching cascade — never writes the catalogue directly
- [x] Suppress candidates below the noise floor
- [x] Tests for budget enforcement and ranking order

### Phase 6b — Receipt capture and history

Nothing in the original plan let a player create a receipt or see one. The
service layer can record a receipt and the purchase page can show one, but the
only caller is a test, and a purchase is reachable only by typing its URL.

This is also a compliance gap, not just a UX one. The published privacy policy
commits four times over to a player being able to see and delete their purchase
history "via account settings" (`templates/spendium/privacy.html`, lines 17,
115, 138, 150) and to receiving a copy of it on request (line 148). None of that
is possible without these views.

`record_receipt()` currently runs extraction inline, so it makes one or two
model calls before returning. That cannot sit in a request cycle: a slow receipt
would hold the connection open for seconds and time out. Processing moves behind
the task system, which means the purchase needs a processing state and the page
needs to reflect it.

- [x] Add a processing state to `Purchase` (pending / processed / failed) with
  the extraction problems recorded against it
- [x] Move `record_receipt()` behind a task; the view stores the image, hashes
  it, creates the pending purchase and enqueues the work
- [x] Upload view: image only, size and content-type validated before storage
- [x] Reject a re-upload whose perceptual hash matches a recent purchase by the
  same player, rather than paying to extract the same receipt twice
- [x] Purchase page shows processing state, and the extraction problems when a
  receipt could not be read reliably
- [x] Purchase list — the player's receipts, newest first, with store, date,
  total, and how many line items are still unresolved
- [x] Delete a single purchase, and delete all purchase history, per the
  published policy. Deleting removes the player-linked rows; anonymous rows
  already written stay, which is what the policy describes
- [x] Export purchase history, to satisfy the access commitment
- [x] Gate receipt scanning to members
- [x] Tests: upload validation, duplicate rejection, async processing states,
  deletion removes the player-linked record and leaves the anonymous one

### Phase 7 — Alias integrity

- [x] Confirmation flow: provisional → authoritative on second independent confirmation
- [x] Contradiction demotes to provisional and reopens for prompting
- [x] Repeated contradiction flags for the admin merge queue
- [x] Auto-cluster new unverified records against existing unverified records
- [x] Auto-merge above the high-similarity bar when both sides are unverified
- [x] `merge_group_ids()` walks the merge chain in both directions
  *(the mechanism only; wiring it into rating aggregation belongs to Phase 9, which is where
  ratings first exist. Phase 9 must use it rather than querying a single product id.)*
- [x] Tests: poisoning scenario, merge rollup preserves ratings

### Phase 8 — Retro-matching

- [x] Scheduled task re-running Tiers 0/1 over unresolved line items
- [x] Guard: never overwrite player-confirmed identity, never alter settled points
- [x] Tests: catalogue growth resolves historical backlog

### Phase 9 — Ratings

- [x] Register `Product` as a survey subject via the existing generic engine
- [x] Confirm `compute_rating` works unchanged for products
- [x] Founder-set opening criteria, flagged temporary in the UI
- [x] Criteria versioning so responses under different question sets stay distinguishable
- [x] Product rating display, response count, trend
  *(trend needed a `ProductRatingSnapshot` model and a daily task. The source plan called
  snapshots an "established pattern from Polium", but Polium has only a single
  `pre_election_rating_snapshot` field, not a time series — there was nothing to reuse.)*
- [x] Gate display on `SurveyConfig.min_survey_threshold`; separate k=10 gate on published
  aggregates, higher for sensitive categories
- [x] Ratings keep product reference only — no receipt FK
- [x] Award rating points as a bonus — never gate purchase points on rating
  *(the existing survey engine already awards them; the coupling this forbids is simply
  never built. Purchase points themselves were missing from the plan entirely — see
  Phase 9b.)*
- [x] Weight unverified (non-receipt-anchored) ratings lower; never let them alone clear the
  display threshold

### Phase 9b — Purchase points

The plan refers to purchase points twice — rating points are "a bonus on top of
purchase points", and nothing may gate them — but no phase ever builds them. A
player currently uploads a receipt, has it read, disambiguates it, and earns
nothing. Spendium's whole loop is rewarding ethical spending, so this is the
reward.

Deliberately after ratings, because the amount depends on a product's rating and
a store's, and both need to exist first.

- [x] Points formula from the design: dollars × store rating, with a reduced
  multiplier for self-reported and online purchases
  *(nothing surveys stores yet, so an unrated store earns at a configurable mid-scale
  baseline. Zero would mean shopping anywhere new earned nothing; one would make the
  rating irrelevant when it arrives.)*
- [x] Award once per purchase, at processing time, via the existing ledger
- [x] Negative line items earn nothing — already identified by
  `negative_line_total_ids`, never subtracted
- [x] Points survive anonymisation: the ledger entry keeps store, date and
  amount, and loses its reference to the basket
- [x] Never re-award on retro-matching or reprocessing
- [x] Show points earned on the purchase page and in the receipt list
- [x] Tests: awarded once, unaffected by later matching, never gated on rating

### Phase 10 — Action Centre

- [x] Aggregate page: hot products, unresolved disambiguations, unrated products
- [x] Navbar badge for genuinely new items, cleared on visit
- [x] Email rules: onboarding sequence, then high-priority triggers only, max one per week

*Hot thresholds were the plan's open question #1 and remain uncalibrated, so they are
config rather than constants: trending volume, rating movement, and how long a computed
flag lasts. Admin flags are exempt from the nightly recompute — a recall is exactly what
no volume metric will have noticed yet.*

### Phase 11 — Metrics and abuse controls

- [x] Instrument alias hit rate, tier distribution, prompt and completion rates
- [x] Instrument new-record, auto-merge, and demotion rates
- [x] Submission velocity check
- [x] High-value receipt hold
- [x] Negative line item suppression *(already built with the points formula in Phase 9b —
  excluded from earning rather than subtracted, so a refund never eats into points earned
  on the rest of the shop.)*

(Perceptual-hash deduplication and the members-only gate move to Phase 6b, where
the upload path they guard is built.)

*Both holds withhold points, never the receipt. The data is worth having regardless of
whether the person supplying it turns out to be honest, and a false positive that delays a
payout is recoverable where one that discards somebody's weekly shop is not. No retroactive
clawback: points are settled once.*

*Metrics are snapshotted daily, platform-wide and per store, because the claim they exist to
test is that the system improves without curation — and a rate computed once says nothing
about whether it is moving. Per store because each chain's receipt strings are learned
separately, so an overall average hides a chain that is not converging at all.*

*Deliberately not built: percentile-based risk tiers and the isolation-forest model from the
superseded draft. Their thresholds can only come from real player behaviour, and guessing
them now would produce controls calibrated to an imagined population. The draft's own advice
was 4–6 weeks of observation first.*

### Phase 12 — Documentation

- [x] Update `spendium-purchase-data-architecture.md` — 30-day window, OLAP write at
  anonymisation time
- [x] Update `local_only/todo.md` — product ratings no longer Stage 5 deferred
- [x] Update the app table in `CLAUDE.local.md`, which still described Spendium as a waitlist
- [x] Confirm the privacy policy still matches actual behaviour before launch

#### Privacy policy audit — two mismatches, both corrected

Found by comparing each claim against the code rather than against the previous version of the
policy. Wording approved and applied to both `templates/spendium/privacy.html` and its source
`local_only/Spendium-Privacy-Policy.md`, which are kept in step by hand.

**1. Purchase record retention.** `privacy.html:138` says purchase records are *"retained while
your account is active"*. They are not: the player link is destroyed at thirty days and the row
deleted. Deleting sooner than promised breaches nothing, but the policy misdescribes the system
and — oddly — claims **weaker** protection than is actually delivered. A player reading it would
expect to find last spring's shopping in their history, and it will not be there.

Applied wording: *"Purchase records — retained for 30 days, then permanently anonymised so
they can no longer be linked to you. You can delete your purchase history at any time before
then via account settings. All purchase records are deleted when you delete your account."*

**2. Survey response deletion.** `privacy.html:140` says *"Individual responses are deleted when
you delete your account"*. `SurveyResponse.player` is `on_delete=SET_NULL`, so the response
survives with its player link nulled. That is anonymisation, not deletion — defensible, and
arguably better for rating integrity, but not what the policy says.

Applied wording: *"Survey responses — retained indefinitely. When you delete your account,
individual responses are anonymised so they can no longer be linked to you; the ratings they
contribute to are not retroactively altered."*

Everything else checks out: receipt images are deleted well inside the promised 24 hours,
`Purchase.player` cascades on account deletion, purchase history is visible and deletable, and
export returns the full history including raw receipt text.

### Phase 13 — Review and hardening

A deliberate pass over everything built, before any of it meets a real player.
Twelve phases produced roughly forty modules and fifteen migrations in one app,
written in sequence and largely reviewed only against the phase in front of it.
This phase looked at the result as a whole, which nothing before it had.

Three real bugs, one unverified guard, one latent N+1, and two tables that grew
without limit. All fixed. The security section was deferred at the time on the
grounds that nothing is in production yet, then done here.

#### Security, authorisation and privacy — done

The durable artifact is `spendium/test_security.py`, which encodes the audit
rather than recording that one happened: a new route with no declared audience
now fails a test.

- [x] Every view enumerated with its intended audience and checked against its
  decorators. All correctly gated; `product_detail` is the only deliberately
  public one.
- [x] Every purchase-scoped lookup filters on `player=request.user`. Both
  helpers did. Coverage did not — three cross-player tests existed for ten
  player-scoped endpoints, so the rest are covered now.
- [x] Privacy policy audited against behaviour (Phase 12): two mismatches found
  and corrected.
- [x] What survives anonymisation checked from every direction.
- [x] Task endpoints reject unauthenticated calls in production. The OIDC guard
  is skipped under DEBUG and had never run; now tested with DEBUG false, for a
  missing and a bogus token.
- [x] Admin reviewed. `AnonymisedPurchase` is read-only and unlinkable;
  `Purchase` shows the player, which is the point of a review queue.
- [x] `@csrf_exempt` **removed** from the waitlist view. It was covering for a
  form that sent no token, not for anything the endpoint needed.

**Bug: `snapshot-metrics` was registered but never routed.** Terraform scheduled
a nightly POST to a URL that did not exist, so metrics would never have been
recorded and nothing would have failed loudly. A test now asserts every
registered task is reachable.

#### Test quality — done

Checked by mutation rather than by reading: break a behaviour, confirm a test
fails, restore. Eight critical behaviours mutated; **seven caught**.

The miss was `retro._should_skip`, which guards the strongest guarantee in that
module and was entirely unverified — the candidate querysets already exclude the
same rows, so the tests passed on the queryset rather than the guard.

The first fix *also* failed the mutation: the new test used a line that was both
PLAYER-tier and resolved, so it passed on the second condition and left the
first branch untested. Isolating them needed an anonymised line, which has no
`disambiguation_state` at all. Both branches are now independently covered.

That sequence is the argument for mutation testing in one example — a test
written specifically to close a gap, which looked right and did not close it.

#### Query cost — done

Profiled by growing the data tenfold and counting queries.

- [x] Action Centre **flat** (16 → 13); receipt list **flat** at 4. No N+1 in
  anything a player hits.
- [x] Removed a duplicated `SurveyConfig.get()` in `ratings.compute` — invisible
  for one product, linear for anything looping over many.

**Found:** `ratings.manufacturer_rating` is linear — 27 queries for 3 products,
123 for 15. Called only from tests, so latent rather than live, and now
documented as unfit for a view until the per-product work is batched.

#### Operational correctness — done

- [x] All migrations apply cleanly from an empty database.
- [x] Every `config()` without a default is present in Secret Manager — all
  eight.
- [x] Task idempotency reviewed. All are safe to retry; `anonymise_purchase`,
  `process_receipt` and `delete_receipt_image` return early on a repeat, the
  snapshot tasks use `update_or_create`, and the sweepers delegate to idempotent
  work.

**Fixed:** `send-action-centre-emails` recorded the send *after* making it, so a
crash between the two would re-email whoever was in flight on retry. Now records
first — a failure costs that player their email this week instead of sending it
twice, which is the safer way round for mail nobody asked for.

**Known limitation:** `enqueue(schedule_time=)` is a no-op under DEBUG, so
anything scheduled rather than immediate never runs locally. Anonymisation is
the main one; exercise it via the sweeper or by calling the service directly.

#### Cost and growth — done

- [x] The kill switch is verified end to end: with
  `MatchConfig.adjudication_candidates = 0`, no second model call is made on a
  receipt that would otherwise adjudicate. It covers Tier 2 only, which is why
  Phase 14 exists.
- [x] Retention added for both snapshot tables, which gained a row per subject
  per day forever. Pruned by the same task that writes them, so retention cannot
  drift from the writer.
- [x] Points overflow checked: 20 criteria at the 999.99 ceiling on a $200 shop
  is ~4M points against a 100M field limit, and ~48 years of weekly maximum
  shops before `total_points` overflows. Comfortable.

#### Simplicity — partly done, remainder deliberately deferred

- [x] Three helpers written for callers that never arrived, deleted.
- [x] `Purchase.verification_method` — **kept.** Four choices, one reachable,
  but it records a fact about how a purchase was evidenced rather than encoding
  a guess, and the QR and self-report paths are in the design. Revisit if they
  are abandoned.
- [x] `flag_count` returning 0 on `Store` and `Manufacturer` — **kept.** The
  abstract property raises otherwise, and nothing calls `should_deprecate` for
  them. Inert and documented.
- [ ] Reassess module boundaries. Deferred: `service`, `points`, `ratings`,
  `catalogue`, `action_centre`, `matching`, `retro` and `abuse` each have a
  defensible remit today, and reshuffling them without a concrete problem to
  solve would churn every import for no gain.
- [ ] The inline styles repeated across Spendium templates. Deferred: a real
  cost, but it wants a stylesheet decision for the whole site rather than a
  Spendium-only fix, and Polium has the same duplication.

### Phase 14 — A kill switch someone can actually find

Requested 2026-07-26. To be built after Phase 13.

The test is a new hire, on their first day, who has just been told the bill is
running away and is the only person available. They should be able to stop the
spending without reading the codebase, asking anyone, or knowing what a "tier"
is.

Today the control is `MatchConfig.adjudication_candidates = 0`, which fails that
test on every count: it is one unlabelled integer among eight in a form called
"Matching configuration", its help text explains *what* it does rather than
*when you would want it*, and nothing indicates it is the emergency lever.

**It is also the wrong lever.** Adjudication only runs on the residual that
Tiers 0 and 1 miss — a minority of lines on a minority of receipts. Extraction
runs on **every uploaded receipt**, so it is almost certainly the larger bill
and the more likely thing to be running away. Setting
`adjudication_candidates = 0` while extraction keeps going would look like
pulling the switch and watching the meter keep spinning, which is worse than
having no switch at all.

- [ ] A single obvious control that stops **all** model spending: extraction and
  adjudication together, not just Tier 2.
- [ ] Reachable from the admin index without knowing which model holds it.
  Named for the situation, not the mechanism — the person looking for it is
  searching for "stop", not for "MatchConfig".
- [ ] States plainly what happens while it is on: uploads are still accepted and
  still queued, receipts are simply not read until it is switched off. Nothing
  is lost, no player data is discarded, and no points are wrongly awarded
  because unprocessed receipts never reach the points step.
- [ ] Equally obvious to switch back off, and says who turned it on and when —
  the panicked hire should not also be the person who has to explain it later.
- [ ] Queued receipts resume when it is cleared, rather than needing a manual
  re-run.
- [ ] Tests: with it on, no model client is constructed by any path — upload,
  reprocessing, or retro-matching.
- [ ] Consider a second, narrower control for adjudication alone, since that is
  the one whose cost scales with catalogue immaturity rather than with usage.
