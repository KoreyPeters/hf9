# "database is locked" on every receipt upload

Reported 2026-07-31 23:35 UTC, from `/tasks/process-receipt/`, once per upload.

Two separate things are wrong, and the first one is embarrassing:

1. **The fix for this was written on 2026-07-26 and never pushed.** Production is
   running code five days older than the working tree.
2. **The diagnosis in `plans/receipt-processing-lock-contention.md` is wrong**
   about the mechanism. Deploying the fix will very likely stop the symptom, but
   for a reason different from the one that plan gives — and the reason matters,
   because the residual it leaves is not addressed anywhere.

---

## 1. Production is not running the fix

`origin/main` is at `fae4873`. The commit containing the transaction split —
`a598bf5`, "Trying to fix database locked issue" — is local-only and has never
been pushed. `cloudbuild.yaml` deploys from the pushed branch, so production is
running `fae4873`, whose `spendium/service.py` is byte-identical to `45baf96`'s.

The traceback proves it rather than merely suggesting it:

```
  File "/app/spendium/task_views.py", line 130, in process_receipt
    service.process_receipt(purchase_id)
  File "/usr/local/lib/python3.14/contextlib.py", line 85, in inner   ← @atomic
    return func(*args, **kwds)
  File "/app/spendium/service.py", line 272, in process_receipt
    purchase.save()
```

Two independent confirmations:

- That `contextlib.py` frame is `ContextDecorator.__call__`. It can only be there
  if `process_receipt` is *decorated* with a context manager. In the working tree
  it is not — `@atomic` moved to `_record_extraction` (`service.py:298`).
- In `45baf96`, `service.py:272` is exactly `purchase.save()`, the first write
  after `_resolve_store`. In the working tree, line 272 is
  `purchase.processing_problems = [str(exc)]`.

**So the first action is to push `a598bf5` and let it deploy.** Nothing below
changes that; the rest is about what remains once it lands.

### Why this is worth a line in the debt register

There is no check anywhere that the running revision matches `origin/main`, and
nothing that would have caught a fix sitting unpushed for five days while the bug
it fixes kept mailing tracebacks. The smoke step in `cloudbuild.yaml:63` proves a
deploy that happened; nothing notices a deploy that never did.

---

## 2. The mechanism is not lock contention

The existing plan says the write lock is *held* across the adjudication call to
Gemini and that other writers wait it out and time out. Under the actual
production topology that cannot be what happened.

**There is only one writer process.**

- `terraform/cloud_run.tf:12-15` — `max_instance_count = 1`.
- `start.sh:27` — `uvicorn --workers 1`, deliberately, for the OOM and
  `LocMemCache` reasons documented there.
- `/data` is a memory-backed `empty_dir`, so there is no second container with
  its own copy.

So the failing task is one thread; the only other writers are other threads in
the same process, doing session rows and page writes measured in milliseconds.
Nothing in that picture holds the write lock for the 60 seconds that
`hf/settings/base.py:100` would have made the task wait.

Two facts the contention story does not explain:

- **The failure is at the first write of the transaction**, not a later one.
  Line 272 is the first statement that writes anything; `_resolve_store` above it
  only reads when the store already exists.
- **The 60-second busy timeout plainly did not apply.** Raising it from 20s to
  60s was supposed to make waiters outlast holders. It changed nothing.

### What is actually happening: SQLITE_BUSY_SNAPSHOT

`DATABASES["default"]["OPTIONS"]` (`hf/settings/base.py:83-101`) sets
`init_command` and `timeout` and does **not** set `transaction_mode`. Django 6.0
supports it — `.venv/.../django/db/backends/sqlite3/base.py:328-331`:

```python
if self.transaction_mode is None:
    self.cursor().execute("BEGIN")
else:
    self.cursor().execute(f"BEGIN {self.transaction_mode}")
```

A bare `BEGIN` is `DEFERRED`: no lock is taken at `BEGIN`. The connection becomes
a *reader* at its first `SELECT`, pinning a WAL snapshot, and only tries to
become a writer later.

In the deployed `process_receipt`, with `@atomic` on the whole function:

| Line | What | Effect |
|---|---|---|
| 216 | `@atomic` → `BEGIN` (deferred) | no lock yet |
| 224 | `Purchase.objects.filter(pk=…).first()` | **read snapshot pinned here** |
| 254-256 | GCS fetch + `extract_receipt` | **seconds pass, snapshot held** |
| 272 | `purchase.save()` — first write | read→write upgrade attempted |

If any other connection has committed between line 224 and line 272, that upgrade
fails with `SQLITE_BUSY_SNAPSHOT`. SQLite reports it with the message
**"database is locked"** — the same string as ordinary contention, which is what
makes it read like contention.

