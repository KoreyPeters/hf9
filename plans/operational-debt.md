# Operational debt

Known problems that are real but were not urgent enough to fix in the moment.
Each entry says what is wrong, why it matters, and what the decision is — so
picking one up does not require reconstructing the reasoning.

This is deliberately separate from `local_only/todo.md`, which tracks design-vs-
implementation gaps (features the design calls for that do not exist yet). This
file tracks things that *are* built and are wrong, mostly operational.

Opened 2026-07-26, from findings during the Cloud Tasks IAM incident.

---

## 1. Production has no shared cache, and is pinned to one worker because of it

**Severity: medium.** Was high — the symptoms below were live in production until
2026-07-26, when the OOM fix dropped uvicorn to `--workers 1`. A single process
means one `LocMemCache` shared by every request, so all four symptoms are
resolved *incidentally*. What remains is that the worker count is now load-
bearing for correctness, which nothing but a comment in `start.sh` records.

**Where:** `hf/settings/prod.py:13`, `start.sh`, and every `cache.*` caller

`hf/settings/base.py:97` defines a Redis backend against `REDIS_URL`, and
`CLAUDE.md` states production uses Redis via Cloud Memorystore. Neither is true:
`prod.py:13` overrides it to `LocMemCache`, with no comment saying why.
`LocMemCache` is per-process, and `start.sh` runs uvicorn with `--workers 4`, so
production has four independent caches and requests land on them arbitrarily.

This is invisible in development, where `runserver` is single-process and every
one of these works perfectly.

What broke while there were four workers, worst first — and what will break again
the moment anyone raises the count without adding Redis:

- **Passkey registration and login are likely broken.** `accounts/passkey.py:30`
  stores the WebAuthn challenge with `cache.set`, and `accounts/passkey.py:37`
  reads it back — in a *different HTTP request*, after the browser has done its
  part. With four private caches, the second request only finds the challenge if
  it happens to land on the same worker. The rest of the time it raises
  "Registration session expired." Expect roughly a 3-in-4 failure rate. Same
  pattern for authentication at `passkey.py:70-71` and `passkey.py:79-83`.
- **Rate limits are 4× looser than configured.** `accounts/ratelimit.py:13-16`
  counts per process, so each worker permits the full allowance independently.
  This guards signup and verification resend, so it is an abuse control, not a
  nicety.
- **Alert email throttling delivers up to 4× the mail.** `core/logging.py:35`
  uses `cache.add` and its docstring says exactly why: "concurrent workers cannot
  each decide they are the first." With per-process caches they can and do. The
  window also resets on every container start.
- **Action Centre badge counts go stale unevenly.** `spendium/context_processors.py:22-36`
  caches per player and `invalidate()` clears one worker's copy, so the badge can
  show different numbers on consecutive page loads. Cosmetic.

**Not affected:** spendium velocity limiting. `SPENDIUM["VELOCITY_LIMIT_PER_HOUR"]`
is enforced by a database count in `spendium/abuse.py:46-47`, not the cache.

**Decide:** stand up Memorystore and point `REDIS_URL` at it, which is what
would let the worker count be a performance decision again rather than a
correctness one. Not urgent while a single worker is enough for the traffic.

Either way `CLAUDE.md` needs correcting — it documents Redis via Cloud
Memorystore, which has never existed.

**Verify:** the passkey failure is inferred from reading the code, not observed.
Worth confirming a passkey now registers and authenticates cleanly, both to close
this out and to establish it was ever broken.

---

## 2. `hf-migrate` does not run migrations

**Where:** `migrate.sh`, `terraform/cloud_run.tf` (`google_cloud_run_v2_job.migrate`),
`cloudbuild.yaml:31`

The job restores to its own `/data` and runs `collectstatic`. It does not run
`migrate` and, with SQLite on a per-container volume, it could not usefully do so —
it is a different container with a different filesystem, so any migration it
applied would be to a copy that is then discarded.

The actual migrations run in `start.sh:10`, at service boot.

Nothing is broken. The hazard is the name: someone will eventually add a
migration step to the job, watch it succeed, and conclude production is migrated.

**Decide:** rename the job to what it does (`hf-collectstatic`), or fold the step
into the build and drop the job. Either way, comment `start.sh` to say that boot
is where migrations actually happen.

---

## 3. WhiteNoise has nothing to serve

**Where:** `hf/settings/base.py:126`, service logs

