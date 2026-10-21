# Cutting the cold start from 30s to about 9s

You were right to be suspicious of the seven seconds. It is not Django migrations
being slow — it is **Python compiling 1,114 modules to bytecode, from scratch, on
every single container start**, because the image ships `.py` files with no `.pyc`
alongside them.

Two changes, one line each, no new moving parts:

| | change | 30s becomes |
|---|---|---|
| A | `initial_delay_seconds: 30 → 0` on the startup probe | ~16s |
| B | `UV_COMPILE_BYTECODE=1` in the Dockerfile | ~9s |

Everything else in this document is either measurement backing those two, or
things I looked at and decided were not worth it.

---

## 1. Where the 30 seconds actually go

A measured boot, 2026-09-23 22:51:

| elapsed | event |
|---|---|
| T+0.0 | instance start |
| T+3.8 | `restoring snapshot` — container start plus generation enumeration |
| T+3.9 | `renaming database` — the restore itself, 0.12s |
| **T+11.5** | `Operations to perform:` — **7.6s, the thing you asked about** |
| T+11.6 | `No migrations to apply` |
| T+13.8 | litestream replicate — second Python start, 2.2s |
| T+15.6 | `Uvicorn running` — third Python start, 1.8s |
| **T+30.0** | probe's first attempt, succeeds immediately |

Two distinct wastes. The 14.4 seconds at the end is a timer. The 7.6 seconds in
the middle is compilation.

## 2. The 7.6 seconds is bytecode compilation, not Django

`Dockerfile:10` runs `uv sync --no-dev`. That installs `.py` files and **does not
compile them**. So the first Python process in a fresh container compiles
everything it imports — 1,114 modules for `django.setup()` alone — and writes
`.pyc` files that only survive until the container dies.

Measured locally, by pointing `PYTHONPYCACHEPREFIX` at an empty directory to
force a cold cache without disturbing the real one:

| bytecode cache | `django.setup()` + `MigrationLoader` |
|---|---|
| warm | **0.99s** |
| cold | **8.38s** |

8.4x, and the cold figure lands within a few hundred milliseconds of the 7.6s
seen in production. That is the answer.

The breakdown with a warm cache shows migrations are not the problem at all:

```
import django       0.03s
django.setup()      0.80s
import loader       0.02s
MigrationLoader     0.15s   ← all 41 migration files
TOTAL               0.99s
```

`migrate` itself costs about 0.15s. **It only looked like a slow migration
because it is the first command to run, so it pays for everything.** That also
explains the shape of the rest of the boot: the second Python start costs 2.2s and
the third 1.8s, because both read the `.pyc` files the first one wrote.

### Fix: `UV_COMPILE_BYTECODE=1`

`uv sync --compile-bytecode` (or the env var, which `uv sync --help` confirms is
`UV_COMPILE_BYTECODE`) moves that work to build time. Costs a slower image build;
saves it on every cold start for the life of the revision.

**Honest about the expected size.** Production should go from 7.6s to somewhere
around 1.5–3s, not to the 0.99s measured locally: a fresh container still has to
*read* those `.pyc` files from a cold filesystem, where my local venv was already
in the OS page cache. I could not pin it exactly because Docker Desktop is not
running here, so the container-level before/after is a todo rather than a
finding. The direction is not in doubt; the last significant figure is.

## 3. The 14.4 seconds is a timer nobody set on purpose

The application is serving-ready at T+15.6. Cloud Run does not look until T+30,
because `initial_delay_seconds = 30` tells it not to. The metric agrees — 78 of
the last 79 cold starts sit in the histogram bucket beginning at exactly 30.0s,
which is a floor rather than a measurement.

It was never a decision: it arrived in commit eb3490d with the rest of the
scaffolding and no plan in `plans/` discusses it.

What it does contribute is part of the **total startup budget**, which must be
preserved:

```
budget = initial_delay + (failure_threshold × period_seconds)
now:      30 + (10 × 5) = 80s
proposed:  0 + (16 × 5) = 80s
```

Identical 80 seconds before Cloud Run gives up — and that matters, because the
budget was nearly exhausted on 2026-09-23 with a boot at 9 attempts of 10. The
only thing that changes is how soon success is noticed. A TCP probe against a
port nothing is listening on is refused immediately, so early attempts cost a log
line and nothing else.

## 4. What the two changes should add up to

