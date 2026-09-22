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
**Update 2026-09-19: measured, and coupled explicitly to item 5.**

This entry has always been a stated risk rather than an observed one. It is now
measured. Over fourteen days:

| | |
|---|---|
| container starts | 440 |
| graceful shutdowns (`signal received, litestream shutting down`) | 439 |
| OOM kills, 30 days | **0** |

The one-off difference is the instance running at the time of the count. So
**essentially every termination in 440 container lifetimes took the graceful
path.** Nor is the 10-second SIGTERM grace period close to tight: signal to
`litestream shut down` measures 128–209ms across sampled shutdowns, under 2% of
the allowance. There is no plausible route to overrunning it.

**Why this is item 5's problem, not this entry's.** The only realistic cause of a
hard kill here is an OOM — the other candidates are platform failures outside our
control, and the grace period has two orders of magnitude of headroom. So the
hard-kill *rate* is not a constant; it is a function of memory pressure, and item
5 records that the database has no size ceiling while `/data` is memory-backed
and only grows. **Item 5 is the trigger for this entry, and this entry is one of
the reasons item 5 matters.** Neither should be read alone.

**Why it matters more later than now.** The exposure is bounded by unreplicated
writes, and production currently has one player and no survey responses. Losing
a second of writes today costs nothing. After launch it costs real user data —
and that is also when the database is growing fastest, which is when the OOM
becomes likely. The two curves move together, in the wrong direction.

**What now depends on this.** `plans/litestream-retention-cost.md` proposes
raising `sync-interval` from 1s to 10s to remove the residual ~$8/month of GCS
LIST operations. That widens this window tenfold. On the measurement above that
is defensible — ten times a rate of zero is still zero — but it is a decision
with an expiry date rather than a permanent one. **Revisit at launch, with item 5
as the trigger rather than the calendar.** If the sync interval is raised, note
here that it was, so the next person reading this entry knows the window is 10s
and not the 1s the text above describes.


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

## 11. The Action Centre ignores the prompt budget

**Where:** `spendium/action_centre.py:84-91`, `templates/spendium/action_centre.html:49`

`disambiguation.prompt_queue` caps prompts at `MatchConfig.prompt_budget`
(default 5) per receipt, and the field's help text gives the reason: "Players
who see fifteen icons ignore all of them."

`action_centre.unresolved_disambiguations` has no cap. It returns every pending
line the player has and the template renders all of them. The exact failure the
per-receipt budget exists to prevent is reachable in one click from the same
interface, and it grows with every receipt uploaded.

**Decision: left alone, deliberately, 2026-08-02.** Raised while adding the
Accept button (`plans/accept-the-suggested-description.md`) and declined for now
— nobody has enough receipts for it to bite yet, and the right cap is probably
not 5, since a page the player chose to open can reasonably show more than a
receipt they were merely looking at. Korey will say when it becomes annoying.

Recorded because the trigger is gradual: no single upload makes this bad, so
there is no moment at which anyone would notice it happening. The Accept button
also makes it likelier to be noticed, since it gives players a reason to go
looking for these.

---

## 12. Rate limit counters do not survive a cold start

**Where:** `accounts/ratelimit.py`, `hf/settings/prod.py:13`,
`terraform/cloud_run.tf` (`min_instance_count = 0`)

`check_rate_limit` keeps its counters in the default cache, which in production
is `LocMemCache` — in-process, and gone when the process is. `min_instance_count`
is zero, so the container is recycled whenever traffic stops. Every cold start
resets every rate limit in the app: signup, verification resend, everything built
on this helper.

A consequence of item 1 rather than a separate fault, but listed separately
because item 1 is written as a *correctness* problem about shared caches and this
is an *abuse control* silently reset on a schedule an attacker does not even have
to know about.

**Decide:** with Redis, this is free — the counters simply live somewhere that
outlives the process. Until then it is worth knowing that the documented limits
are upper bounds under sustained traffic and nothing at all under intermittent
traffic.

Found 2026-08-02 while investigating `plans/bot-signups.md`.

