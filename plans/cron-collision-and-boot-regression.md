# The cron collision is real, and it is a symptom of something worse

You asked me to fix the scheduler collision that has been emailing you. It is
real and the fix is one line. But it only started producing 429s because **cold
starts have tripled since 2026-09-05, and that regression is a direct consequence
of the Litestream retention change I made on that date.** It is still getting
worse, and on its current trajectory it re-creates the 503 outage from
2026-09-01.

Fixing only the cron would remove the emails while the actual problem kept
growing. That is the worst of both outcomes, because the emails are currently the
only thing pointing at it.

---

## Part A — the collision you asked about

`check_deprecations` runs `0 * * * *`. Five other jobs also run at minute zero:

| time | job |
|---|---|
| 02:00 | `check_deletions` |
| 03:00 | `sweep_purchase_anonymisation` |
| 05:00 | `snapshot_ratings` |
| 06:00 | `recompute_hotness` |
| Tue 15:00 | `action_centre_emails` |

With `maxScale: 1`, when two jobs fire seconds apart against a cold instance the
second has nowhere to queue and gets a 429. Every 429 in the last 30 days but one
was `check_deprecations`, because it is the job present at all five collision
points:

```
2026-09-21T03:01  hf-check-deprecations
2026-09-20T03:01  hf-check-deprecations
2026-09-20T02:01  hf-check-deprecations
2026-09-19T05:00  hf-check-deprecations
2026-09-19T03:00  hf-check-deprecations1
2026-09-02T04:30  hf-retro-match          ← not a collision; see below
```

### The fix: one line

```hcl
# terraform/cloud_scheduler.tf, check_deprecations
schedule = "15 * * * *"   # was "0 * * * *"
```

Minute :15 is unused. The hourly cluster becomes :05, :10, :15 — the same
10-minute span it occupies today at :00, :05, :10 — so the wake-window reasoning
in the file's comments is preserved exactly, and the daily jobs at :00 get five
clear minutes before `sweep_pending_receipts` at :05. One change closes all five
collision points, where moving the five daily jobs individually would be five
changes and five chances to introduce a new overlap.

**A stale comment to fix while there.** The clustering rationale at
`cloud_scheduler.tf:119` says *"The service runs with `cpu_idle = false`, so
Cloud Run bills CPU for an instance's whole lifetime."* That has been false since
2026-08-08 — `cloud_run.tf:119` sets `cpu_idle = true`. The clustering is still
worth keeping (it saves cold starts, which are now expensive for a different
reason), but the stated reason is wrong and would mislead the next person costing
a change.

**The `retro_match` 429 is unexplained.** Nothing else is scheduled at 04:30. It
was most likely a cold start coinciding with scanner traffic. This fix will not
eliminate 429s entirely, because the underlying cause is `maxScale: 1`, not the
cron layout.

---

## Part B — why it started firing now

The collision has existed since these jobs were written. What changed is the size
of the window it can fail in.

**Startup probe attempts per cold start, by day** (`period_seconds = 5`, so each
attempt is about five seconds of boot time):

| date | mean attempts | max |
|---|---|---|
| 2026-09-06 | **1.0** | 1 |
| 2026-09-08 | 2.4 | 8 |
| 2026-09-11 | 2.7 | 5 |
| 2026-09-14 | 3.6 | 6 |
| 2026-09-17 | 5.2 | 8 |
| 2026-09-21 | **5.6** | 7 |
| 2026-09-22 | 5.7 | 6 |

Monotonic, ~0.3 attempts/day, no plateau. Boot has gone from about 20 seconds to
about 60, and a measured cold start on 2026-09-22 confirms it:

```
01:00:26  Starting new instance
01:00:33  (container up, start.sh begins)
01:01:15  restoring snapshot          ← 42 seconds of silence
01:01:15  renaming database           ← the restore itself: 0.2s
01:01:22  Operations to perform       ← migrate
01:01:26  Uvicorn running
```

The restore is not slow. **The 42 seconds happen before it starts.**

### What is in the gap

`start.sh:4` runs:

```sh
litestream restore -if-db-not-exists -if-replica-exists \
  -o /data/db.sqlite3 "gcs://${LITESTREAM_GCS_BUCKET}/hf/db"
```

