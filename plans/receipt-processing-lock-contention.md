# Get the network calls out of the write transaction

`process_receipt` holds SQLite's single write lock across a call to Gemini. Any
other writer waits, times out, and surfaces as `OperationalError: database is
locked`. Observed in production 2026-07-26 22:12 UTC.

Two mitigations are already applied and are not the fix. This describes the fix.

---

## What actually happens

`service.py:216` decorates the whole of `process_receipt` with `@atomic`. Inside
that one transaction:

| Line | What | Cost |
|---|---|---|
| 254-255 | Read the image from GCS | network |
| 256 | `extraction.extract_receipt` — Gemini | **seconds** |
| 264 | `_resolve_store` → `Store.objects.create` | **first write — takes the lock** |
| 275-299 | Purchase fields, matching, line items | writes |
| 301 → 363 | `_adjudicate_residuals` → `adjudication.adjudicate` — Gemini | **seconds, holding the lock** |
| 306-311 | `abuse.evaluate`, `points.award_for_purchase` | writes |
| — | commit — lock released | |

SQLite permits one writer. The lock is taken at line 264 and held until commit,
and the span between them contains a full LLM round trip. Everything else that
wants to write blocks for that entire time.

The extraction call at line 256 is *before* the first write, so it is not part of
the problem. **The adjudication call is.** That distinction matters: the obvious
fix of "move the Gemini call out of the transaction" targets the wrong call.

### Why now

Three changes this month converged, none wrong alone:

- The OIDC `audience` fix means Cloud Tasks are actually delivered for the first
  time, so `process-receipt` genuinely runs concurrently with other work.
- `sweep-pending-receipts` runs every 15 minutes over *all* pending purchases,
  including one uploaded seconds ago whose task is already in flight.
- `SESSION_SAVE_EVERY_REQUEST = True` means every authenticated request writes a
  session row — including the player refreshing "We're still reading this
  receipt" while the lock is held.

`plans/operational-debt.md` item 7 predicted the session-write pressure and said
it would be the first thing to examine if contention appeared. It appeared. It is
a contributor, not the cause — the cause is the length of the write window.

### Why it is not currently corrupting anything

The transaction that causes the lock is also what makes the failure clean: the
whole attempt rolls back, the purchase stays `pending`, and the Cloud Task retry
or the next sweep reprocesses it. The costs are a 500, a wasted Gemini extraction,
and a receipt that takes longer than it should.

This matters for sequencing: **the `@atomic` must not simply be removed.** Doing
that converts a self-healing failure into partial writes.

## The constraint that rules out the easy fix

Points depend on adjudication. `points.py:20` computes
`product_points = Σ (line total × ppd(product))`, and `_adjudicate_residuals` is
what assigns products to lines Tiers 0 and 1 could not match. So adjudication
cannot simply be moved after `award_for_purchase` — that underpays the player for
exactly the items the model resolved.

The ordering extract → match → adjudicate → award is real. The fix has to keep it
while shortening the write window.

## Approach

Four phases, with transactions only around the writes.

1. **Read** *(no transaction)* — guards, image fetch, `extract_receipt`.
2. **Persist** *(atomic, short)* — store, purchase fields, line items,
   normalisation. Status stays `pending`.
3. **Adjudicate** *(no transaction)* — the Gemini call, returning decisions.
4. **Settle** *(atomic, short)* — apply decisions, `abuse.evaluate`,
   `award_for_purchase`, status to `processed`.

Two short write windows instead of one long one, and no network call inside
either.

### The problem this creates, and the fix for it

A crash between phases 2 and 4 leaves a purchase with line items and a `pending`
status. The sweep re-runs it, phase 2 executes again, and the receipt gets
**duplicate line items**.

So phase 2 must be idempotent: delete existing line items for the purchase before
creating them. That is safe because line items are derived entirely from the
extraction, with one exception — player disambiguation choices. Check whether any
line can carry a player decision before phase 2 completes; if it can, phase 2 must
preserve it, and that is the sharpest edge in this work.

`award_for_purchase` is already idempotent, guarded on `points_awarded`
(`points.py:144`), so phase 4 is safe to repeat.

### Concurrency, separately

Two runners can still both pass the `processing_status != PENDING` guard at
`service.py:225` before either commits, and both do the LLM work. Today the loser
dies on the lock; after this fix both may succeed, wasting a Gemini call each and
racing on the same rows.

The durable answer is a claim: transition `pending → processing` with a
conditional `UPDATE` and proceed only if it affected a row. That needs a new
status, a migration, template changes (`purchase_detail.html` keys on `"pending"`),
and a lease timeout so a crashed run does not strand a receipt in `processing`
forever. It is the correct design and it is a bigger change than the transaction
split — worth doing second, and worth doing.

## Already applied

Neither is the fix; both reduce how often the window is contended.

- **Sweep grace period.** `pending_purchase_ids` now ignores purchases younger
  than `SPENDIUM["SWEEP_GRACE_MINUTES"]` (default 5). The sweep exists for dropped
  tasks and receipts that waited out a stop, both minutes old at least; a fresh
  upload has a live task and needed no second runner. This removes the dominant
  collision.
- **Busy timeout 20s → 60s.** A waiter now outlasts a lock holder rather than
  dying. Treating the symptom deliberately: the window is the bug.

---

## Todo steps

1. Confirm whether a `PurchaseLineItem` can carry player disambiguation state
   before processing completes. This decides whether phase 2 may delete and
   recreate lines, and everything below depends on the answer.
2. Split `process_receipt` into the four phases, `@atomic` on 2 and 4 only.
   Remove the decorator from the function itself.
3. Make phase 2 idempotent per step 1 — delete-and-recreate, or reconcile.
4. Test: a crash between phases leaves no duplicate line items when reprocessed.
   Simulate by raising inside phase 3.
5. Test: the write window contains no network call. Assert the model client is
   never invoked inside an open atomic block — a fixture that fails if
   `connection.in_atomic_block` is true when the fake model is called pins this
   permanently, and is what stops it regressing.
6. Test: points still account for products the adjudicator resolved. This is the
   ordering constraint, and the reason the obvious fix was wrong.
7. Measure the write window before and after — log the span from first write to
   commit — so the improvement is a number rather than an assumption.

**Then, separately**

8. Add `STATUS_PROCESSING` and the conditional-`UPDATE` claim, with a migration.
9. Add a lease timeout so a purchase stuck in `processing` returns to the sweep.
10. Update `purchase_detail.html`, which currently keys on `"pending"` for the
    "still reading" state, and `pending_purchase_ids`.
11. Revisit the 60s busy timeout once the window is short. It is compensation for
    a problem that should no longer exist.
