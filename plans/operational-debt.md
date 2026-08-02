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

**Update 2026-08-02: it happened, and the prediction was half right.**

Every receipt upload was failing with "database is locked", and this was the
other half of the mechanism — the session write on the redirect after upload is
what broke the `process-receipt` task's transaction. So the write pressure noted
here was real and it was load-bearing.

But not through contention, which is what "first thing to examine if write
contention appears" led the first investigation to look for, and why that
investigation reached the wrong answer. The session write did not make anything
*wait*. It committed, instantly and successfully, and thereby invalidated the WAL
snapshot of a transaction that had been open across a Gemini call — which then
failed the moment it tried to write. See
`plans/receipt-upload-database-locked.md`.

Fixed at the other end, by `transaction_mode = "IMMEDIATE"`, so a read-then-write
transaction takes the write lock up front and has no snapshot to lose. The
session writes are unchanged and no longer harmful.

**Still no action here**, but the reason has changed. It is no longer a latent
hazard waiting to bite; it is ordinary write volume, and the thing that made it
dangerous is gone. What it still costs is WAL that Litestream ships to GCS on
every authenticated request.

The cheap fix if that ever matters: a cache-backed session store, which needs
item 1 resolved first, since sessions in `LocMemCache` would not survive across
workers. Turning off `SESSION_SAVE_EVERY_REQUEST` is *not* the cheap fix — it
would change `SESSION_COOKIE_AGE` from meaning "a year since you last used it" to
"a year since you signed up", which is the behaviour the comment above it
deliberately argues against.

---

## 8. Stores are deduplicated by name only, and nothing merges them

**Where:** `spendium/service.py:70-82`, `spendium/models.py:29-50`

`_resolve_store` matches the printed receipt name case-insensitively and creates
a new `Store` on a miss. The comment there is honest about it being naive, and
argues correctly that a fragmented store list costs less than wrongly merging two
chains, which would pool their aliases and corrupt retailer-scoped matching.

Three things make it debt rather than a settled tradeoff:

- `Store.flag_count` is hard-coded to `0` (`models.py:44`), so the
  `LifecycleMixin` it inherits can never deprecate anything. The community
  route out of a duplicate does not exist.
- There is no `merged_into` and no merge-group resolution. `Product` has both
  (`models.py:144`, `catalogue.merge_group_ids`), and product ratings aggregate
  across the group precisely so a merge does not orphan ratings. Stores have
  no equivalent.
- Receipt store names are not stable strings. "LOBLAWS", "Loblaws #1234" and
  "LOBLAW GREAT FOOD" are three records for one chain.

**Why it is getting worse.** Today fragmentation costs a slightly untidy admin
list. Once stores are rated (`plans/rateable-subjects-and-store-ratings.md`) it
costs real points: `points.store_points` pays `spend × compute_declaration_points(store)`,
so two records for one chain accumulate separate survey responses, clear the
k-threshold separately or not at all, and pay different rates for shopping at
the same place. Players will notice this before we do.

**Decision:** not fixed as part of store ratings — it is a design problem of its
own, and store rating is worth shipping without it. It is the natural next plan.
The cheap partial mitigation, if it bites before then, is admin-side merge with
a `merged_into` FK mirroring `Product`, since the aggregation code would then be
the same shape in both places.

---

## 9. `criteria_version` is recorded on every response and read by nothing

**Where:** `surveys/models.py:12,87`, `surveys/ratings.py:11,40`

`Category.criteria_version` is bumped when a question set changes, and every
`SurveyResponse` records the version in force when it was answered. The field's
own help text states the guarantee this buys:

> "Responses record the version they were given under, so answers to different
> questions are never averaged together as though they were the same."

That guarantee does not exist. The field is written at `surveys/service.py:94`,
`spendium/views.py:402` and by the seeding command, and read only by tests
asserting it was written. Neither `compute_rating` (`surveys/ratings.py:11`) nor
`compute_declaration_points` (`:40`) filters, groups or partitions by it — they
select on `criterion__is_active` and the 365-day window and nothing else. Answers
given under version 1 and version 2 are pooled exactly as though the field were
absent.