To find the newest generation, Litestream 0.3 enumerates the generations
directory and then inspects **each generation individually**. That is O(n) in
LIST calls, at roughly 50ms each.

Generation count:

| date | generations |
|---|---|
| 2026-09-05 (fix deployed) | 252 |
| 2026-09-19 | 690 |
| 2026-09-22 | **758** |

758 × ~50ms ≈ 38 seconds, which is the gap.

### The cause is the retention fix

`plans/litestream-retention-cost.md` set `retention-check-interval: 24h`,
deliberately longer than a container's ~26-minute life so that it never fires,
handing pruning to the 30-day GCS lifecycle rule. That worked exactly as
designed: nothing prunes generations any more, and they accumulate at ~23–30/day.

The degradation begins 2026-09-06. Revision `hf-app-00044-k5w`, carrying that
change, deployed 2026-09-05. **My change, and the plan's own "what I am
deliberately not doing" section explicitly dismissed this**: *"Reducing the
number of generations — one per container start is inherent to Litestream 0.3 on
ephemeral containers, and it turns out not to be the cost."* That was true of the
billing and false of everything else. Generation count is not a cost problem; it
is a **latency** problem, and nobody checked that axis.

### Where it is heading

Generations are pruned by the 30-day lifecycle rule, so the steady state is about
30 days × ~25/day ≈ **750–900 generations** — which is roughly where we are, so
the plateau is close. That is the good news. The bad news is what it plateaus at:

- Boot ~60–70s against an **80-second startup probe budget**
  (`initial_delay 30 + 10 × 5`). Max observed is already 8 attempts; the
  threshold is 10.
- The 2026-09-01 outage was exactly this: probe budget exhausted → instance
  killed → 503. That incident's cause was never identified. **This mechanism is a
  candidate for it** — though note generations were only ~250 then, so if it was
  the same cause the threshold is lower than this analysis implies.

We are one bad day away from 503s returning, and this time the cause is known.

---

## A correction to the last measurement

`plans/litestream-retention-cost.md` records that after the fix the residual
38,896 LISTs/day were "the 1-second sync interval". **That attribution is at best
half right.**

Restore enumerates every generation, so each cold start costs ~600–750 LISTs at
current counts. At ~30 cold starts/day that is 18,000–22,500 LISTs/day — roughly
half the measured residual, and it scales with generation count rather than with
uptime. The hourly baseline I used (~1,290/hour against ~1,920 seconds of
container uptime) is consistent with restore plus a sub-1/second sync loop, not
with sync alone.

This is the third time in this file's history that a Litestream measurement has
been attributed confidently and incompletely. The pattern is always the same:
a number is explained by the first mechanism that roughly fits, and the
arithmetic closes well enough that nobody looks for a second one.

**Practical consequence:** reducing generation count also reduces the GCS bill,
so Part B's fix and the outstanding `sync-interval` question are not independent.
Settle generations first, then re-measure before touching `sync-interval`.

---

## The options for Part B

The goal is to hold generations at a few hundred without reintroducing the
$31/month of LIST operations.

**Option 1 — shorten the GCS lifecycle rule to 7 days.** *(Recommended.)*
Deterministic, costs nothing, independent of container lifetime. ~25/day × 7 =
about 175 generations, so boot returns to roughly its September-6 figure.

The risk the earlier plan raised: a lifecycle rule prunes whether or not the app
runs, so an app idle longer than the rule loses its entire replica, and
`restore -if-replica-exists` would then start from an empty database rather than
fail loudly. At 7 days that requires a week of total outage — and the hourly
`check_deprecations` means the app cannot be idle that long without a failure
already worth a much louder alarm. Versioning gives a second line of defence.
It is a real risk and it is small, but it is the reason this is a decision rather
than something I would simply do.

**Option 2 — set `retention-check-interval` to ~20 minutes.** Just under the
container lifetime, so it fires about once per container instead of the five or
six times the old 5m setting caused. At a 7-day retention holding ~175
generations, that is 30 containers × 175 ≈ 5,250 LISTs/day ≈ $1 CAD/month.
Affordable, and it keeps pruning next to the thing making the objects.

