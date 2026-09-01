# `manage.py migrate` hangs on cold start, and the site 503s

## The short version

Since roughly 15:00 UTC on 2026-09-01, about half of all cold starts never
finish booting. `python manage.py migrate` — which `start.sh:10` runs on every
container start — begins and never returns. Cloud Run kills the instance after
the 80-second startup probe budget, returns 503, and starts another, which has
about a 50% chance of doing the same thing.

**I do not yet know why it hangs**, and the most important finding in this
document is *why I cannot tell you*: Python's stdout is block-buffered here, so
`migrate` prints nothing at all unless it exits. Every failed start is a blank
space in the logs. The first fix is to make the next occurrence readable.

The `retainer error` lines you pasted are a separate thing and are harmless.
They are explained at the bottom so they can be set aside.

## What is actually happening

Cold start timings, from `renaming database from temporary location` (Litestream
restore finishes) to the next thing that speaks:

| start | restore done | `migrate` output | uvicorn up | outcome |
|---|---|---|---|---|
| 18:24:54 | +0.0s | +6.2s | +11.6s | probe OK, 2 attempts |
| 16:05:39 | +0.0s | +9.5s | +16.4s | probe OK, 3 attempts |
| 15:12:48 | +0.0s | +11.0s | +15.0s | probe OK, 2 attempts |
| 15:05:05 | +0.0s | +15.4s | +19.6s | probe OK, 3 attempts |
| 18:10:21 | +0.0s | **never** | never | **probe failed after 66s** |
| 18:04:22 | +0.0s | **never** | never | **probe failed after 65s** |
| 18:02:50 | +0.0s | **never** | never | **probe failed after 65s** |
| 17:19:58 | +0.0s | **never** | never | **probe failed after 67s** |
| 17:10:31 | +0.0s | **never** | never | **probe failed after 61s** |

This is bimodal. `migrate` either completes in 6–15 seconds or it never completes
at all. There is no middle. **That shape is a block — a lock, or a network call
with no timeout — not a machine that is merely slow.** A slow container would
produce a spread of times and occasional late successes; there are none.

Thirteen failed starts today. One yesterday, six on 2026-08-14, zero on every
other day in the 30-day retention window. This is new.

### It is user-facing

Ten 503s today, each after an 85–95 second wait:

```
18:10:01  95.1s  /tasks/sweep-receipt-images/
17:19:39  86.3s  https://humanflourish.ing/     ← a person
17:10:07  85.4s  /tasks/sweep-receipt-images/
17:05:11  86.1s  /tasks/sweep-pending-receipts/
17:04:32  91.2s  https://humanflourish.ing/     ← a person
17:00:54  85.8s  /tasks/check-deprecations/
16:00:54  86.1s  /tasks/check-deprecations/
15:00:34  85.3s  /tasks/check-deprecations/
15:00:23  86.3s  /tasks/send-action-centre-emails/
07:00:31  94.6s  /tasks/check-deprecations/
```

Two of those are real homepage loads. Between 17:00 and 17:21 the site was
effectively down: five consecutive failed starts, no successful boot for twenty
minutes.

### One thing has already been lost

`hf-action-centre-emails` runs `0 15 * * 2` — weekly, Tuesday 15:00 UTC. Today is
Tuesday. It fired at 15:00, got a 503 at 15:01:58, and **never ran**.

`gcloud scheduler jobs describe hf-action-centre-emails` shows no `retryConfig`
at all, so the attempt count is the default of zero retries. A single 503 is the
whole of it. This week's Action Centre emails will not go out, and nothing will
notice until someone asks why.

That is a second bug wearing the first one's clothes: **the scheduled jobs have
no retry policy**, so any transient failure silently drops the work rather than
deferring it. `check-deprecations` runs hourly so it self-heals; a weekly job
does not.

## What I have ruled out

- **A code change.** The serving revision is `hf-app-00040-jfp`, created
  2026-08-08. No deploy today. `git status` shows commit 5f3eb6d is still local
  (`ahead 1`), so nothing of yesterday's work is in production.
- **OOM.** No memory-limit kills in the last two days. The `oom_kill` alert has
  not fired.
- **Database growth.** The Litestream snapshot is ~120KB, essentially unchanged
  from 31 August (120,430 → 120,725 bytes). Restore consistently takes under a
  second and is not where the time goes.
- **The startup probe being too tight.** Successful boots finish in 12–20
  seconds against an 80-second budget. There is four times the headroom needed.
  The probe is not the problem — see "what I am not doing".
- **Litestream.** In every failed start the restore completed normally and
  logged `renaming database from temporary location`. The hang is strictly after
  Litestream is done and before `migrate` produces output.

## Why I cannot see the cause, and the fix for that

`PYTHONUNBUFFERED` is set nowhere — not in the `Dockerfile`, not in
`terraform/cloud_run.tf`, not in the deployed environment (I checked all three).
Python therefore block-buffers stdout when it is a pipe, which it is under Cloud
Run.

The consequence: `Operations to perform:` and `No migrations to apply.` are not
printed when `migrate` reaches those points. They are printed when the **process
exits** and the buffer flushes. So a successful boot shows its output all at
once, and a hung boot shows nothing whatsoever — not because nothing happened,
but because nothing was flushed.