**Why it matters.** The point of versioning is that a rating means "answers to
*these* questions". Bump the version — which `seed_spendium_criteria.py --bump-version`
invites — and the displayed rating silently becomes an average across two
different question sets, which is the specific outcome the field was added to
prevent. The rating is not wrong so much as it stops meaning anything precise,
and nothing in the interface says so.

**Decision:** not fixed alongside store ratings. What to *do* about old-version
answers is a genuine design question — drop them, decay them, show them
separately, or hold a rating steady until the new set has enough responses — and
each has a different effect on what players see the day criteria change. The
plan `plans/rateable-subjects-and-store-ratings.md` moves the field onto
`CriterionAnswer` so it is well-defined once a subject can match several
categories, but deliberately leaves the aggregation untouched.

Worth resolving **before** the first real criteria change rather than after,
since the ambiguity is invisible until someone bumps a version and then applies
retroactively to everything already collected.

---

## 10. Nothing notices when production is not running the code we think it is

**Severity: medium**, and it is the one item here that has already cost real
time rather than merely threatening to.

**Where:** `cloudbuild.yaml`, and the absence of anything else

Deploys are triggered by a push to `origin/main`. There is no check anywhere —
not in the build, not in the app, not in monitoring — that the revision serving
traffic is the revision at the head of the branch. Nothing distinguishes "the
fix is deployed and did not work" from "the fix was never deployed".

**How it bit.** The transaction split that `plans/receipt-processing-lock-contention.md`
describes was committed on 2026-07-26 as `a598bf5` and not pushed. Production
went on running `fae4873` for six days, mailing an `OperationalError` traceback
on every receipt upload, while the working tree contained a fix for it and a
plan document describing that fix in the past tense. The traceback itself was
the only thing that gave it away, and only because a stack frame
(`contextlib.py`, from an `@atomic` decorator that no longer exists in the
working tree) happened to be inconsistent with the current source.

That is a bad detection story. It relied on reading a traceback closely enough
to notice a frame that should not have been there. A slightly different bug —
one whose traceback was consistent with both revisions — would have sent the
investigation into the current source looking for a fault that had already been
fixed there.

**Why the existing checks do not cover it.** `cloudbuild.yaml:63` smoke-tests a
deploy that ran. Nothing tests for a deploy that never started. The monitoring
in `monitoring.tf` watches the running service's behaviour, which is exactly the
thing that looks normal when old code is running correctly.

**Decide:** the cheap version is to bake `$SHORT_SHA` into the image as an env
var and surface it — a `/healthz` field, a log line at boot, or the admin
footer. That alone converts "is the fix live?" from an inference into a lookup.
The thorough version compares it to `origin/main` and alerts on drift, which is
more machinery than this project needs today.

Worth pairing with a habit rather than only a tool: the working agreement ends
at "Korey commits and pushes", and nothing in the loop closes over whether the
push actually happened.

---

## Suggested order

1. Item 10 — surface the running revision. Cheapest thing on the list, and the
   only one that has already wasted a debugging session rather than merely
   threatening to.
2. Items 2 and 3 together — both are `collectstatic` running in the wrong
   container, and both are small.
3. Item 5 — track database size. The only remaining item whose absence means a
   failure arrives with no warning at all.
4. Item 4 — a decision to make deliberately rather than a bug to fix.
5. Item 1 — no longer urgent now that one worker makes the cache coherent, but it
   is what pins the worker count. Confirm passkeys work before closing it out.
6. Item 6 — recorded, no action, revisit on evidence.
7. Item 8 — no action until store ratings ship, then reassess. It is the one
   item on this list that a shipping feature actively makes worse.
8. Item 9 — no deadline, but a trigger: resolve it before the first deliberate
   criteria change, not after. Afterwards the fix has to decide what to do with
   answers already pooled.

Item 7 has left this list: it fired on 2026-07-31, was resolved at the database
configuration rather than at the session store, and is kept above only for the
correction it carries — the symptom it predicted was not the symptom it produced.
