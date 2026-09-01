# The rating snapshot job has been 404ing since 9 August

## What is broken

Cloud Scheduler fires `hf-snapshot-product-ratings` at 05:00 UTC daily against
`https://humanflourish.ing/tasks/snapshot-product-ratings/`. That path stopped
existing on 8 August. Every run since has returned 404.

```
jobName: hf-snapshot-product-ratings
url:     https://humanflourish.ing/tasks/snapshot-product-ratings/
status:  NOT_FOUND
debugInfo: URL_ERROR-ERROR_NOT_FOUND. Original HTTP response code number = 404
```

First failure `2026-08-09T05:00:52Z` — the first scheduled run after revision
`hf-app-00040-jfp` went live at `2026-08-08T16:32Z`. Most recent
`2026-08-31T05:00:42Z`. Twenty-three consecutive runs, every one a 404. Log
retention is 30 days so the whole span is still visible; it does not extend
further back, because the job worked before that deploy.

The rename is in `hf/task_urls.py:64-68`, from commit 9e8a2d5 *Added store
ratings*:

```diff
-        "snapshot-product-ratings/",
-        spendium_task_views.snapshot_product_ratings,
-        name="task_snapshot_product_ratings",
+        "snapshot-ratings/",
+        spendium_task_views.snapshot_ratings,
+        name="task_snapshot_ratings",
```

Nothing else drifted. Every other enabled scheduler job's URI resolves to a
registered path in `hf/task_urls.py`; this is the only mismatch.

## The root cause is not what it looks like

The obvious reading is "the code was renamed and the scheduler job was
forgotten." That is wrong, and the correct version matters because it changes
what the fix and the guard have to be.

Commit 9e8a2d5 **did** update the scheduler job. `terraform/cloud_scheduler.tf:194`
already says:

```hcl
resource "google_cloud_scheduler_job" "snapshot_ratings" {
  name = "hf-snapshot-ratings"
  ...
    uri = "https://humanflourish.ing/tasks/snapshot-ratings/"
```

Code and infrastructure were changed together, correctly, in one commit. What
never happened is `terraform apply`. The state file still holds the old resource:

```
$ terraform state list | grep scheduler
...
google_cloud_scheduler_job.snapshot_product_ratings   ← state
```

against `snapshot_ratings` in configuration. So the deployed job is still the
July one.

**The asymmetry is the bug.** `cloudbuild.yaml` runs on every push to `main` —
build → push → migrate → deploy → smoke → cutover — so application code ships
itself. There is no `terraform` step in that pipeline and no `.github/workflows`
directory at all. Infrastructure ships only when a human remembers to run
`terraform apply` from a laptop. On 8 August the code half of a two-part change
deployed automatically and the infrastructure half sat in the repo.

This is a sibling of debt item 10 — *"nothing notices when production is not
running the code we think it is"* — except the drift is in Cloud Scheduler
rather than the image, and the repo is the half that is *correct*.

The `smoke` step (`cloudbuild.yaml:63`) curls `$_APP_URL/` and nothing else, so
it could not have caught this and would not catch the next one.

## What it cost — CORRECTED DURING IMPLEMENTATION

**The section below is wrong, and is left standing so the correction is legible.**

I checked production rather than reasoning about it, by pulling the current
Litestream snapshot out of GCS and querying it read-only (the copy was deleted
immediately afterwards). Production is pre-launch:

| table | rows |
|---|---|
| `spendium_product` | 93 |
| `spendium_store` | 8 |
| `spendium_purchaselineitem` | 70 |
| `accounts_player` | 1 |
| **`surveys_criterion`** | **0** |
| **`surveys_surveyresponse`** | **0** |
| `surveys_criterionanswer` | 0 |
| `spendium_productratingsnapshot` | 0 |
| `spendium_storeratingsnapshot` | 0 |

There are no survey criteria, so no survey can be answered; there are no
responses, so `compute()` returns `score is None` for every product
(`ratings.py:92` — `score = ... if total_weight else None`), and both
`snapshot_all()` and `snapshot_all_stores()` skip every subject.

**So the correct number of rows for the job to write is zero, and always has
been.** `ProductRatingSnapshot` has never held a row in production. The 23 days
of 404s destroyed nothing, because there was nothing to record on any of them.

Three claims below are therefore wrong:

- *"23 days missing from an existing trend line"* — there is no existing trend
  line. The gap is 23 days of zero rows.
- The backfill question is **moot**. There is nothing to reconstruct, and
  Decision 1 does not need answering.