**Every failed start today is unreadable for this reason alone.** Two cheap
changes make the next one readable:

```dockerfile
ENV PYTHONUNBUFFERED=1
ENV PYTHONFAULTHANDLER=1
```

`PYTHONUNBUFFERED` gives line-by-line output, so we see how far `migrate` gets.
`PYTHONFAULTHANDLER` makes Python dump every thread's stack on a fatal signal,
which turns the next change into a precise answer:

```sh
timeout -s ABRT 60 python manage.py migrate --noinput
```

If `migrate` hangs, `timeout` sends SIGABRT at 60 seconds, and the fault handler
prints exactly which frame every thread is parked in — the import, the socket
read, the lock acquisition, whatever it is. One occurrence and we know.

This is the only part of this plan I am confident is right, because it does not
depend on knowing the cause.

## Hypotheses, and how to tell them apart

Ranked by how well they fit "bimodal, never completes, started today, no code
change". None is confirmed; the diagnostic above settles it in one occurrence.

1. **A network call with no timeout during `django.setup()`.** `spendium/apps.py`
   `ready()` imports `task_views`, which pulls in the extraction and adjudication
   modules and through them `google.genai` and grpc. `accounts/apps.py` imports
   signals and the Turnstile system check. If any of that reaches the metadata
   server or an auth endpoint at import time and that endpoint is intermittently
   unreachable, this is exactly the shape you would see. **Fits best.**
2. **Metadata server contention on credential fetch.** Related but distinct: ADC
   resolution hitting `169.254.169.254`. Would also explain the correlation with
   busier periods.
3. **SQLite lock on the restored file.** Fits "block" but not the rest —
   `/data` is per-instance, nothing else has the file open at that moment, and
   this would not have started today.
4. **CPU starvation during imports.** `startup-cpu-boost` is **not** enabled
   (confirmed against the deployed service), and the container has 1 vCPU with
   `cpu-throttling: true`. This would explain slowness, but *not* an unbounded
   hang — a starved import still finishes. Demoted for that reason, though
   enabling boost is worth doing regardless.

The load picture is consistent with a contention-triggered cause: 47 cold starts
today against 33–42 on recent days, and 118 redirect + 103 not-found responses
from the PHP vulnerability scanner that has been hammering the site.

## What I propose

**Tier 1 — do now, small, reversible, does not depend on knowing the cause.**

- `PYTHONUNBUFFERED=1` and `PYTHONFAULTHANDLER=1`, plus the `timeout -s ABRT`
  wrapper around `migrate` in `start.sh`. Makes the next hang self-explaining,
  and as a bonus converts an infinite hang into a fast crash-and-retry, which is
  strictly better than occupying the whole probe budget.
- `run.googleapis.com/startup-cpu-boost: 'true'`. Cheap, helps every cold start
  whether or not it is the cause, and Cloud Run bills the boost only during
  startup.
- Add `retryConfig` to the scheduler jobs — at minimum `retryCount: 3` with
  backoff. A weekly email should not be lost to one 503. This is worth doing on
  its own merits and would have saved today's Action Centre run.
- Re-run `hf-action-centre-emails` by hand once the service is stable, so this
  week's emails actually go out.

**Tier 2 — the decision that actually ends the outage.**

Set `min_instance_count = 1`. Cold starts leave the user-facing path entirely:
the hang can still happen on a replacement instance, but a warm instance keeps
serving while it retries, so a boot failure stops being a 503.

This partially reverses the 2026-07-26 decision recorded in debt item 6 and the
work in `plans/near-zero-hosting.md`, which established that **the entire
~$60/month bill was idle CPU** and drove it to near zero with `cpu_idle = true`.
An always-on instance reintroduces some of that — less than the old $60, because
`cpu-throttling` stays on and idle CPU is billed at the reduced rate, but not
nothing. **That is a real trade and it is yours to make**, which is why it is a
decision below rather than a step in the list.

My recommendation: do Tier 1 now, and take Tier 2 if the hang recurs after the
diagnostic lands, or immediately if you would rather not risk another twenty
minutes of downtime while we wait for evidence.

## What I am deliberately not doing

- **Not raising the startup probe budget.** The instinct is to give it more time,
  but the hang is unbounded — it never completes, so a larger `failureThreshold`
  would extend each outage rather than rescue it. The 80-second budget is the
  thing currently limiting the damage and forcing a retry. It should stay.
- **Not moving `migrate` out of the boot path.** It is the obvious idea — it runs
  47 times a day and says `No migrations to apply.` every time — but debt item 2
  already establishes why it cannot move: with SQLite on a per-container volume,
  a separate migrate job migrates a copy that is then discarded. Doing it through
  Litestream instead would mean a second writer against the same replica path,
  which is precisely the split-brain hazard found on 31 August. Migrate-at-boot
  is inherent to this architecture.
- **Not blocking the scanner.** It adds cold starts and is worth addressing, but
  it is not causing the hang — plenty of failed starts were triggered by
  scheduler jobs, not the scanner.
