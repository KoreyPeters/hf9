# Receipt parsing: image to canonical product

## Overview

When a player photographs a receipt, Spendium needs to do two things that are conceptually distinct: extract the raw text from the image, and interpret that text into canonical product identities that match records in our database. We use Gemini Vision on Vertex AI for both steps in a single call, rather than separating OCR and interpretation. This is the right approach because the interpretation problem — turning "TP-COLG-250" into "Colgate Toothpaste Bright Whitening 250ml" — requires world knowledge that a pure OCR tool cannot provide.

Receipt scanning is a members-only feature. The AI processing cost per receipt is negligible (well under 5% of the annual membership fee even for heavy users), so this is not a financial constraint — it is a deliberate product decision to make membership tangibly valuable.

---

## How Gemini receives the image

The receipt image is passed to Gemini as an inline multimodal part alongside the text prompt. The image bytes are base64-encoded and sent directly in the API request body; no separate Vision API call is required. Gemini processes the image and the prompt together in a single inference pass, which means the extraction and normalisation instructions are applied to the visual content simultaneously rather than in sequence.

For receipt images specifically, we pass the image at medium resolution. High resolution increases token cost and latency with diminishing returns on single-page thermal receipts; low resolution risks losing small-font line items. Medium is the practical default, with the option to retry at high resolution if the confidence score on a first pass is low.

The API call structure is approximately:

```python
from vertexai.generative_models import GenerativeModel, Part, GenerationConfig

model = GenerativeModel("gemini-2.5-flash")

image_part = Part.from_data(
    data=image_bytes,
    mime_type="image/jpeg"
)

response = model.generate_content(
    [image_part, prompt_text],
    generation_config=GenerationConfig(
        response_mime_type="application/json",
        response_schema=RECEIPT_SCHEMA,   # see Schema section below
        temperature=0.1                   # low temperature for deterministic extraction
    )
)
```

Temperature is set low (0.1) because extraction is not a creative task. We want Gemini to be decisive rather than exploratory when reading a receipt.

---

## Prompt design

The prompt has three layers: a system-level instruction that establishes the task, few-shot examples that calibrate the output style, and a per-request context block that includes candidate products from our catalogue for the specific retailer.

### System instruction

The system instruction is static and cached on Vertex AI (we pay to process it once and get a discounted rate on all subsequent calls that reuse it). It establishes:

- The role: a product identification specialist, not a generic receipt transcriber
- The output contract: expand all abbreviations, never return internal SKUs or store codes
- The canonical name format: `[Brand] [Product Name] [Variant] [Size/Weight/Count]`
- The confidence vocabulary: what constitutes a `catalogue_match`, `inferred`, and `uncertain` result
- The instruction to self-assess image quality and report it as a top-level field

Example system instruction:

```
You are a receipt parsing specialist for a product ratings platform. Your job is 
to identify exactly what products a customer purchased, expressed in the form a 
consumer would recognise — not the store's internal codes.

For each line item on the receipt:
1. Expand all abbreviations using your knowledge of retail products.
2. Produce a canonical product name in the format: [Brand] [Product Name] [Variant] [Size].
   Example: "TP-COLG-250" → "Colgate Toothpaste Bright Whitening 250ml"
   Example: "PC BLK LBL COFF 300G" → "PC Black Label Ground Coffee 300g"
   Example: "BNLS CHKN BRST KG" → "Chicken Breast Boneless per kg"
3. If a matching product appears in the provided catalogue context, use that 
   product's exact name and ID. Prefer a catalogue match over your own inference.
4. If no catalogue match exists, infer the canonical name from your world knowledge.
5. If you cannot confidently identify a product, set match_type to "uncertain" and 
   provide your best guess as canonical_name anyway.

Never return PLU codes, UPC barcodes, or store-specific SKUs as the canonical name.
Always return a canonical name even for uncertain items — a best guess is more useful 
than a blank.
```

### Few-shot examples

Below the system instruction, include 6–10 worked examples covering the range of difficulty we expect. These are particularly important for:

- Store-brand abbreviations (e.g. "PC", "LIFE", "KIRKLAND", "PCBIO")
- Weight-priced items (e.g. "BNLS CHKN BRST KG @ $14.99/kg")
- Multi-pack items where the count matters (e.g. "EGGS LG DZ" vs "EGGS LG 18PK")
- Items that look like product codes but aren't (e.g. "750ML RED WINE" is not a SKU)