---

## 13. Error alerts depend on the thing most likely to be broken

**Where:** `hf/settings/prod.py` (`LOGGING`, `ADMINS`, `EMAIL_BACKEND`)

Unhandled exceptions are reported by emailing `ADMINS` through Mailgun. So when
Mailgun is what is broken, the report about it is sent through Mailgun.

Not hypothetical. Between 20 and 26 July 2026 every signup returned a 500
because `MAILGUN_SENDER_DOMAIN` named a domain absent from the Mailgun account.
Django tried to mail a traceback for each one; each of those mails failed the
same way. Twenty consecutive days of a broken signup page produced no alert, and
it was found by looking at the player list for unrelated reasons.

`monitoring.tf` alerts on 5xx rate through Cloud Monitoring, which does not
depend on Mailgun — so there *was* a second channel. Worth confirming whether it
fired and, if not, why: an alert policy that missed twenty 500s is a bigger
problem than the mail backend.

**Decide:** at minimum, confirm the Cloud Monitoring 5xx policy actually fires,
since it is the only alerting path that survives an email outage. Beyond that,
mail delivery failures deserve to be visible somewhere that is not email —
`AdminEmailHandler` swallows its own exceptions by design, so a failing alert
channel is silent by construction.

Found 2026-08-03 while diagnosing a 500 on the magic-link endpoint. See
`plans/bot-signups.md`.

---

## 14. Litestream generations are never pruned — FIXED 2026-08-08, and the fix cost $375/year until 2026-09-05

**Where:** `litestream.yml`, `terraform/storage.tf`

**Correction 2026-09-05. The fix below was worse than the problem it solved.**

`retention-check-interval: 5m` did stop generations accumulating. It also began
costing **about $1.03 CAD/day in GCS LIST operations** — $375/year — to prune
2 MiB of snapshots that were costing nothing at all. Measured on 2026-09-04,
whole project, one day:

| Operations | Bucket | Method |
|---|---|---|
| **150,798** | `hf-litestream-*` | **ListObjects** |
| 88 | `hf-litestream-*` | DeleteObject |
| 74 | `hf-litestream-*` | WriteObject |
| 72 | `hf-litestream-*` | ReadObject |

Everything else in the project is single or double digits. LIST outnumbers every
other operation about 1,700 to 1.

The mechanism: the retention check walks every generation individually, so it
costs `generations × checks` — roughly 857 LISTs every five minutes a container
is awake, across ~14 hours of daily container uptime. Note what follows from
that shape: **generation count is 0.1% of the cost and check frequency is
99.9%**, so shortening the retention window would have achieved nothing.

Resolved by setting the interval to `24h` — longer than any container lives, so
it never fires — and letting the 30-day GCS lifecycle rule in
`terraform/storage.tf` do the pruning it was always able to do. Full working in
`plans/litestream-retention-cost.md`.

**Three things worth carrying forward, none of them about Litestream:**

1. **The original problem was 2 MiB.** It was never costing anything. The entry
   below correctly says "Nothing is at risk" and "it costs nothing" and then
   proposes a fix anyway. Tidiness is not a reason to change a running system.
2. **The fix was never priced.** A five-minute interval against a growing list of
   generations is a near-quadratic cost, and no one asked what it would bill.
3. **The lesson from the first fix caused the second fault.** The entry below
   ends by warning that *a periodic task whose interval exceeds the process
   lifetime never runs at all* — so the remedy chosen was an interval far
   *shorter* than the process lifetime. Correct for reliability, and expensive
   for precisely the same reason. A rule of thumb applied without measuring is
   just a different way to be wrong.

The general lesson at the bottom of this entry still stands. It simply is not the
only consideration.

*Original entry, uncorrected, follows.*

Thirty-odd generation directories accumulated since May, each holding a full
snapshot of the database, one per container start.

**The cause was not missing retention.** Litestream 0.3.13 defaults `retention`
to 24h and it was active the whole time. What was missing was any opportunity to
act: `retention-check-interval` defaults to **one hour**, and since `cpu_idle`
became true the container scales to zero and restarts roughly hourly, so the
cleanup pass almost never survived long enough to run.

