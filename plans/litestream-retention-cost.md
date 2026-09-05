# The Litestream retention fix costs $24/month

> **Re-measured 2026-09-05, at implementation: it is now $31/month, not $24.**
> LISTs rose from 117,697 to **150,798/day** ($0.81 → **$1.03 CAD/day**) because
> generations grew from 223 to 252 in the intervening days. The cost scales with
> generation count even though generation count is not what to *fix* — more
> generations means more objects for each check to walk. The analysis below is
> unchanged and its numbers are left as originally measured.

Debt item 14 was fixed on 2026-08-08 by setting `retention-check-interval: 5m`.
That change traded a 2 MiB storage problem for a **$24 CAD/month operations
problem**. This undoes it properly.

My change, my error, and the error is not the setting — it is that I optimised a
resource that was never costing anything and never priced the fix.

---

## 1. The measurement

GCS operations, three days, whole project:

| Operations/day | Bucket | Method |
|---|---|---|
| **117,697** | `hf-litestream-*` | **ListObjects** |
| 74 | `hf-litestream-*` | ReadObject |
| 74 | `hf-litestream-*` | WriteObject |
| 64 | `hf-litestream-*` | DeleteObject |

Everything else in the project is single digits. LIST outnumbers every other
operation **1,600 to 1**.

At the Class A rate of $0.05 per 10,000: **$0.59 USD ≈ $0.81 CAD per day**,
which matches the blue band on the billing chart. So this is confirmed, not
inferred.

**$24 CAD/month. $295 CAD/year. To prune 2 MiB of snapshots.**

### Where the LISTs come from

Two sources, and the split decides the whole design:

| Source | LISTs/day | Share |
|---|---|---|
| Retention checks (156/day × ~754) | 117,624 | **99.9%** |
| Container starts (30/day × 2.4) | 73 | 0.1% |

A `litestream restore` costs **2.4 LIST calls** — it enumerates generations with
one paginated call, not one call per generation. The retention check does the
opposite: it walks every generation individually, so its cost is
`generations × checks`, and at 223 generations and a 5-minute interval that is
754 calls every five minutes the container happens to be awake.

**This matters more than it looks:** it means the number of generations is
almost irrelevant to cost, and the *check frequency* is everything. Shortening
the retention window would barely help. Stopping the frequent checks fixes it
entirely.

## 2. What I got wrong

Debt item 14 said generations were accumulating unpruned and offered two
remedies — a Litestream retention policy, or a GCS lifecycle rule. I chose
Litestream, reasoning that "it keeps the decision next to the thing making the
objects."

That reasoning was fine. Three things around it were not:

- **The problem was 2 MiB.** Storage was never a cost. I fixed something that
  was not costing anything.
- **I never priced the fix.** A five-minute interval against a growing list of
  generations is a quadratic-ish cost and I did not think about the operations
  bill at all.
- **I had already measured that operations dominate.** The `cpu_idle`
  investigation a week earlier was entirely about billed *operations* rather
  than resources. I did not carry the lesson across.

The register entry for item 14 also drew a general lesson — *a periodic task
whose interval exceeds the process lifetime never runs at all* — and the fix I
chose was to make the interval far shorter than the process lifetime. Correct
for reliability, expensive for exactly the same reason.

## 3. What is already there

Found while planning, and it changes the shape of the fix.

**`terraform/storage.tf:1-20` already has a lifecycle rule** on the Litestream
bucket, deleting at `age = 30`. So GCS-side pruning is not something to add — it
exists, and it has been running the whole time. Litestream's retention at 168h
simply prunes more aggressively, so the lifecycle rule never gets to act on
anything current.

**Versioning is enabled on the bucket.** So every Litestream delete makes the
object noncurrent rather than removing it. Live: 489 objects, 24.5 MiB. All
versions: **2,098 objects, 70.4 MiB.** Objects Litestream deleted on 2 August
are still present as noncurrent versions, and will be until the 30-day rule
reaches them.

That is not a cost problem at this size, but it means the bucket holds roughly
three times what it appears to, and nothing in the config says so.

## 4. The fix

**Stop the frequent retention checks; let the existing lifecycle rule prune.**

```yaml
# litestream.yml
retention: 168h
retention-check-interval: 24h    # was 5m
```

A container session now averages about 26 minutes, so a 24-hour check
effectively never fires. That is the intended behaviour rather than an accident:
**GCS becomes the pruner, and Litestream's retention becomes a backstop** that
acts only in the unusual case of a long-lived container.

Expected: 117,697 → about 73 LIST operations/day. **$0.81 → under $0.01 CAD/day.**