- **Not touching the concurrent-instance Litestream exposure or the 30-second
  cold-start latency.** Still unwritten, still separate.

## Decisions for you

1. **`min_instance_count = 1`, or wait for the diagnostic?** Recommendation
   above. It costs money against a bill that was deliberately driven to near
   zero, so it is a genuine trade rather than an obvious win.
2. **How aggressive should scheduler retries be?** `retryCount: 3` with the
   default backoff is my suggestion. Note that several of these tasks are
   idempotent by design (`sweep_purchase_anonymisation`, `sweep_pending_receipts`
   say so in their docstrings), but `send-action-centre-emails` records that it
   sent before sending, specifically so a retry cannot double-mail — so retries
   are safe there too.
3. **Should the deploy pipeline gain a startup smoke test?** `cloudbuild.yaml`
   curls `/` once, which passes on a warm instance. It would not have caught
   this. Out of scope here, but worth its own decision.

---

## Todo

- [x] Add `ENV PYTHONUNBUFFERED=1` and `ENV PYTHONFAULTHANDLER=1` to the
      `Dockerfile` — **written, not deployed.** Needs a push; see below.
- [x] Wrap the migrate step in `start.sh` as
      `timeout -s ABRT 60 python manage.py migrate --noinput`, with a comment
      saying why the timeout is there and what the ABRT is for — **written, not
      deployed.**
- [x] Verify locally that `PYTHONFAULTHANDLER` actually produces a thread dump on
      SIGABRT in this image — a diagnostic that has never been seen to work is
      not a diagnostic — **partially.** Docker Desktop was not running, so this
      could not be exercised in the real image. Verified instead under WSL Linux:

      ```
      $ PYTHONFAULTHANDLER=1 timeout -s ABRT 2 python3 -c "import time; time.sleep(60)"
      Fatal Python error: Aborted
      Current thread 0x000071ebdca63740 (most recent call first):
        File "<string>", line 1 in <module>
      exit code: 124
      ```

      That is the mechanism working end to end — signal delivered, stack named,
      non-zero exit for `set -e`. **Caveats, stated rather than glossed:** it ran
      on Python 3.8, and the image is 3.14; and `timeout`'s presence in
      `python:3.14-slim` is reasoned from coreutils being Essential in Debian
      rather than observed. Both are low risk and, more importantly, fail
      *loudly* — a missing `timeout` makes `start.sh` abort on the first boot and
      the `cloudbuild.yaml` smoke step fails before cutover, so traffic stays on
      the old revision. Worth re-checking against the image when Docker is up.
- [x] Enable `run.googleapis.com/startup-cpu-boost` in `terraform/cloud_run.tf`
- [x] Add `retryConfig` (`retryCount: 3`) to the scheduler jobs in
      `terraform/cloud_scheduler.tf` — all ten, 30s-to-300s backoff.
- [x] `terraform plan`, confirm the diff is only those changes, then apply
      (**needs Korey's approval**) — approved via "just do phase 1". Plan was
      `0 to add, 11 to change, 0 to destroy`: `startup_cpu_boost false -> true`
      plus ten `retry_config` additions, nothing else. Applied; created revision
      `hf-app-00042-rgv`, now serving 100%.
- [ ] Push so the Dockerfile and `start.sh` changes actually deploy — note these
      are the first changes here that require a *deploy*, not just an apply, so
      the two halves must both land (debt item 15). **Blocked: push is denied to
      me.** This is the half of Phase 1 that is written but not live.
- [x] Confirm the new revision serves, and that a cold start still boots normally
      — `hf-app-00042-rgv` at 100% traffic. Checked that Terraform's
      `ignore_changes` on the image did not roll back the deploy that landed at
      19:01: digest `sha256:ecff5401…` is identical on `00041` and `00042`.
      Three clean cold starts since 18:59 (restore → migrate in 6–8s → uvicorn →
      probe OK). **No failures since 18:11 — which is before any of this landed,
      so the hang has stopped recurring on its own, not been fixed.**
- [x] Re-run `hf-action-centre-emails` by hand so this week's emails go out, and
      confirm from the logs that it returned 200 and actually sent — returned
      **200 in 0.34s**. Whether it *sent* anything is **not verifiable**: the task
      calls `send_mail(..., fail_silently=True)` and logs nothing. Given
      production has one player, no survey criteria and no responses (item 16),
      it almost certainly had nothing to prompt about and sent nothing. The
      scheduled run is no longer missing; whether it had work to do is unknown.
- [ ] Watch for the next hung start and read the fault handler dump; record what
      it names in this document
- [ ] Only then decide the real fix, and write it up — the tiered changes above
      are mitigation and instrumentation, not a cure
- [ ] Decide on `min_instance_count = 1` (see Decisions #1)
- [x] Record in `plans/operational-debt.md`: scheduled jobs have no retry policy,
      so a transient failure silently drops the work; weekly jobs cannot
      self-heal the way hourly ones do — added as **item 17**, marked fixed, with
      the general lesson kept: frequency hid the fault, and the only job with a
      gap long enough to expose it was the one where a lost run costs something.