| phase | now | after |
|---|---|---|
| container start + restore | 3.9s | 3.9s |
| first Python start (migrate) | 7.6s | ~2s |
| second Python start (ensure_superuser) | 2.2s | ~1.5s |
| third Python start (uvicorn) | 1.8s | ~1.5s |
| probe delay | 14.4s | ~0s |
| **total** | **30.0s** | **~9s** |

Roughly a 3x improvement from two one-line changes.

## 5. Considered and rejected

- **Combining `migrate` and `ensure_superuser` into one command.** Worth about
  1.5s once bytecode is precompiled, at the cost of a new management command and
  a boot path that no longer reads as two obvious steps. Revisit if the numbers
  after A and B disappoint; not worth it up front.
- **Lazy-importing heavy libraries.** Checked with `python -X importtime`:
  `genai` and `grpc` do not load during `django.setup()` at all, and PIL costs
  about 10ms. There is nothing here, and that is worth recording so nobody else
  goes looking.
- **Skipping `migrate` when nothing has changed.** A cheap stdlib-`sqlite3`
  check could tell whether the newest migration in the image is already applied,
  skipping Django entirely. It looks attractive and **saves nothing**: whichever
  process starts Python first pays the compilation cost, so skipping migrate just
  moves those seconds onto uvicorn. Once B lands there is only ~0.15s of actual
  migration work to save, against a real risk of booting un-migrated.
- **`period_seconds: 5 → 2`.** Three more seconds for a less conventional probe
  config. Revisit only if A underdelivers.
- **`min_instance_count = 1`.** Removes cold starts from the user path entirely
  and remains the standing Tier 2 decision in
  `plans/startup-hang-and-503s.md`. It costs money; these two changes cost
  nothing and are worth doing first regardless of how that lands.
- **Upgrading Litestream, trimming the image, changing base images.** All
  plausible, none measured, and the two changes above account for 21 of the 30
  seconds. Not while there is low-hanging fruit.

## 6. Monitoring, kept deliberately small

You said the alert matters less than the reduction, so this is one resource
rather than the project the previous draft implied.

`run.googleapis.com/container/startup_latencies` already exists — DELTA,
DISTRIBUTION, in ms, per revision — and has recorded this service all along. It
captured the September regression and its fix without being asked:

| date | cold starts | mean |
|---|---|---|
| 2026-09-12 | 31 | 39.1s |
| 2026-09-22 | 35 | **61.3s** |
| 2026-09-24 | 35 | 30.9s |
| 2026-09-26 | 17 | 30.0s |

So this needs an alert policy and nothing else. **Debt item 18 proposed a
log-based metric on probe attempts; that was the wrong instinct** — the platform
already measures the real duration.

**One trap, found the hard way.** Querying this metric with `ALIGN_PERCENTILE_50`
over a multi-day window returned p50 = 68.1s against a mean of 30.0s — impossible.
Reconstructing the histogram from raw exponential buckets gave the truth:

```
n=79   weighted mean = 30.4s
  >= 30.0s   n=78   cum= 98%
  >= 58.4s   n= 1   cum=100%
```

The aligner was the artefact. So the todo list makes "check the aligner against
known data" a step, not an assumption.

Threshold: **45s**. Above the expected ~9s baseline by a wide margin, below the
80s budget at which this becomes an outage, and — checked against the table — it
would have fired on 2026-09-14, eight days before the crisis.

## Decisions for you

1. **Do A and B together, or A first?** A is Terraform-only and reversible in a
   minute; B needs a deploy. Doing both at once means one measurement covering
   two changes, which is how you end up unable to attribute a regression. My
   preference is **A, measure, then B, measure** — it costs one extra day and
   makes both numbers real. Say if you would rather have it all at once.
2. **Alert now or after?** Adding it first means it is watching while the boot
   path changes, which is the more useful order.

---

## Todo

- [x] Set `initial_delay_seconds = 0` and `failure_threshold = 16` in
      `terraform/cloud_run.tf`, with a comment recording the budget arithmetic and
      that the 80s total is deliberately unchanged