Deleting the retention settings entirely would leave the defaults — 24h
retention, 1h check — which is worse: the check would still fire on any session
longer than an hour, and a 24h retention window is thinner than the lifecycle
rule provides. Setting them explicitly is what makes the intent legible.

### Why not shorten the retention window instead

Because generation count is 0.1% of the cost. Cutting 168h to 24h would remove
about 7 generations' worth of LISTs per check and leave the other 99% intact.

### Why not a shorter lifecycle rule

The existing 30-day rule should stay generous, and this is the one genuine risk
in handing pruning to GCS:

**A lifecycle rule runs whether or not the app does.** Litestream's retention is
self-limiting — it only prunes while the container is alive, so an idle app
cannot delete its own backup. A lifecycle rule has no such limit. With a 7-day
rule and an app idle for eight days, GCS would delete **the entire replica**, and
`start.sh` runs `litestream restore -if-replica-exists`, which would then start
from an empty database rather than fail.

At 30 days that is not a plausible scenario. It becomes one the moment somebody
tunes the rule down to "save space", which is why the reasoning belongs in the
terraform comment rather than only here.

## 5. What I am deliberately not doing

- **Removing versioning.** It is holding 46 MiB of noncurrent objects for up to
  30 days and costing nothing meaningful. It is also a second line of defence
  against exactly the deletion accident described above. Worth documenting
  rather than changing.
- **Adding a noncurrent-version lifecycle rule.** Same reason. Revisit if the
  bucket ever grows enough to matter.
- **Reducing the number of generations.** One per container start is inherent to
  Litestream 0.3 on ephemeral containers, and it turns out not to be the cost.
- **Upgrading Litestream.** 0.5 changes the on-disk format entirely (LTX,
  compaction levels). Possibly better here, and far too large a change to make
  while chasing a billing line.

## 6. Risks

- **Pruning now depends on a rule nobody watches.** If the lifecycle rule were
  ever removed, generations would accumulate indefinitely with nothing
  reporting it. Mitigated by the terraform comment and by the fact that storage
  growth is slow and cheap; not mitigated by any alarm.
- **The cost is invisible until billed.** Nothing alerts on operation counts.
  The $50 budget would catch a repeat eventually, at 50% of a monthly budget —
  which is roughly a month of noticing nothing.

---

## Todo

- [x] `litestream.yml`: `retention-check-interval: 5m` → `24h`, with a comment
      giving the measured numbers and saying GCS is the pruner — done, including
      a note that deleting the two keys outright is *worse* than either value,
      since the defaults are 24h retention with an hourly check.
- [x] Comment `terraform/storage.tf`'s lifecycle rule to say it is now the
      primary pruning mechanism, and why 30 days must stay generous — an idle
      app plus a short rule deletes the replica
- [x] Note the versioning interaction in the same comment: deletes become
      noncurrent versions, so the bucket holds ~3× its apparent size
- [x] Update debt item 14 with the correction and the measurement — rewritten as
      a correction with the 2026-09-04 figures, keeping the original text below
      it. Three lessons recorded, the sharpest being that item 14's own closing
      rule of thumb is what produced this fault: it warned that an interval
      longer than the process lifetime never runs, so the remedy chosen was an
      interval far shorter than it.
- [ ] After deploy, re-measure GCS operations and record the actual figure here.
      Expect ~73/day. **The fix is not verified until that number is in this
      document** — this is the second time a Litestream retention change has had
      an unmeasured consequence.

      **Blocked, and blocked twice over.** `litestream.yml` is baked into the
      image by `COPY . .`, so this needs a build and deploy — which needs a push,
      and push is denied to me. Then the figure only means anything after a full
      day on the new revision, since the measurement is operations *per day*.

      Re-measure with:

      ```
      curl -s -H "Authorization: Bearer $(gcloud auth print-access-token)" \
        "https://monitoring.googleapis.com/v3/projects/human-flourishing-4/timeSeries\
      ?filter=metric.type%3D%22storage.googleapis.com%2Fapi%2Frequest_count%22\
      &interval.startTime=<DAY>T00:00:00Z&interval.endTime=<DAY+1>T00:00:00Z\
      &aggregation.alignmentPeriod=86400s&aggregation.perSeriesAligner=ALIGN_SUM"
      ```

      Expect `ListObjects` on `hf-litestream-*` to fall from ~150,798 to double
      digits. If it does not, the check is still firing and the interval is not
      the whole story.

**Decisions for Korey**

- [ ] Is a 7-day recovery window still right, given GCS is now doing the pruning
      at 30 days and Litestream's 168h only applies to long-lived containers? The
      effective window becomes 30 days, which is more generous than intended and
      costs nothing.
- [ ] Worth a budget alert on GCS operations specifically? The current budget
      would take about a month to notice a repeat of this.