The catch is that it is tuned against a container lifetime nobody controls. If
instances start living 10 minutes the check never fires and generations grow back
silently; if they live two hours it fires six times and the bill returns. **My
original 5m→24h change was an over-correction** — from far too often to never —
and 20m is the value that was actually wanted. But it is fragile in a way
Option 1 is not.

**Option 3 — both.** Lifecycle rule at 7 days as the deterministic floor,
retention check at 20m as the in-process backstop. Costs about $1/month and does
not depend on either mechanism alone being right.

**Option 4 — a daily task that keeps the newest N generations.** *(Now the
recommendation — see the question below.)* Bounded by **count**, not age, which
removes the idle-app risk entirely: there is no elapsed time after which the
replica disappears, because the rule is "always keep N" rather than "delete older
than D". One LIST plus a handful of deletes per day, so effectively free, and
`core/tasks.py` already has the machinery.

At ~50ms of restore enumeration per generation, N is a direct dial on boot time:

| N | enumeration | recovery points at ~25/day |
|---|---|---|
| 50 | ~2.5s | ~2 days |
| 100 | ~5s | ~4 days |
| 200 | ~10s | ~8 days |

N = 100 keeps boot overhead at about five seconds and gives four days of restore
points. The GCS lifecycle rule stays at 30 days as an untouched backstop.

---

## Korey's question: "would we keep any versions at all? Couldn't we keep the last 10 versions?"

Two separate things, and the answer to the second is no — but the instinct behind
it is right and produced Option 4.

**`num_newer_versions` will not work here.** A GCS lifecycle rule counts *versions
of one object name*. Every Litestream generation is a distinct object name:

```
hf/db/generations/956068f216c529e5/snapshots/00000000.snapshot.lz4
hf/db/generations/0034e05faf465e4b/snapshots/00000000.snapshot.lz4
```

Each is written exactly once and never overwritten, so each has one version and
zero newer versions. `num_newer_versions = 10` would match nothing and delete
nothing. "Keep the last 10 generations" and "keep the last 10 versions" are not
the same statement, and GCS lifecycle can only express the second.

**GCS lifecycle cannot express "keep the newest N generations" at all.** Its
conditions are per-object — age, version count, storage class, prefix match.
There is no "keep the newest N distinct prefixes". Age is the only lever
available to it, which is why Options 1–3 are all age-based. That limitation is
what makes Option 4 worth the small amount of code: the thing you actually want
is expressible, just not in a lifecycle rule.

> **Correction 2026-09-23.** The recoverability argument below is right about
> GCS and wrong about the code that was then written against it. `list_blobs`
> returns blobs with `generation` populated, so `blob.delete()` issues a
> *versioned* delete: it destroys that exact version permanently and object
> versioning never engages. The first real prune therefore **permanently removed
> 1,686 objects that this section says would have been archived.**
>
> Fixed in `core/replica.py` by deleting through `bucket.delete_blob(name)`,
> which removes the live version and leaves a noncurrent one. Four tests now fail
> if it goes back, including one whose fake blob raises on `.delete()`.
>
> The claim below is true from 2026-09-23 onward and was false for the one run
> that mattered most — the unattended-verification run. Full account in the
> implementation notes at the end of this document.

**Would we keep any versions? Yes — arguably too many.** Measured 2026-09-22:

| | objects | size |
|---|---|---|
| live | 1,660 | 92 MB |
| all versions | 2,228 | 122 MB |

The 568 noncurrent objects are generations Litestream deleted before 2026-09-05,
when its retention was still pruning. With versioning on, a lifecycle `Delete`
makes an object noncurrent rather than removing it, and **there is no noncurrent
rule on this bucket**, so those persist indefinitely. A 7-day rule would really
mean "live for 7 days, then noncurrent forever".

Two consequences:

- **It costs nothing.** 122 MB is about $0.003 CAD/month. Not worth acting on.
- **It softens the risk that made Option 1 a decision rather than a default.** The
  earlier worry was that an app idle longer than the rule loses its entire
  replica. With versioning, the objects are still there as noncurrent versions —
  recoverable by hand, though Litestream would not find them and
  `restore -if-replica-exists` would silently start from an empty database. So
  the failure is "starts fresh and needs manual recovery", not "data gone".