- My verbal claim during investigation that `top_rated()` had been serving a
  stale 9 August listing was also wrong. `top_rated()` reads
  `Max(taken_on)`, gets `None`, and returns `[]` — the home page has been
  correctly showing nothing this whole time.

**The fix was still worth making.** The job was genuinely broken and would have
silently recorded nothing on the first day real responses arrived — which is
exactly when the data becomes unreconstructible. It was a live trap that had not
yet been stepped on. But the honest characterisation is *a latent fault found
before it cost anything*, not *23 days of lost data*.

One adjacent finding, same root cause: `surveys_criterion` is empty, so the
`seed_spendium_criteria` and `seed_store_criteria` management commands — the
latter shipped in commit 9e8a2d5, the same commit as this bug — have never been
run in production. Shipped code, unapplied setup, discovered by accident. Logged
to the debt register.

### Original section follows, uncorrected.

## What it cost

`spendium/task_views.py:102` is one task covering both subjects:

```python
@task("snapshot-ratings")
def snapshot_ratings() -> None:
    ratings.snapshot_all()
    ratings.snapshot_all_stores()
```

So the 404 took out both:

- **`ProductRatingSnapshot`** — 23 days missing from an existing trend line.
- **`StoreRatingSnapshot`** — has *never run in production*. The model arrived in
  migration `0020_storeratingsnapshot`, in the same commit that broke the URL.
  Store trend lines have no history at all, not a gap in one.

Ratings themselves are unaffected. `compute()` and `compute_store()` run on
demand from live responses; nothing a player sees today is wrong. What is lost is
only the record of what those ratings *were* on each of those days.

### The docstring's claim is true in general and not true yet

`spendium/ratings.py:342` says a rating "recomputed from a rolling window cannot
be reconstructed after the fact — responses age out of it." Correct in steady
state. But `spendium/ratings.py:32` sets `RATING_WINDOW_DAYS = 365`, and the
window's lower edge is currently before any data exists. Nothing has aged out.

So the 23 days *are* reconstructible — but only by teaching the rating engine to
compute as of a past date, which means threading an `as_of` parameter through
`_responses()` (`ratings.py:96`), `purchase_count()` (`ratings.py:106`),
`compute()`, `compute_store()` and `store_points_per_dollar()`. Note that
`purchase_count()` has no date filter at all today; it counts every
`PurchaseLineItem` and `AnonymisedLineItem` outright, so an as-of mode is a new
capability there rather than a tightened cutoff.

**My recommendation: don't.** That is a modification to the most
correctness-sensitive code in Spendium, carrying real risk of changing live
ratings, to recover three weeks of chart history on a product with few users. The
trade is bad. But it is a product call about your own data, not mine, so it is
listed as a decision below.

**What I will not do under any circumstances** is the cheap version — stamping
today's computed rating onto the 23 missing dates via `update_or_create`. That
fabricates measurements, and it would draw a confident flat line asserting
ratings that were never taken. `snapshot_all_stores()` already articulates the
principle in its own docstring: skip rather than write a value that "would read
as *rated badly* rather than *not rated*." Inventing history is the same error
with more conviction.

## The fix

`terraform apply`. The configuration is already right; it has just never been
executed.

I ran `terraform plan -lock=false` against real state. It is clean:

```
Plan: 1 to add, 0 to change, 1 to destroy.

  - google_cloud_scheduler_job.snapshot_product_ratings will be destroyed
      - name = "hf-snapshot-product-ratings"
      - uri  = ".../tasks/snapshot-product-ratings/"

  + google_cloud_scheduler_job.snapshot_ratings will be created
      + name = "hf-snapshot-ratings"
      + uri  = ".../tasks/snapshot-ratings/"
```

Nothing else is pending. That is worth stating plainly, because the real hazard
in "just run apply" is usually the *other* drift that goes out with it, and here
there is none. Twenty-three days of not applying Terraform produced exactly one
divergence.

Destroy-then-create is correct here rather than something to avoid. Both the
Terraform address and the GCP resource `name` changed, and `name` forces
replacement. A `moved` block would be the right tool only if the address changed
while the GCP name stayed — not the case. A scheduler job holds no state worth
preserving; the gap between destroy and create is irrelevant for something that
next fires at 05:00 UTC.

After applying, the first real run is 05:00 UTC the following morning. I would
rather not wait for that to find out whether it works, so the plan triggers the
job by hand and reads the result.

## The guard

A test asserting that every URI in `cloud_scheduler.tf` resolves against
`hf/task_urls.py` is the intuitive answer. It is worth having — it is nearly free
and it catches the next half-done rename — but **it would have passed on 8 August
and caught nothing here**, because both files were already consistent. The
divergence was between the repo and the world, and no test that reads only the
repo can see it.