The examples should represent actual patterns from Canadian grocery and pharmacy receipts, as that is our primary market. These should be refined over time as we observe real failure cases from Polium resolutions.

### Per-request catalogue context

For each receipt, we prepend a context block containing candidate products from our database, filtered to the likely retailer and general category. This is retrieved via the same vector search infrastructure used downstream for matching:

```
## Product catalogue context (use these exact names and IDs where they match)

- prod_a1b2: "Colgate Toothpaste Bright Whitening 250ml"
- prod_c3d4: "Colgate Toothpaste Cavity Protection 100ml"
- prod_e5f6: "Colgate Toothpaste Sensitive Pro-Relief 110ml"
- prod_g7h8: "Crest 3D White Toothpaste 75ml"
[... up to ~30 candidates ...]
```

This catalogue context is not static — it is generated per receipt by querying the product database for the retailer identified from the receipt header, narrowed by category signals from a fast preliminary scan of the line items. Providing it in the prompt means Gemini performs the final matching step itself, in context, rather than requiring a separate vector search round-trip after extraction. When Gemini finds a catalogue match, it returns the `catalogue_id` directly, and that product can be awarded points immediately with no further processing.

---

## Response schema

We use Vertex AI's `response_schema` parameter to enforce structured output. This is applied at the token-sampling level — the model cannot produce output that violates the schema. No output validator is needed for structural conformance, though arithmetic sanity checks should still be performed on the numbers.

```python
RECEIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "store_name": {"type": "string"},
        "store_address": {"type": "string"},
        "transaction_datetime": {
            "type": "string",
            "description": "ISO 8601 format, e.g. 2025-05-30T14:23:00"
        },
        "image_quality": {
            "type": "string",
            "enum": ["good", "degraded", "poor"],
            "description": "good: all text clearly legible; degraded: some text unclear but most items readable; poor: significant portions unreadable"
        },
        "line_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "raw_text":       {"type": "string",  "description": "Verbatim text from receipt"},
                    "canonical_name": {"type": "string",  "description": "Expanded human-readable product name"},
                    "catalogue_id":   {"type": "string",  "description": "Matching product ID from catalogue context, or null"},
                    "match_type":     {"type": "string",  "enum": ["catalogue_match", "inferred", "uncertain"]},
                    "quantity":       {"type": "number"},
                    "unit_price":     {"type": "number"},
                    "line_total":     {"type": "number"}
                },
                "required": ["raw_text", "canonical_name", "match_type", "line_total"]
            }
        },
        "subtotal": {"type": "number"},
        "tax":      {"type": "number"},
        "total":    {"type": "number"}
    },
    "required": ["store_name", "line_items", "total", "image_quality"]
}
```

### Post-extraction arithmetic checks

After receiving the response, perform these checks before routing the result:

- Do line item totals sum to within ±$0.05 of `subtotal`?
- Does `subtotal + tax` equal `total` within ±$0.05?
- Is `transaction_datetime` plausible (not in the future, not more than 30 days old)?
- Are any `line_total` values negative (valid for returns, but flag for review)?

If arithmetic checks fail and `image_quality` is `degraded` or `poor`, retry the call with `media_resolution` set to `high`. If the second pass also fails arithmetic checks, route the entire receipt to a manual review queue rather than Polium — this is a data quality problem, not a product identity problem.

---

## Routing by match type

The three `match_type` values map directly onto the three routing paths in the pipeline:

| match_type | Action |
|---|---|
| `catalogue_match` | Award points immediately. No further processing required. |
| `inferred` | Write `canonical_name` as a proposed new product entry. Auto-approve if similarity search finds a close existing match (cosine similarity > 0.95). Otherwise, route to Polium for confirmation. |
| `uncertain` | Route to Polium. Include `raw_text` and `canonical_name` (as a starting suggestion) in the Polium task payload. |

Items with `image_quality: poor` at the receipt level are flagged regardless of individual item `match_type` — they are more likely to contain errors that would result in wrong points being awarded.

---

## Self-improvement mechanism

The system is designed to require less human input over time. Every confirmed resolution — whether auto-approved or community-voted through Polium — feeds back into the product catalogue in two ways:

**Alias accumulation.** When a Polium vote confirms that "TP-COLG-250" (from Shoppers Drug Mart) resolves to `prod_a1b2` ("Colgate Toothpaste Bright Whitening 250ml"), the string "TP-COLG-250" is stored as an alias on that product record. The next time this exact string appears on a receipt from any retailer, the vector search returns `prod_a1b2` as a high-confidence candidate, and Gemini will match to it from the catalogue context rather than inferring independently. The item never reaches Polium again.