Every boot logs `UserWarning: No directory at: /app/staticfiles/`, because
`collectstatic` runs in the migrate job's container and the service's container
never gets the output. Static assets are served from GCS via `STATIC_URL`, so
nothing is visibly broken.

But `CompressedManifestStaticFilesStorage` is configured and WhiteNoise sits in
the middleware chain on every request doing nothing, and the local fallback
people assume is there is not.

**Decide:** run `collectstatic` in the Dockerfile so the service has the files, or
drop WhiteNoise from production middleware and let GCS own static entirely. Tied
to item 2 — both stem from `collectstatic` running in the wrong container.

---

## 4. `enqueue` call sites outside spendium still fail hard

**Where:** `accounts/views.py:118`, `polium/views.py:1103`

`spendium/service.py` now absorbs enqueue failures via `_enqueue_or_sweep`, which
is safe there *only* because every task it covers has a scheduled sweep that calls
its handler directly. These two have no such backstop, so they were deliberately
left raising — absorbing them would silently lose the work instead of a receipt's
processing merely being delayed.

That is the right default, but it was a decision made in passing rather than one
anyone weighed. `update-candidate-rating` dropping means a stale rating with
nothing to notice; `verify-email-reminder` dropping means a player never gets
chased.

**Decide:** per call site — give it a sweep and absorb, or leave it raising and
accept a 500. Do not generalise `_enqueue_or_sweep` into `core.tasks.enqueue`;
whether a dropped task is survivable is a per-caller fact.

---

## 5. Database sizing still has no ceiling, though it now has an alarm

**Where:** `terraform/cloud_run.tf` (service `volumes` / `resources`)

The SQLite database lives on a `medium = "MEMORY"` empty_dir capped at 256Mi,
inside the container's memory limit, so it is resident in RAM and grows forever.

Partly addressed 2026-07-26: the container went to 1Gi and `--workers 1` after
three OOM kills, and `monitoring.tf` now alerts on the OOM log line directly, so
the next one will be noticed rather than inferred. That is a smoke detector, not
a fire escape.

What is still missing is knowing how much room is actually left. Nothing tracks
database size, so the first sign of the ceiling will be the alert firing. The
failure mode is nasty on a memory-backed volume: an OOM kill is a SIGKILL, so
Litestream never runs its final sync and the tail of the WAL is lost (item 6).

**Decide:** track database file size — a metric, or just log it at boot — and
work out the real ceiling before receipts accumulate. Raising the volume's
`size_limit` does nothing without raising container memory alongside it.

---

## 6. Scale-to-zero can lose the tail of the WAL

**Where:** `terraform/cloud_run.tf` (`cpu_idle = false`, `min_instance_count = 0`)

Accepted trade, recorded so it is not rediscovered as a surprise.

Graceful shutdown is verified working: Litestream signals uvicorn, waits for it to
close, then syncs. But an instance killed without SIGTERM (OOM, platform
eviction) loses whatever had not replicated — bounded by Litestream's sync
interval, so roughly a second of writes.

`min_instance_count = 1` would shrink the window at the cost of an always-on
instance. Explicitly declined 2026-07-26 in favour of scale-to-zero.

**No action.** Revisit if data loss is ever observed, or if item 5 makes OOM kills
likely.

---

## 7. Session writes land on a single-writer database

**Where:** `hf/settings/base.py` (`SESSION_SAVE_EVERY_REQUEST`)

Rolling year-long sessions mean a session row is rewritten on every authenticated
request rather than once per login. That is the intended tradeoff — the point is
that a ten-second errand should never meet a login screen — but it multiplies
writes on a database that permits exactly one writer, and every write becomes WAL
that Litestream ships to GCS.

**No action now.** First thing to examine if write contention or replication lag
appears. The cheap fix if it does: a cache-backed session store, which needs item
1 resolved first, since sessions in `LocMemCache` would not survive across workers.

---

## Suggested order

1. Items 2 and 3 together — both are `collectstatic` running in the wrong
   container, and both are small.
2. Item 5 — track database size. Cheapest remaining item, and the only one whose
   absence means a failure arrives with no warning.
3. Item 4 — a decision to make deliberately rather than a bug to fix.
4. Item 1 — no longer urgent now that one worker makes the cache coherent, but it
   is what pins the worker count. Confirm passkeys work before closing it out.
5. Items 6 and 7 — recorded, no action, revisit on evidence.