Fixed by setting `retention-check-interval: 5m`, which is comfortably inside even
a short-lived instance, and `retention: 168h` — a week, chosen for the recovery
window rather than for storage, since snapshots are ~95KB and item 13 means a
problem could go unnoticed for longer than a day.

Worth keeping the general shape of this in mind: **a periodic task whose interval
exceeds the process lifetime never runs at all.** Nothing reports that; it simply
does not happen. Anything else on an hourly in-process timer deserves the same
question.

Original entry follows.

No retention is configured, so Litestream leaves a generation directory behind
on every container start. Thirty-odd have accumulated since May, each holding a
full snapshot of the database.

2.13 MiB today, so it costs nothing. Two things make it worth writing down
rather than ignoring:

- **The rate just went up.** With `cpu_idle = true` the container scales to zero
  and restarts roughly hourly, so generations now accrue at about 24/day rather
  than a handful.
- **Each one holds a whole snapshot**, so the total grows with the product of
  restart frequency and database size — both of which increase over time.

Nothing is at risk; restore always uses the newest generation. This is a storage
bill that grows quietly and a listing that gets harder to read when you are
trying to diagnose something under pressure.

**Decide:** set a retention policy in `litestream.yml`, or add a GCS lifecycle
rule on the replica bucket. The first is better — it keeps the decision next to
the thing making the objects.

Found 2026-08-08 while verifying the `cpu_idle` change.

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
9. Item 11 — waiting on evidence, and the evidence is Korey finding the Action
   Centre annoying. Cheap whenever it is picked up.
10. Item 12 — folded into item 1 whenever Redis lands. Worth knowing before then
    that the signup rate limit is weaker than it reads.

Item 13 jumps the queue: confirming the Cloud Monitoring 5xx policy fires is
worth doing before anything else on this list, because every other item here is
found late if alerting does not work.

Item 7 has left this list: it fired on 2026-07-31, was resolved at the database
configuration rather than at the session store, and is kept above only for the
correction it carries — the symptom it predicted was not the symptom it produced.
## 15. A commit that changes code and infrastructure ships only half of itself

**Where:** `cloudbuild.yaml`, `terraform/`

**Severity: high.** This has already caused one silent 23-day outage of a
scheduled job, and the mechanism is entirely general.

Pushing to `main` fires the Cloud Build trigger, which runs build → push →
migrate → deploy → smoke → cutover. Application code therefore ships itself.
There is no `terraform` step in that pipeline, and no `.github/workflows`
directory. Infrastructure changes ship only when someone remembers to run
`terraform apply` from a laptop.

So a commit touching both halves deploys the code half automatically and leaves
the infrastructure half sitting in the repo, with **nothing reporting the
difference**. The repo looks correct — it *is* correct — while production
disagrees with it. This is item 10's problem pointed the other way: there, the
image might not match the repo; here, the infrastructure does not, and the repo
being right is exactly what makes it hard to spot.

**The worked example.** Commit 9e8a2d5 renamed a task path and updated
`terraform/cloud_scheduler.tf` in the same commit, correctly. The code deployed
that afternoon; the Terraform was never applied. `hf-snapshot-product-ratings`
then called a URL that no longer existed, 404ing every morning from 2026-08-09
to 2026-08-31 — 23 consecutive failures, found by hand while looking at
something else. Full write-up in `plans/scheduler-url-drift.md`.

That incident cost nothing in the end, because production has no survey
responses yet and the job's correct output was zero rows either way. It would
not have been free three months from now.

**Partly mitigated 2026-08-31.** `google_monitoring_alert_policy.scheduler_failure`
now emails on any Cloud Scheduler job failure, so this *class* of drift gets
caught within a day for scheduled jobs specifically. `core/test_scheduler_targets.py`
catches the related case where only one of the two files is updated. Neither
touches the general problem: an unapplied change to Cloud Run limits, IAM, or
bucket lifecycle would still be invisible.