Catching this class of failure means watching deployed reality. The cheapest
thing that would actually have worked is an alert on scheduler job failures.

`terraform/monitoring.tf` already establishes the pattern: `google_logging_metric.oom_kills`
(line 44) counts a log line, and `google_monitoring_alert_policy.oom_kill` (line
61) alerts on the metric. A scheduler failure metric is the same shape:

```hcl
resource "google_logging_metric" "scheduler_failures" {
  project = var.project
  name    = "hf-scheduler-failures"
  filter = join(" AND ", [
    "resource.type=\"cloud_scheduler_job\"",
    "severity>=ERROR",
  ])

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}
```

Verified against real logs: this filter matches both the daily 404s and the
`check-deprecations` 503 on 31 August, and matches nothing else. It is not
job-specific on purpose — any of the ten scheduled jobs failing for any reason
is worth an email.

Note the existing 5xx policy could never have caught this: a 404 is not a 5xx.
Cloud Scheduler's own failure log is a separate signal with no policy on it at
all, which is exactly the hole.

**A log-based metric does not backfill.** It counts entries written after the
metric exists. The alert starts working from apply time, and the 23 days of
history stay invisible to it.

### One honest caveat about the alert

Debt item 13 is still open on a single unanswered question: has a Cloud
Monitoring alert email *ever actually arrived*? Adding a fourth policy to a
notification channel nobody has confirmed works buys less than it appears to.

There is now a fresh test case for that question, which is why it is in the todo
list. `hf-app` returned a 503 on `/tasks/check-deprecations/` at
`2026-08-31T17:00:44Z`. The `http_5xx` policy alerts on any 5xx in a 60-second
window with `duration = "0s"` and `trigger.count = 1`, so that request should
have produced an email. Whether one landed in `me@koreypeters.org` on 31 August
is a thirty-second check in your inbox, and it settles item 13 either way.

## What I am deliberately not doing

- **Not adding Terraform to `cloudbuild.yaml`.** Auto-applying infrastructure on
  push is a much larger decision than this bug justifies — it needs a plan step,
  an approval gate, and a service account with far broader permissions than the
  build has today. The asymmetry is worth recording as debt and deciding
  separately.
- **Not renaming the code back to `snapshot-product-ratings`.** The new name is
  better; the task genuinely covers stores as well as products now.
- **Not touching the 30-second cold starts or the concurrent-instance Litestream
  exposure.** Separate problems from the same investigation, out of scope here.
- **Not running `terraform apply` myself** without you saying so.

## Decisions for you

1. **Backfill the 23 days, or accept the gap?** My recommendation is accept it,
   for the reasons above. If you want the backfill, it is a separate plan — it
   touches the rating engine and deserves its own review rather than being
   smuggled into a one-line infrastructure fix.
2. **Add the scheduler-failure alert now, or resolve debt item 13 first?** The
   alert is cheap and independently useful; my inclination is to add it now and
   treat the channel question as the separate thing it already is.
3. **`terraform apply` needs your say-so.** The plan is clean and I have shown it
   in full above, but I will not run it unprompted.

---

## Todo

- [x] Confirm `terraform plan` is still `1 to add, 0 to change, 1 to destroy`
      immediately before applying, in case anything drifted since this was written
      — re-checked 2026-08-31, unchanged. Saved to a plan file with `-out` so the
      apply executes exactly what was confirmed rather than re-deciding.