And critically: **the busy handler is not invoked for it.** There is no amount of
waiting that can rescue a stale snapshot; only a rollback and retry can. That is
precisely why 20s → 60s did nothing, and it is the single strongest piece of
evidence for this diagnosis over the old one.

### Why it happens on *every* upload

The other committer is not a coincidence, and it is not the player refreshing.
It is the upload's own redirect:

- `spendium/views.py:298` — a successful upload returns
  `redirect("spendium:purchase_detail", pk=purchase.pk)`.
- `hf/settings/base.py:170` — `SESSION_SAVE_EVERY_REQUEST = True`, so that GET
  rewrites the session row and commits.
- Meanwhile the `process-receipt` task, enqueued moments earlier at
  `service.py:207`, is sitting inside its open transaction waiting on Gemini.

Upload → redirect → session write commits → task's snapshot is stale → first
write fails. Every time, with no second player and no unlucky timing required.
`purchase_detail.html:32` then tells the player to "Refresh in a moment", and
each refresh is another committing write.

This is debt register item 7 arriving — but not in the form it predicted. Item 7
expected session writes to *contend*. They do not contend; they invalidate.

### What deploying `a598bf5` actually does

It shrinks the window between the snapshot read and the first write from *seconds
spanning a Gemini call* to *microseconds*. In `_record_extraction`
(`service.py:298-315`) the read in `_resolve_store` and the write at line 315 are
adjacent.

That is a very large improvement and will almost certainly stop the reports. It
is not a guarantee: a session write landing inside that microsecond window still
produces the same failure, now rare and random instead of certain. Which is a
worse thing to debug, not a better one.

---

## 3. The durable fix

Set the transaction mode:

```python
# hf/settings/base.py, in DATABASES["default"]["OPTIONS"]
"transaction_mode": "IMMEDIATE",
```

`BEGIN IMMEDIATE` takes the write lock at `BEGIN` rather than upgrading later. No
upgrade means no stale-snapshot failure, ever — and because acquiring the lock at
`BEGIN` *is* ordinary contention, the `timeout: 60` busy handler finally applies
to it. Writers queue instead of failing.

**Ordering is not optional.** Applying `IMMEDIATE` to the code currently deployed
would be actively worse: it would take the write lock at line 216 and hold it
across both Gemini calls, converting a fast failure into a genuine 60-second
stall for every other request. The split must land first. Since both are one push
apart, the safe sequence is: push `a598bf5`, confirm it deployed, then make the
settings change.

### The cost, stated plainly

Under `IMMEDIATE`, *every* `atomic` block serialises on the write lock, including
read-only ones that would previously have run concurrently. At one instance, one
worker and current traffic this is not a real cost. It would become one if the
worker count ever rises — which is already load-bearing for other reasons (debt
item 1).

### Then reconsider the 60-second timeout

Item 11 of the old plan says to revisit it once the window is short. Under
`IMMEDIATE` the number finally means something — it is a real queue rather than
dead configuration. 60s is too long for a live web request; something nearer 15s
surfaces a stuck writer instead of pinning a request for a minute.

**Decided: 15s.** Korey set it alongside the `transaction_mode` change. That
also closes item 11 of `plans/receipt-processing-lock-contention.md`.

---

## 4. What I am deliberately not proposing

- **Turning off `SESSION_SAVE_EVERY_REQUEST`.** It would cut write volume
  substantially and it is the trigger here. But it is a product decision, not a
  bug fix: with it off, `SESSION_COOKIE_AGE` runs from login rather than from
  last activity, so an active player is logged out a year after signing up. The
  comment at `base.py:164-168` argues for the current behaviour on purpose.
  Flagging it; not deciding it.
- **Retrying `OperationalError` in `core/tasks.py`.** Cloud Tasks already retries
  five times. Under the old code each retry burned a fresh Gemini extraction,
  which was expensive; after the split, `IMMEDIATE` removes the failure mode a
  retry would be catching. Adding a retry now would mask the next distinct bug.
- **The `STATUS_PROCESSING` claim** (old plan items 8-10). Still correct, still
  worth doing, and *less* urgent under this diagnosis than under the old one —
  the double-runner race it prevents was never what produced these tracebacks.
  Worth noting it also gets more reliable under `IMMEDIATE`, since a conditional
  `UPDATE` claim is exactly the read-then-write shape that `DEFERRED` makes
  fragile.

---

## 5. Verifying it, rather than assuming it

The diagnosis above is read off the traceback, the deployed source and the SQLite
locking rules. It has not been reproduced. Per the working agreement, the guard
has to be watched failing before it counts.

A test can force it deterministically — no timing luck needed, because
`SQLITE_BUSY_SNAPSHOT` depends on ordering, not on duration:

```python
@pytest.mark.django_db(transaction=True)
def test_deferred_transaction_fails_when_another_writer_commits():
    """The reported failure, reproduced without a Gemini call.

    Open a transaction, read (pinning a WAL snapshot), let a second connection
    commit, then write. Under BEGIN DEFERRED the upgrade fails immediately with
    "database is locked" — and the 60s busy timeout does not delay it, which is
    what tells the two mechanisms apart.
    """
    ...
```

Two assertions carry the argument:

- It **fails without** `transaction_mode` and **passes with** `IMMEDIATE`. That
  is the fix demonstrated rather than asserted.
- It fails **fast** — well under the configured 60s. That is what distinguishes
  snapshot invalidation from contention, and it is the fact the old plan's
  diagnosis cannot account for.

### Confirmed, 2026-07-31

`core/test_sqlite_transaction_mode.py`. Against current settings:

```
test_a_transaction_that_reads_then_writes_survives_a_concurrent_commit FAILED
test_waiting_longer_does_not_help                                      PASSED
test_immediate_is_what_fixes_it                                        PASSED
```

The failure is `OperationalError: database is locked` — the production error,
reproduced with no Gemini call, no Cloud Tasks and no race to lose. Adding
`"transaction_mode": "IMMEDIATE"` turns the first green and the second to a skip
(nothing left to characterise). That setting is now applied.

`test_waiting_longer_does_not_help` is the one that settles the argument between
the two diagnoses. The write fails in well under a second against a 60-second
timeout, which contention cannot produce.

Two things the writing of it turned up, both worth knowing:

- **It cannot use the test database.** Django's SQLite test database is the
  shared in-memory one, where `PRAGMA journal_mode=WAL` is silently a no-op —
  and with no WAL there is no snapshot to invalidate. The tests open their own
  file-backed database via `django_db_blocker.unblock()`. A version of this
  written the obvious way, with `@pytest.mark.django_db(transaction=True)`,
  would have passed against the bug and proved nothing.
- **The competing writer has to run on a thread.** Inline, it can only ever
  demonstrate the broken case: under `IMMEDIATE` it is *supposed* to block on
  our write lock, and an inline write would simply fail and take the test with
  it. On a thread with a patient timeout, the same sequence shows both
  behaviours — sails past us under `DEFERRED`, queues behind us under
  `IMMEDIATE`.

---

## Todo steps

**First, and independent of everything else**

- [ ] Push `a598bf5` to `origin/main` and confirm Cloud Build deploys it.
      (Korey's call and Korey's command — `git push` is denied to me.)
- [ ] Confirm the deployed revision by uploading a receipt and checking the
      traceback is gone, or that any new one has no `contextlib` frame above
      `process_receipt`.

**Then the durable fix**

- [x] Write the reproduction test above and watch it fail against current
      settings. If it does not fail, this diagnosis is wrong and nothing below
      should be applied. **Done** — `core/test_sqlite_transaction_mode.py`. The
      diagnosis holds: see "Confirmed" below.
- [x] Add `"transaction_mode": "IMMEDIATE"` to `DATABASES["default"]["OPTIONS"]`
      in `hf/settings/base.py`, with a comment explaining that `DEFERRED` makes
      every read-then-write `atomic` block a stale-snapshot risk. The stale
      comment on `timeout` was rewritten at the same time — it described the
      superseded diagnosis and pointed at the superseded plan.
- [x] Confirm the test now passes, and run the full suite — `IMMEDIATE` changes
      locking for every test that writes. **703 passed, 1 skipped.**
- [x] Check `spendium/test_processing_transactions.py` still passes and still
      fails when `@atomic` is put back on `process_receipt`; that guard is what
      stops the deployed regression recurring. Verified by putting `@atomic`
      back and watching two of its three tests fail, then reverting.

**Decisions for Korey**

- [x] Lower `timeout` from 60s to ~15s now that it governs a real queue?
      **Done — Korey set it to 15.**
- [x] `SESSION_SAVE_EVERY_REQUEST` — leave as is, accepting the write volume?
      **Left as is.** Recorded in debt item 7, along with why turning it off is
      not the cheap fix it looks like.

**Register the debt**

- [x] Add an item to `plans/operational-debt.md`: nothing detects that the
      running revision has drifted from `origin/main`. A fix sat unpushed for
      five days while the bug it fixed kept mailing tracebacks. **Item 10**, and
      first in the suggested order.
- [x] Amend item 7 (session writes): the predicted symptom was contention; the
      observed symptom was snapshot invalidation. Worth correcting so the next
      reader is not looking for the wrong thing.
- [x] Add a correction note to `plans/receipt-processing-lock-contention.md`
      pointing here, so its diagnosis is not taken at face value later. Its
      *remedy* was right; its explanation was not.
