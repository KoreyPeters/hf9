# Spendium — Purchase Data Architecture

## Principles

- Player purchase data is sensitive. The architecture should minimise breach exposure and make re-identification technically infeasible, not merely policy-prohibited.
- Fraud detection and aggregate analytics are legitimate needs. The architecture must serve both without compromising the above.

---

## Two-Layer Design

### Layer 1 — Transactional Database (player-linked, time-limited)

The full purchase record — store, products, amounts, timestamps — is written to the transactional database linked to the player's account. This record exists for **seven days only**. A Cloud Tasks job is scheduled at write time to delete it at exactly day seven.

This window exists to support fraud detection and points dispute resolution. After seven days, no identified purchase data remains anywhere in the system.

The points ledger entry — store name, date, points awarded, multiplier applied — is retained permanently. It contains no product detail and is sufficient for the player to understand their game history.

### Layer 2 — OLAP Warehouse (anonymised, permanent)

At the moment of processing, the full receipt is written to the OLAP warehouse. Before this write, all player identifiers are stripped in the processing pipeline — not as a database operation, but in code, so identified data never exists in the OLAP environment.

All line items from a single purchase are attributed to a **single randomly-generated UUID token**, created fresh at processing time. This token is never stored in the transactional database and is never derived from any player identifier. It has no mathematical relationship to the player.

The token exists solely to preserve basket-level analysis — understanding which products are bought together — which is analytically valuable for rating engagement and pressure campaigns.

---

## Fraud Detection

Seven days is sufficient for the abuse patterns relevant to Spendium:

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
| **Retention** | 7 days | Permanent |
| **Granularity** | Full receipt | Full receipt |
| **Purchase token** | None | Random UUID, per purchase |
| **Purpose** | Points, fraud, disputes | Ratings, analytics, campaigns |
