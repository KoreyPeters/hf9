# Spendium — Purchase Data Architecture

**Status:** the two-layer design here is what was built. Two numbers changed during
implementation and are corrected below — the retention window (seven days → thirty)
and when the anonymous row is written (processing time → anonymisation time). See
`spendium-product-identity-and-ratings.md` for the reasoning.

## Principles

- Player purchase data is sensitive. The architecture should minimise breach exposure and make re-identification technically infeasible, not merely policy-prohibited.
- Fraud detection and aggregate analytics are legitimate needs. The architecture must serve both without compromising the above.

---

## Two-Layer Design

### Layer 1 — Transactional Database (player-linked, time-limited)

The full purchase record — store, products, amounts, timestamps — is written to the transactional database linked to the player's account. This record exists for **thirty days only**. A Cloud Tasks job is scheduled at write time to anonymise it at exactly day thirty, with a daily sweeper as backstop.

**Changed from seven days.** This window was originally scoped to fraud detection and dispute resolution alone. Rating and disambiguation were added as requirements afterwards, and both need the player to be able to act on their own purchase — which seven days does not allow. Thirty days also sits well inside what the published privacy policy already commits to.

After thirty days, no identified purchase data remains anywhere in the system. The row is not merely stripped of its player: it is deleted outright, because the points ledger holds a `GenericForeignKey` whose `object_id` is a plain integer with no constraint, so a surviving row could be joined back to the player who earned points from it.

The points ledger entry — date, points awarded, multiplier applied — is retained permanently. Its human-readable description naming the store is **cleared at anonymisation**: while the purchase existed the description merely repeated what the purchase row already held, but kept afterwards it would be the only surviving record of where and when somebody shopped, and years of those amount to a movement trace more revealing than the basket this step exists to destroy.

The points ledger entry — store name, date, points awarded, multiplier applied — is retained permanently. It contains no product detail and is sufficient for the player to understand their game history.

### Layer 2 — OLAP Warehouse (anonymised, permanent)

At the moment of **anonymisation**, the full receipt is written to the OLAP warehouse. Player identifiers are never carried across — the anonymous row is constructed in code from the fields that survive, so identified data never exists in the OLAP environment.

**Changed from processing time.** An anonymous row created up front, under a token with no route back, could never absorb the corrections a player makes during their window. The analytical layer would permanently record product identities the player had already fixed. Writing at day thirty means the row carries the settled identity, and costs nothing — nothing consumes the analytical layer in real time.

All line items from a single purchase are attributed to a **single randomly-generated UUID token**, created fresh at anonymisation. This token is never stored in the transactional database and is never derived from any player identifier. It has no mathematical relationship to the player.

The token exists solely to preserve basket-level analysis — understanding which products are bought together — which is analytically valuable for rating engagement and pressure campaigns.

---

## Fraud Detection

Thirty days is more than sufficient for the abuse patterns relevant to Spendium:

- **Velocity abuse** — detectable within hours
- **Duplicate store gaming** — detectable within days
- **Round-number padding on self-reports** — detectable on first occurrence

The one gap this window does not cover is **coordinated multi-account rating inflation**. This pattern is detected at the store and product level in the OLAP layer, where the signal is the aggregate rating movement, not the individual player record. The OLAP layer is the correct place for this analysis.

---

## K-Anonymity on Published Ratings

Product and store ratings derived from OLAP data are not published until a minimum threshold of **k purchases** exists behind them. This prevents reverse-engineering of individual baskets from sparse aggregate data. The threshold for k is a product decision, with a suggested minimum of 10 and higher thresholds for sensitive categories (health, personal care).

---

## Summary

| | Transactional DB | OLAP Warehouse |
|---|---|---|
| **Player-linked** | Yes | No |
| **Retention** | 30 days | Permanent |
| **Granularity** | Full receipt | Full receipt |
| **Purchase token** | None | Random UUID, written at anonymisation |
| **Purpose** | Points, fraud, disputes | Ratings, analytics, campaigns |