The alert was **verified end to end before being trusted**, not just deployed: a
throwaway job pointed at a nonexistent path produced the ERROR log, incremented
the metric, opened the incident and delivered the email. Worth stating because
the last policy added to this channel sat unproven for a month, and the
reasonable-looking assumption about it (item 13) turned out to be wrong in a way
nobody could have guessed from reading the config.

**Decide:** the honest options are (a) put `terraform plan` in CI so drift is
*reported* on every push without granting the pipeline apply rights, (b) add a
gated `terraform apply` step with an approval, or (c) accept manual applies and
rely on per-resource alerting like the one just added. (a) is the cheapest real
improvement and does not require trusting the build with broad IAM — but there
is no CI at all today, so it means standing that up first.

Found 2026-08-31 while investigating the scheduler 404s.

---

## 16. Production has never been seeded with survey criteria

**Where:** `spendium/management/commands/seed_spendium_criteria.py`,
`spendium/management/commands/seed_store_criteria.py`

**Severity: low, but it hides other faults.** Not a bug — a setup step that has
not been run.

Counted directly from the production database on 2026-08-31: `surveys_criterion`
is empty. So is `surveys_surveyresponse`. There are 93 products, 8 stores, 70
purchase line items and 1 player, so the catalogue and receipt-reading paths have
been exercised; the rating path has not, because with no criteria there is
nothing to answer.

`seed_store_criteria` shipped in commit 9e8a2d5 and has never been run at all,
which is the same shape as item 15: code that arrived in a deploy alongside a
setup step nobody performed.

**Why it is worth an entry rather than just doing it.** While it stays this way,
every rating-dependent code path returns empty and looks healthy doing so.
`snapshot_all()` writes zero rows, `top_rated()` returns `[]`, the Action Centre's
rating-mover set is empty. A genuine fault anywhere in that chain would produce
exactly the same output as the current correct behaviour, which is how the
scheduler 404 went unnoticed for as long as it did — there was no visible
difference between "broken" and "nothing to do yet".

**Decide:** run both seed commands against production, or decide deliberately
that Spendium ratings stay dormant until launch and note that here so the next
person reading empty snapshot tables does not go looking for a bug.

Found 2026-08-31 while verifying the scheduler fix actually wrote rows.

---

## 17. Scheduled jobs had no retry policy — FIXED 2026-09-01

**Where:** `terraform/cloud_scheduler.tf`

No `retry_config` was set on any of the ten scheduler jobs, so `retry_count`
defaulted to zero. One non-2xx response and the run was abandoned outright.

**It bit before it was noticed.** On 2026-09-01 `hf-action-centre-emails` — which
runs `0 15 * * 2`, once a week — fired into a cold start that never finished
booting, took a 503 at 15:01:58, and stopped there. No retry, and the next
scheduled attempt was seven days away. The work was recovered only because
someone happened to be reading the logs for an unrelated reason.

Fixed by adding `retry_count = 3` with a 30s-to-300s backoff to all ten jobs.
Retrying is safe for every one of them: the sweeps document their idempotency,
the snapshots are `update_or_create` keyed on today's date, hotness and
retro-match are recomputations, and `send_action_centre_emails` deliberately
records that it sent *before* sending so a retry cannot double-mail.

**The general shape, which is the part worth keeping.** Frequency hid the fault.
Eight of the ten jobs run hourly or daily, so a dropped run was invisible — the
next one along fixed it. Only the weekly job had a gap long enough for the
missing retry to matter, and it is the one job where a lost run costs something
that cannot be recovered by waiting. **A defect that only shows on the rarest
code path is not a rare defect; it is a defect with a long fuse.** Anything else
here that runs weekly or less deserves the same look.

Worth noting alongside item 13: alerting would not have caught this either. The
`scheduler_failure` policy added 2026-08-31 fires on the *failed attempt*, which
is right, but nothing watches for work that simply never happened.

Found 2026-09-01 while investigating the cold-start hang
(`plans/startup-hang-and-503s.md`).

---