**Embedding enrichment.** When a new `canonical_name` is confirmed (either as a new product or a match to an existing one), a new embedding is generated for that name and stored in the product catalogue's vector index. This improves the semantic similarity search for future receipts containing similar but not identical strings. Over time, the embedding space becomes denser around common products, and the effective matching threshold can be raised — meaning fewer items are routed to Polium.

**Retailer-specific pattern learning.** As alias accumulation grows, the per-request catalogue context becomes more useful. For any given retailer, we will have a growing set of confirmed retailer-specific strings → canonical product mappings. These are stored on the product record and used to bias the candidate retrieval step — a Safeway receipt will surface Safeway-specific aliases as higher-priority candidates than purely semantic matches.

The expected trajectory: at launch, a meaningful fraction of line items from each retailer will be `uncertain` and will go to Polium. Within two to three months of normal usage, common products at common retailers should be resolving as `catalogue_match` with no community involvement. The long tail of unusual or regional products will continue to surface in Polium indefinitely, but volume should fall substantially.

---

## Abuse detection and points controls

### Design philosophy

Hard point caps calibrated to a fixed number are brittle — they're right for today's player population and wrong for every future one. The preferred approach is percentile-based thresholds that adapt automatically as the population grows and evolves, combined with soft controls that introduce friction rather than hard stops wherever possible. A player who hits a verification hold experiences a minor delay. A player who hits a hard cap experiences a confrontation. The former is almost always the right default.

Returns are a deliberate non-concern at this stage. Determined fraudsters will not photograph return receipts, will use partial returns rather than full ones, and the engineering cost of receipt-to-bank-transaction reconciliation exceeds the losses any individual abuser could generate. This decision is recorded here as a deliberate choice rather than an oversight. Negative `line_total` values (which appear on receipts that include both purchases and same-receipt returns) should not receive points, but no retroactive point subtraction is implemented for returns processed separately.

### Signals

The following per-player metrics form the basis of both the rules engine and the statistical model. All should be stored in a player summary document in Firestore, updated on each receipt submission and by the daily BigQuery reconciliation job:

- Points earned in the last 1 / 7 / 30 days
- Receipts submitted in the last 1 / 7 / 30 days
- Average receipt value (rolling 30-day)
- Points per receipt ratio (rolling 30-day), compared to population mean
- Retailer diversity score: count of distinct retailers in last 30 days
- Category concentration ratio: fraction of receipts dominated by a single product category in last 30 days
- Receipt submission time distribution: standard deviation of submission hour (low SD indicates robotic regularity)
- Image similarity flag: whether any recent receipt image is a near-duplicate of a prior submission (see below)

No single signal is sufficient. A high average receipt value may simply mean a player shops at expensive stores. Low retailer diversity may mean they genuinely only shop at one chain. The risk assessment should be based on the combination of signals, not any individual one.

### Risk tiers

Each player is assigned a risk tier, stored on their Firestore record and recomputed by the daily BigQuery job. The tier drives which controls apply at submission time.

| Tier | Description | Controls applied |
|---|---|---|
| 1 — Normal | Within population norms across all signals | Standard processing, points credited immediately |
| 2 — Elevated | Outlier on one or two signals, not combination | Verification hold: points enter pending state for 48 hours before crediting |
| 3 — High | Outlier on three or more signals, or 95th+ percentile on combined score | Verification hold + soft multiplier reduction on receipts above median value + added to human spot-check queue |
| 4 — Critical | 99th+ percentile, or image similarity flag triggered | Hard cap applied + account flagged for manual investigation before further points are credited |

Tier assignment should be conservative at launch and loosened as the model is calibrated against real data. Start with wide thresholds and tighten them once you have enough labelled examples from the human review queue.

### Rules engine (synchronous, per-submission)

The rules engine runs as part of the Cloud Run worker, synchronously, on every receipt submission. It reads the player's current risk tier and applies the following checks before awarding points:

**Receipt image deduplication.** Compute a perceptual hash of the submitted image and compare it against the player's last 90 days of receipt hashes stored in Firestore. A near-duplicate (hash distance below threshold) triggers an immediate Tier 4 flag. This is the cheapest high-value fraud check available and should be implemented first.