Noncurrent versions do not affect boot time either way: a normal LIST returns
only live objects, so Litestream never enumerates them.

**Revised recommendation: Option 4**, N = 100, leaving the 30-day lifecycle rule
and versioning untouched. It expresses the actual intent, it is bounded by count
so idleness is harmless, and it leaves both existing safety nets in place. Option
1 remains a perfectly reasonable smaller change if you would rather not add code.

---

## What I am deliberately not doing

- **Not raising `maxScale` or `min_instance_count`.** That would fix the 429s
  structurally and is the standing Tier 2 decision from
  `plans/startup-hang-and-503s.md`. It costs money and is a bigger decision than
  this bug warrants — but note that if you take it, Part A becomes cosmetic.
- **Not touching `initial_delay_seconds = 30`.** Worth a look on its own — it
  puts a floor under every cold start — but changing the probe while the thing it
  measures is regressing would confuse both.
- **Not upgrading Litestream.** 0.5 changes the on-disk format entirely and may
  well fix this class of problem, but not while chasing a live regression.
- **Not re-opening `sync-interval`.** Blocked on Part B, per the correction above.

## Decisions for you

1. ~~**Which option for Part B?**~~ **Decided 2026-09-23: Option 4, N = 50.**
   Korey chose a smaller N than the 100 I suggested, for a faster boot.

   **What N = 50 buys and costs.** At the observed 25–33 cold starts/day that is
   **roughly 36–48 hours** of restore points, and about 2.5 seconds of enumeration
   on every boot. The window is thinner than the 7 days `retention: 168h` in
   `litestream.yml` documents — so that comment now describes an intent nothing
   implements, and is corrected as part of this work rather than left to mislead.

   Defensible: a 2-day window was explicitly called "thin" in debt item 13 only
   because alerting was unproven at the time. It is now proven (Korey confirmed
   alert emails arrive, 2026-09-01), so a fault gets noticed the same day rather
   than sitting undetected past the window.

   Worth revisiting if cold starts become much more frequent, since N is a count
   and the window it represents shrinks as starts increase. Noted in the task's
   docstring so the next reader sees the coupling.
2. **Should the alert distinguish "failed and recovered" from "failed and lost"?**
   Both 429s this week retried and succeeded, and you were emailed anyway. After
   Part A those emails mostly stop, so this is less urgent than it was — but an
   alert that fires on recovered failures trains you to ignore it, which is
   precisely what debt item 13 is about.
3. **Is 7 days the right recovery window?** Option 1 makes the lifecycle rule the
   effective window, and 30 days was chosen when it was a backstop rather than
   the primary mechanism.

---

## Todo

- [x] `terraform/cloud_scheduler.tf`: `check_deprecations` `0 * * * *` → `15 * * * *`
      — applied. `gcloud scheduler jobs list` confirms no two jobs now share a
      minute: the hourly cluster is :05/:10/:15, the daily jobs sit alone at :00
      of their own hours, and the two :30 jobs are in different hours.
- [x] Fix the stale `cpu_idle = false` claim in the clustering comment at
      `cloud_scheduler.tf:119`, keeping the clustering rationale but correcting
      the reason — the clustering now justifies itself by cold-start cost
      (startup CPU plus a LIST per generation) rather than by idle billing.
- [x] Implement the chosen Part B option — `core/replica.py` and
      `core/task_views.py`, wired into `hf/task_urls.py` and registered in
      `core/apps.py`, with `LITESTREAM_GCS_BUCKET` and
      `LITESTREAM_KEEP_GENERATIONS` (default 50) in `hf/settings/base.py`, plus
      the `hf-prune-generations` scheduler job at 20:40 UTC.
- [x] If Option 4: verify the prune by watching it delete. Run it once against a
      count well above N and confirm exactly N remain, before it runs unattended
      against the real replica. **A task that deletes backups must be watched
      doing it**, not trusted because the code reads correctly — dry run first
      (820 generations found, 770 to delete, prefix confirmed against the real
      bucket), then the real run, then a recount: **exactly 50 live generations
      remain.** This step is also what caught the versioned-delete bug; the code
      read correctly and was wrong.
