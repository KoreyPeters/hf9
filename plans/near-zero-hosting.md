# Getting the bill to approximately zero

The trap, stated as Korey put it: Cloud Run is cheap when CPU is throttled
between requests, but SQLite-on-container needs a background process that
throttling starves. Cloud SQL costs what it saves. A long-lived container saves
nothing.

There is a way out, and the numbers say it is worth more than it looks.

---

## 1. The number that decides this

Measured from three days of production request logs:

```
requests sampled:                              918
total request-seconds over 3 days:           100.1
implied vCPU-seconds per month (1 vCPU):     1,001
implied GiB-seconds per month (1 GiB):       1,001
longest single request:                       30.9s
```

Cloud Run's monthly free tier is **180,000 vCPU-seconds, 360,000 GiB-seconds and
2 million requests** (per billing account — worth confirming against your own
account, but the order of magnitude is not in doubt).

So with CPU billed only during requests, this application uses about **0.6% of
the free allowance**. Multiply the traffic by fifty and it still fits.

**The entire ~$60/month is idle CPU.** Not traffic, not storage, not Gemini —
just a container being paid for while it sits doing nothing, because
`cpu_idle = false` bills for the instance's whole lifetime.

Which means the target is not "cheaper". The target is **zero**, and the only
thing standing in the way is Litestream needing CPU between requests.

## 2. The cheapest experiment, before building anything

`terraform/cloud_run.tf` says `cpu_idle = false` exists because "Litestream
replicates from a background process and flushes a final time on shutdown.
Throttling the CPU between requests starves both."

That is the right concern. It may also be untrue in the way that matters.

CPU throttling does not freeze the container during *requests* — and every write
this application makes happens inside one. Litestream would replicate in bursts
whenever traffic arrives. The only exposure is the last write before the
instance goes idle: nothing wakes Litestream to ship it, and the instance is
eventually killed.

**Unless the shutdown grace period allocates CPU**, which Cloud Run does provide
for throttled instances. The same terraform comment says graceful shutdown is
*already verified working* — Litestream signals uvicorn, waits, then syncs. If
that still holds with throttling on, the entire problem is a one-line change.

So before designing anything:

1. Set `cpu_idle = true` in a revision.
2. Make a write, let the instance go idle, force a cold start, confirm the write
   survived.
3. Check the GCS generation timestamps against the write times.

If it holds, stop here. **The bill goes to zero for the price of one line.**

## 3. If it does not hold: request-driven durability

Litestream's daemon needs continuous CPU. Durability does not — it needs writes
to reach GCS, and writes only ever happen while CPU is allocated.

- **Cold start** — `litestream restore` already runs in `start.sh`, and startup
  CPU is always allocated even on throttled instances. Unchanged.
- **After a consequential write** — take a consistent snapshot with SQLite's
  backup API (`sqlite3.Connection.backup`, not a file copy, which would tear
  under a concurrent writer) and upload it to GCS.
- **Debounce** — a dirty flag plus a minimum interval, so a receipt that writes
  several times in one task uploads once.

The writes that matter are few and already identifiable: signup, receipt
processed, survey submitted, points awarded. Adding ~300ms to each is invisible
next to a 15-second receipt read, and reads pay nothing.

**This has a defined expiry, and that is the point.** Whole-file upload is fine
at a few megabytes and absurd at a few hundred — Litestream exists precisely
because incremental WAL shipping scales and file copying does not. This is the
right answer *while building and growing*, and it stops being right at a size
that can be stated in advance: when the database gets big enough that the upload
is noticeable, move to option 4. Writing that threshold down now is what stops
it becoming a surprise.

## 4. When SQLite itself becomes the constraint: serverless Postgres

Not now. But worth knowing the shape, because SQLite has already cost real time:
the `database is locked` investigation, `transaction_mode = IMMEDIATE`, the busy
timeout, the single-worker pin, and a database that lives in RAM (debt items 5
and 6).

Serverless Postgres — Neon and similar — has a free tier with scale-to-zero
compute and enough storage for years at this rate. It removes Litestream, tmpfs,
the single-writer contention and the RAM ceiling in one move, and Django's
Postgres support is first-class.