- [x] `terraform plan`, confirm the diff is only that, then apply
      (**needs Korey's approval**) — plan was `1 to add, 1 to change, 0 to
      destroy`: the two probe fields and the alert policy, nothing else. Applied,
      creating revision `hf-app-00046-khg`.
- [x] **Measure A before doing B.** Startup latency should fall 30.0s → ~16s
      within a day. Record the actual figure here — **19.6s on the first cold
      start**, from the logs:

      ```
      17:04:56.189  Starting new instance
      17:05:15.800  Uvicorn running
      17:05:15.800  STARTUP TCP probe succeeded after 4 attempts
      ```

      **30.0s → 19.6s, a 10.4s saving.** The important detail is the second and
      third lines sharing a timestamp: the probe now confirms readiness the
      instant the port opens, where before it waited until T+30 regardless. The
      floor is gone.

      Two caveats, stated rather than rounded away. This was a
      `DEPLOYMENT_ROLLOUT` start, which pulls the image fresh and so is slower
      than a routine `AUTOSCALING` one — the steady-state figure should be
      better. And it is one boot; the metric had not ingested data for the new
      revision yet. Confirm over a day with:

      ```
      ...&filter=...resource.label.revision_name="hf-app-00046-khg"
      &aggregation.alignmentPeriod=3600s
      &aggregation.perSeriesAligner=ALIGN_DELTA
      &aggregation.crossSeriesReducer=REDUCE_MEAN
      ```

      **Attribution survives shipping B**, because the metric is per revision:
      `00046` is A alone, the next revision is A+B. The daily aggregate will
      blend them; the per-revision series will not.
- [ ] Confirm startup probe failures did not increase — the budget is unchanged on
      paper, so any increase means the arithmetic above is wrong. Nothing so far,
      but one boot is not evidence; check again after a day of traffic.
- [x] Add `ENV UV_COMPILE_BYTECODE=1` to the `Dockerfile` above the `uv sync`
      line, with a comment giving the 0.99s vs 8.38s measurement — placed above
      the sync deliberately, since it is read during it and moving it after would
      leave it inert while still reading as correct. **Needs a push to deploy.**
- [x] Confirm the image actually contains `.pyc` files after the change — the
      whole saving rests on it, and `uv sync` silently ignoring the setting would
      look identical to it working until the latency was measured — **confirmed
      2026-10-21, and it was only half true.** Built from a clean `git archive`:
      Django 899/899 compiled, but the app's own modules **0/198**, migrations
      0/48. The project has no `[build-system]`, so uv never installs `/app`.
      Closed by a `compileall` step; details in `plans/migrate-slow-tail.md`.
- [x] Note the image build time before and after; if it grows a lot, that is a
      real cost on every deploy and belongs in the record — the `compileall`
      step takes 2.7s locally. The `uv sync` step took 12.5s with bytecode on;
      its time without it was not captured (quiet build). Image grew 557MB →
      626MB. Negligible either way.
- [ ] Push, deploy, and **measure B**: the first Python start should fall from
      7.6s to roughly 2s, and total startup latency to roughly 9s. Record both
- [ ] If the container-level figure differs much from the local 8.4x, say so here
      and work out why rather than rounding it into agreement
- [x] Add `google_monitoring_alert_policy.cold_start_latency` on
      `startup_latencies` at 45s, wired to the existing `errors` channel —
      created and enabled.
- [x] **Verify the alert's aligner against known data**: query it over
      2026-09-22 (mean 61.3s) and 2026-09-25 (mean 30.0s) and confirm it returns
      those figures. `ALIGN_PERCENTILE_50` over a long window did not — done
      before writing the policy, and it caught a second mistake on the way:
      `ALIGN_DELTA` + `REDUCE_MEAN` returns a **DOUBLE**, not a distribution, so
      the first attempt read the wrong field and reported "no data". With the
      right field:

      | day | known mean | aligner returns |
      |---|---|---|
      | 2026-09-22 | 61.3s | **61.3s** (n=35, min 48.0, max 72.0) |
      | 2026-09-25 | 30.0s | **30.0s** (n=27, min 30.0, max 30.0) |

      Exact on both. The second row is also the cleanest possible confirmation of
      the probe floor: min and max identical to a tenth of a second across 27
      cold starts.
- [x] Update debt item 18: closed by a built-in metric, and record why the
      log-based proxy it proposed was the wrong instinct — done. The blind spot
      was never instrumentation; it was not checking for an existing metric before
      deciding to build one.
- [x] Update `plans/cold-start-monitoring-and-optimisation.md` to point here —
      it was the monitoring-first draft of this plan and should not be read as
      the live one — marked SUPERSEDED at the top.