- [x] If Option 4: confirm the task never deletes the generation currently being
      written — the live container holds one open, and pruning by age-ordering
      must keep the newest unconditionally — two guards, both verified by
      removing them and watching the tests fail: `keep` protects the newest N,
      and `MINIMUM_AGE` refuses to delete anything under an hour old regardless
      of count. Removing the latter deletes 150 live generations in the test.
- [x] `terraform plan`, confirm the diff is only those changes, then apply
      (**needs Korey's approval**) — plan was `1 to add, 1 to change, 0 to
      destroy`: the schedule change and the new job, nothing else. Applied.
- [ ] **Deploy the Python half.** `core/replica.py`, the task, the settings and
      the `litestream.yml` comment are all in the image, so they need a push and
      a build — not just the apply that is already done. **This is urgent in a
      way the earlier ones were not:** `hf-prune-generations` now exists in Cloud
      Scheduler and fires at 20:40 UTC, but `/tasks/prune-generations/` does not
      exist in the deployed revision, so until the push lands it will 404 and
      alert — exactly the drift of `plans/scheduler-url-drift.md`, created
      deliberately this time and with a known expiry.
- [x] Verify the collision is gone: confirm from the scheduler logs that
      `check_deprecations` now fires at :15 and that no job shares a minute —
      confirmed from the deployed job list.
- [x] **Watch generation count fall.** Record it here at 48h and at 7 days. The
      fix is not verified until the count is stable at a few hundred rather than
      merely falling — overtaken by events: the manual run took it straight from
      820 to exactly 50. What still needs watching is that it *stays* bounded
      once the task runs unattended, which needs the deploy above.
- [ ] **Watch boot time recover.** Mean startup probe attempts should return
      toward 1–2. Record the figure here. This is the measurement that actually
      matters, and it is the one the previous plan did not think to take
- [ ] Re-measure GCS LIST operations once generations are stable, and correct the
      attribution in `plans/litestream-retention-cost.md` — the "1-second sync
      interval" claim there is wrong and is currently the record
- [ ] Update debt item 14 again: the 2026-09-05 fix traded a $31/month billing
      problem for a 40-second boot regression. Both entries in that item's
      history are now corrections of corrections, which is itself the lesson
- [ ] Record in `plans/operational-debt.md`: nothing measures cold-start
      duration, so a 3x regression ran for sixteen days and surfaced only as a
      side effect of unrelated 429 alerts

---

## Implementation notes, 2026-09-23

### The verification step earned its place, and cost something

The plan required watching the prune delete before trusting it. That step found
a bug the code review would not have: `blob.delete()` on a blob returned by
`list_blobs` is a **versioned** delete. The blob carries a `generation`, so the
API call destroys that exact version permanently instead of archiving it as a
noncurrent one.

It was caught by counting objects afterwards rather than by reading the code:

| | before | after |
|---|---|---|
| live objects | 1,660 | 105 |
| all versions | 2,228 | 543 |

Archiving deletes would have left `all versions` near 2,228. It dropped instead,
which is only possible if the versions were destroyed rather than archived.

**What that cost.** 1,686 objects across 770 generations, spanning roughly
2026-09-05 to 2026-09-21, are permanently gone rather than recoverable for 30
days as intended. The newest 50 generations — about 36–48 hours of restore
points — are intact, and the database itself is unaffected: it is ~120KB with one
player, eight purchases and no survey responses, and every one of those
generations was a snapshot of substantially the same tiny database. The realised
loss is point-in-time recovery into a two-week window during which almost nothing
changed.

It should still not have happened, and it happened because I asserted
recoverability in this document, then wrote code that quietly did not have it.

**Fixed** by deleting through `bucket.delete_blob(name)`. Four tests fail if it
reverts, including one whose fake blob raises on `.delete()` with an explanation.

### What this says about the method

Three times in this file's history a Litestream change has been reasoned
carefully and measured incompletely: the retention interval that cost $31/month,
the generation growth that tripled boot time, and now a delete that looked
correct and destroyed what it was supposed to preserve. The pattern each time is
the same — **the mechanism was understood and the observable was not checked.**

The step that caught it was not cleverness. It was counting the objects before
and after, which took one command.