**Submission velocity check.** If more than N receipts have been submitted by this player in the last hour, hold all further submissions from that player for human review. N should be calibrated against real behaviour but a reasonable starting point is 5 receipts per hour.

**High-value receipt hold.** Any single receipt with `total` above a configurable threshold (suggested starting point: $500) is automatically placed in pending state regardless of tier, and a human reviewer is notified. Most legitimate high-value receipts (large grocery shops, electronics) will clear this within a working day without player impact.

**Negative line item suppression.** Any line item with a negative `line_total` is excluded from points calculation silently. No points are awarded or subtracted for that item.

### Statistical model (asynchronous, daily)

A daily BigQuery scheduled job recomputes each player's position in the population distribution across all signals and updates their risk tier. This job does not need to be a trained ML model initially — a pure SQL implementation that computes percentile ranks and applies threshold rules is sufficient to launch.

Once the human review queue has generated enough labelled examples (players confirmed as abusive vs legitimate), an isolation forest model should be trained on the signal set using BigQuery ML or Vertex AI. Isolation forests are well-suited to this problem because:

- No labelled fraud cases are needed to train the initial model (unsupervised)
- The model finds outliers by measuring how easily a data point can be isolated from the rest of the population
- It handles the mixed signal problem well — a player who is unusual across multiple dimensions simultaneously scores as a stronger outlier than one who is unusual on only one

Once the model is in production, the daily job runs inference against the current signal snapshot for all active players and writes updated tier assignments back to Firestore. The rules engine reads these tiers on every submission.

### Soft multiplier reduction

For Tier 3 players, rather than blocking earnings, apply a reduced points multiplier to receipts above the population median receipt value. The reduction should be gradual — for example, receipts above the 75th percentile value earn at 0.75× for a Tier 3 player, receipts above the 90th percentile value earn at 0.5×. Receipts below the median earn at full rate regardless of tier.

This is intentionally invisible to the player. They continue to earn points and the platform continues to receive their purchase data, which has legitimate value for HF's manufacturer engagement programme. The reduction bounds the disbursement exposure without creating a confrontational cap experience.

### Power users vs abusers

The anomaly detection infrastructure identifies both abusers and legitimate power users — they look similar in the data until you examine the content of their receipts. A player in the 95th percentile by points who shops at eight different retailers, uploads receipts for diverse product categories, and submits at normal hours is a power user and a valuable contributor. The same statistical profile with low retailer diversity, high category concentration, and clustering around a single high-value category is a different story.

The human review queue for Tier 3 and 4 players should be used to generate labelled examples in both directions. Confirmed power users should be cleared from scrutiny and potentially identified for community engagement (moderator roles, early access features). Confirmed abusers inform model retraining. The same queue serves both purposes.

### Implementation sequence

In order of priority:

1. Receipt image deduplication at submission time — negligible cost, catches lazy abuse immediately
2. Daily BigQuery job computing per-player percentile ranks across all signals — no model, pure SQL, gives visibility before any controls are applied
3. Verification hold for Tier 2+ players — introduce friction without confrontation
4. High-value receipt hold ($500+) — bounds exposure from the highest-value abuse cases
5. Human review queue for Tier 3/4 — generate labelled examples for future model training
6. Isolation forest model in BigQuery ML — once labelled examples exist, automate tier classification
7. Soft multiplier reduction for Tier 3 — last because it requires careful calibration against real data

---

## Open questions

- What confidence threshold should distinguish `inferred` (auto-approve) from `uncertain` (Polium)? This will require empirical calibration against real receipts once we have data. A reasonable starting point is: auto-approve if the vector search similarity for the inferred name against an existing product is above 0.95; send to Polium if below.
- How large should the catalogue context block be? There is a cost/quality tradeoff: more candidates improve match accuracy but increase input token count. Suggested starting point: 30 candidates, revisit at scale.
- Should we store raw receipt strings (the `raw_text` field) permanently, or only aliases that have been confirmed via Polium? Storing unconfirmed strings risks polluting the alias index with noise. Recommendation: store only confirmed mappings.
- What are the right thresholds for risk tier boundaries? These cannot be set meaningfully before real player data exists. The daily BigQuery percentile job should run for at least 4–6 weeks before any tier-based controls are activated, so that thresholds are calibrated against actual behaviour rather than guesses.
- At what disbursement level does the buy-and-return abuse pattern become worth addressing more aggressively? This should be reviewed when HF's per-player disbursements reach a level where the maximum possible points fraud by a single player represents a meaningful absolute sum. At launch this is not the case.