**The cost is FTS5.** `spendium/search.py` (106 lines) narrows the product
catalogue with an FTS5 `MATCH`, kept in step by `spendium/signals.py` and
rebuildable by one management command. On Postgres that becomes `pg_trgm` or
`tsvector` — call it 200 lines including tests, plus a data migration and a
dependency outside GCP.

Contained, not trivial. Worth doing when SQLite's limits are costing more than
that, and not before.

## 5. What does not help

- **Cloud SQL.** Correctly ruled out. The cheapest instance is roughly what the
  idle CPU costs today, so it moves the money rather than saving it.
- **GKE.** A cluster to run one pod pinned to one replica.
- **A long-lived VM.** ~$16/month against a Cloud Run bill that could be zero.
  The VM's advantages are real but they are about debt items 5 and 6, not price
  — see `plans/single-vm-vs-cloud-run.md`.
- **Memorystore.** Three times the cost of the machine it would serve.

The pattern in all four: they are ways of paying for a computer that is running
all the time. The insight this application can exploit is that **it does not need
one** — it needs about a thousand seconds of CPU a month, and that is free.

---

## Result: the experiment passed, 2026-08-08

`cpu_idle = true` is live and Litestream replicates fine under throttling.

| Evidence | What it shows |
|---|---|
| Store created 02:21; WAL segments in GCS at 02:21:02, :03, :04, :09, :13, :26 | Replication within **seconds**, throttled |
| 17 cold starts over the next 14 hours, store still present | Survived scale-to-zero end to end |
| Snapshot size 97,039B → 97,181B on the very next generation, → 97,571B since | Writes reached the *replica*, not just RAM — a generation's snapshot is taken from a database restored entirely from GCS |

**Why the original concern did not hold.** Throttling removes CPU *between*
requests, and this application only ever writes *during* one — every write path
is a view or a task endpoint. Litestream gets CPU exactly when there is
something to replicate. The comment in `cloud_run.tf` has been rewritten, since
it asserted the opposite.

**The tripwire, recorded in the terraform comment as well.** This reasoning fails
the moment anything writes outside a request — a background thread, an in-process
scheduler, a queue consumer. Add one and `cpu_idle` must go back to `false`.

**Unchanged:** a hard kill with no SIGTERM still loses whatever has not shipped.
Debt item 6, true before this change too.

**Sections 3 and 4 are therefore not needed.** No request-driven backup, no
Postgres migration, no VM. Left in this document because the reasoning is worth
keeping if the tripwire above is ever hit.

## Todo

- [x] Set `cpu_idle = true`, deploy, and confirm a write survives an idle
      scale-to-zero and a cold start
- [x] Check GCS object generation timestamps against write times, so "it
      survived" is evidence rather than luck
- [x] Rewrite the `cloud_run.tf` comment, which claimed throttling starves
      Litestream
- [ ] **Korey:** delete the `zzz-coldstart-test` store
- [ ] Watch a week of billing. Expect approximately zero.
- [ ] Litestream generations are never pruned — 30+ have accumulated, one per
      container start, and the container now restarts hourly. 2MiB today and
      growing faster than before. Worth a retention policy in `litestream.yml`
      or a line in the debt register.

**Not needed unless the tripwire is hit**

- [ ] Add a `backup-database` task: `Connection.backup()` to a temp file, upload
      to GCS, debounced by a dirty flag and a minimum interval.
- [ ] Call it after the consequential writes — signup, receipt processed, survey
      submitted.
- [ ] Test that a snapshot taken during a concurrent write restores cleanly. The
      backup API is what makes this true and a file copy would not; the test is
      what proves I used it.
- [ ] Record the database size at which this stops being sensible, and add it to
      `plans/operational-debt.md` with the number in it.

**Regardless**

- [ ] Confirm the Cloud Monitoring 5xx alert fires (debt item 13). Scaling to
      zero properly means longer gaps where nothing is watching, so the alerting
      path matters more, not less.

**Decisions for Korey**

- [ ] Is a small window of possible write loss acceptable while building? The
      answer changes whether §3 needs to be synchronous or can be a debounced
      task.
- [ ] Confirm the Cloud Run free-tier figures against your own billing account
      before relying on them.