- [x] Run `terraform apply` — replaces `hf-snapshot-product-ratings` with
      `hf-snapshot-ratings` (**needs Korey's approval**) — approved and applied
      2026-08-31: `1 added, 0 changed, 1 destroyed`, no other resources touched.
- [x] Verify the new job exists and points at `/tasks/snapshot-ratings/`
      (`gcloud scheduler jobs describe hf-snapshot-ratings --location=us-central1`)
      — `ENABLED`, `0 5 * * *` UTC, POST, OIDC as `hf-tasks@`.
- [x] Confirm the old job is gone from `gcloud scheduler jobs list` — gone; the
      only remaining snapshot jobs are `hf-snapshot-ratings` and `hf-snapshot-metrics`.
- [x] Trigger the job by hand rather than waiting for 05:00 UTC
      (`gcloud scheduler jobs run hf-snapshot-ratings --location=us-central1`)
      — ran 2026-08-31T23:51:57Z.
- [x] Confirm the run returned 200 in the scheduler execution log, not 404 —
      `POST /tasks/snapshot-ratings/ HTTP/1.1" 200 OK`, scheduler logged
      `severity=INFO status=200`. The 404 is gone.
- [x] Confirm rows actually landed — **expectation was wrong; zero is correct.**
      No Litestream WAL segment followed the task, which meant it wrote nothing.
      Rather than accept a 200 as proof, I pulled the production database and
      counted: 0 survey criteria, 0 responses, so every subject is skipped by
      design. See the corrected section above. The task is working; there is
      simply nothing to snapshot until surveys are seeded and answered.
- [x] Add `google_logging_metric.scheduler_failures` to `terraform/monitoring.tf`
      — added and applied; metric `hf-scheduler-failures` exists.
- [x] Add `google_monitoring_alert_policy.scheduler_failure`, following the
      `oom_kill` policy's shape, wired to the existing `errors` notification
      channel — created and enabled. **The first apply failed** with
      `Error 404: Cannot find metric(s) that match type = ...hf-scheduler-failures`;
      a new log-based metric takes minutes to become visible to Monitoring and
      Terraform's graph cannot know that. Retried after five minutes and it
      succeeded. Documented in a comment above the resource so the next person
      applying from scratch does not think the config is broken.
- [x] Verify the guard by watching it fire: point a throwaway scheduler job at a
      path that does not exist, let it run, confirm the metric increments and the
      alert opens, then delete the job. An alert that has never fired has not
      been shown to work — **verified up to the notification channel.** Created
      `hf-alert-test-delete-me` targeting `/tasks/this-path-does-not-exist/`, ran
      it, and observed the whole chain:
      1. Cloud Scheduler logged `severity=ERROR`, `status=NOT_FOUND` — the exact
         shape of the real 23-day failure.
      2. The metric recorded `int64Value: 1` for the 00:05–00:06Z window, carrying
         `resource.labels.job_id = hf-alert-test-delete-me`, confirming the
         `group_by_fields` grouping produces one incident per job.
      3. The policy's condition is `> 0`, `duration 0s`, 60s alignment, trigger
         count 1, so that data point satisfies it.

      4. **The alert email arrived.** Confirmed by Korey. Cloud Monitoring does
         not write incidents to Cloud Logging, so this last hop was the one thing
         I could not observe myself; his inbox is the only place it is visible.

      Job deleted afterwards; `gcloud scheduler jobs list` is back to the ten
      real jobs.

      So this guard is verified **end to end** — induced failure, ERROR log,
      metric increment, condition satisfied, incident opened, email delivered —
      rather than reasoned about. That is the whole point of the step: the
      previous alert on this channel went twenty days before anyone knew whether
      it worked (debt item 13), and the answer then turned out to be that the
      policy had not existed at all.
- [x] Add a test cross-referencing every `uri` in `terraform/cloud_scheduler.tf`
      against `hf/task_urls.py` urlpatterns — cheap, and catches the next
      half-done rename even though it would not have caught this one — added as
      `core/test_scheduler_targets.py`, following the `terraform/secrets.tf`
      cross-check already in `core/test_prod_settings.py:63`. Carries a second
      test guarding against the parse silently returning an empty set, which
      would pass against anything.
- [x] Verify that test fails by watching it fail: revert the `cloud_scheduler.tf`
      URI to `snapshot-product-ratings/` in the working tree, confirm the test
      goes red, restore it — went red with
      `AssertionError: scheduler jobs point at paths not registered in
      hf/task_urls.py: ['snapshot-product-ratings/']`, then restored;
      `git diff` on that file is clean.
- [x] Record in `plans/operational-debt.md`: application code deploys
      automatically on push while infrastructure requires a manual
      `terraform apply`, so a commit that changes both ships half of itself.
      Reference this incident as the worked example — added as **item 15**, with
      three options for what to do about it left as Korey's decision.
- [x] Answer debt item 13's open question: did an alert email arrive for the
      `2026-08-31T17:00:44Z` 503 on `/tasks/check-deprecations/`? Update item 13
      with the answer either way — **yes.** Korey confirmed the alert emails
      arrive, and one of them is what started this investigation. Item 13 updated
      and dropped to the bottom of the list; the structural point is kept because
      it is still true.
- [x] Decide on the backfill (see Decisions #1); if yes, write it as its own plan
      — **moot.** There is nothing to backfill: both snapshot tables have always
      been empty because production has no survey responses. See the correction
      above.
- [x] Also recorded as **item 16**: production has never been seeded with survey
      criteria, so every rating-dependent path returns empty and looks healthy
      doing so — which is part of why this bug survived 23 days.
