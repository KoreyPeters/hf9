# Blacklist Redesign Plan

Migrates from the threshold-based auto-blacklist built in Phase 13 to the permanent,
admin-initiated, three-condition system specified in `design.md`.

---

## What Was Built vs What the Design Requires

### Current implementation (Phase 13)

| Aspect | Current |
|---|---|
| Trigger | Automatic: `current_rating < 0.25` on every rating update |
| Exit | Automatic: `current_rating >= 0.50` lifts blacklist |
| Conditions | None — any candidate can be auto-blacklisted by a low rating |
| Admin involvement | None |
| History fields | `lifted_at`, `rating_at_lift` (exit path concept) |

### Design requirement

| Aspect | Design |
|---|---|
| Trigger | Admin action only, after community deliberation |
| Exit | **None — permanent, no exit path** |
| Condition 1 | Candidate must have endorsed HF (placed HF link on campaign website) |
| Condition 2 | Admin confirms the candidate won their election |
| Condition 3 | Post-election rating fell below **half of their pre-election high** and remained there for **90 consecutive days** |
| Documentation | Forum discussion URL recorded on blacklist record |

### Mismatches to fix

1. `BlacklistHistory.lifted_at` and `rating_at_lift` — must be removed (permanent = no exit)
2. `BLACKLIST_ENTRY` / `BLACKLIST_EXIT` auto-trigger logic — must be removed from the rating task
3. No endorsement gate (condition 1) — fields missing from `Candidate`
4. No election win confirmation (condition 2) — fields missing from `Candidate`
5. No sustained-window tracking (condition 3) — no `rating_below_threshold_since` field
6. No admin action — blacklisting has no human checkpoint
7. Three existing tests that assert the old lift behaviour — must be rewritten

---

## Condition 3 in Detail

The threshold is **not fixed**. It scales with the candidate's own pre-election peak:

```
blacklist_threshold = pre_election_rating_snapshot × BLACKLIST_RATIO
```

Where `BLACKLIST_RATIO` defaults to `0.50` (half). Examples from the design:

| Pre-election high | Blacklist threshold |
|---|---|
| 80% | below 40% |
| 60% | below 30% |
| 30% | below 15% |

A candidate who endorsed with strong community support is held to a proportionally higher
standard — because they set that standard themselves.

Both `BLACKLIST_RATIO` (default `0.50`) and `BLACKLIST_SUSTAINED_DAYS` (default `90`) are
configurable by HF administrators without a code deployment. They live in Django settings
(readable from environment variables) so no admin deploy is needed to adjust them.

---

## Data Model Changes

### `Candidate` — add six fields (one migration)

```python
# endorsement gate (condition 1)
is_endorsed = models.BooleanField(default=False)
endorsement_url = models.URLField(blank=True)
endorsement_verified_at = models.DateTimeField(null=True, blank=True)

# election win gate (condition 2)
election_win_confirmed = models.BooleanField(default=False)
pre_election_rating_snapshot = models.DecimalField(
    max_digits=5, decimal_places=2, null=True, blank=True
)

# sustained-window tracking (condition 3)
rating_below_threshold_since = models.DateTimeField(null=True, blank=True)
```

`pre_election_rating_snapshot` is set by the admin when they confirm the election win —
at that moment `current_rating` is captured. It is the baseline for the threshold formula.

`rating_below_threshold_since` is maintained by the rating task (see below). The admin
reads this field to see how long the candidate has been below their personal threshold.

### `BlacklistHistory` — remove two fields, add three (one migration)

**Remove:**
- `lifted_at` — exit path does not exist
- `rating_at_lift` — exit path does not exist

**Add:**
```python
blacklisted_by = models.ForeignKey(
    settings.AUTH_USER_MODEL,
    on_delete=models.SET_NULL,
    null=True,
    related_name="blacklist_actions",
)
forum_discussion_url = models.URLField(blank=True)
reason = models.TextField(blank=True)
```

`blacklisted_at` and `rating_at_blacklist` remain unchanged.

### Combined migration

Both model changes ship as one migration — they are logically coupled and both are
prerequisites for the admin action.

---

## Settings Changes (`hf/settings/base.py`)

Add two configurable values:

```python
BLACKLIST_RATIO = config("BLACKLIST_RATIO", default=0.50, cast=float)
BLACKLIST_SUSTAINED_DAYS = config("BLACKLIST_SUSTAINED_DAYS", default=90, cast=int)
```

These can be overridden in production via environment variables without a deploy.

---

## Rating Task Changes (`polium/task_views.py`)

Remove `BLACKLIST_ENTRY`, `BLACKLIST_EXIT`, and the entire blacklist branch. The task's
sole responsibility becomes:

1. Compute rating, return early if `None`
2. Save `current_rating`
3. If `is_endorsed` AND `election_win_confirmed` AND NOT `is_blacklisted` AND
   `pre_election_rating_snapshot` is set:
   - Compute `threshold = pre_election_rating_snapshot × settings.BLACKLIST_RATIO`
   - If `current_rating < threshold` and `rating_below_threshold_since` is `None` → set it to now
   - If `current_rating >= threshold` and `rating_below_threshold_since` is set → clear it to `None`

Blacklisting is never triggered here. The task only maintains the window-tracking field that
the admin uses to assess condition 3.

---

## Admin Changes (`polium/admin.py`)

### `CandidateAdmin`

New fields in `fieldsets`:

```
Endorsement: is_endorsed, endorsement_url, endorsement_verified_at
Election:    election_win_confirmed, pre_election_rating_snapshot
Blacklist:   is_blacklisted, blacklisted_at, rating_below_threshold_since
```

`is_endorsed`, `election_win_confirmed`, `pre_election_rating_snapshot` are editable.
`rating_below_threshold_since` and `blacklisted_at` are read-only.

**Custom admin action: `initiate_blacklisting`**

Added to `CandidateAdmin.actions`. When triggered on selected candidates:

1. Verifies `is_endorsed=True` (condition 1) — skips candidate, shows warning if not
2. Verifies `election_win_confirmed=True` (condition 2) — skips candidate, shows warning if not
3. Verifies `rating_below_threshold_since` is set and the duration ≥ `settings.BLACKLIST_SUSTAINED_DAYS` (condition 3) — skips candidate, shows warning if not
4. Verifies `is_blacklisted=False` — skips if already blacklisted
5. Opens an intermediate confirmation page that shows the candidate name, current rating,
   computed threshold, and days below threshold; collects `forum_discussion_url` and `reason`
6. On confirm: sets `is_blacklisted=True`, `blacklisted_at=now`, creates `BlacklistHistory`
   with `blacklisted_by=request.user`, `forum_discussion_url`, `reason`,
   `rating_at_blacklist=current_rating`; permanently revokes endorsement
   (`is_endorsed=False`, `endorsement_verified_at=None`)

The intermediate page is a standard Django admin view returning an `HttpResponse` with a
form — no third-party package required.

### `BlacklistHistoryAdmin`

Remove `lifted_at`, `rating_at_lift` from `list_display` and `readonly_fields`.
Add `blacklisted_by`, `forum_discussion_url`, `reason`.
Make all fields read-only (this is a permanent historical record).

---

## Test Changes (`polium/tests.py`)

### Remove (test wrong behaviour)

- `test_blacklist_lift_above_exit_threshold` — lift no longer exists
- `test_blacklist_not_lifted_between_thresholds` — lift no longer exists

### Rewrite

- `test_blacklist_entry_below_threshold` → `test_rating_task_does_not_blacklist` — confirm
  the task never sets `is_blacklisted=True` regardless of rating value

### Add

| Test | What it covers |
|---|---|
| `test_rating_task_sets_window_when_conditions_met` | endorsed + win confirmed + rating below threshold → `rating_below_threshold_since` set |
| `test_rating_task_clears_window_on_recovery` | rating recovers above threshold → field cleared |
| `test_rating_task_threshold_scales_with_snapshot` | threshold uses `pre_election_rating_snapshot × BLACKLIST_RATIO`, not a fixed value |
| `test_rating_task_ignores_window_without_endorsement` | not endorsed → window field never set |
| `test_rating_task_ignores_window_without_election_win` | endorsed but win not confirmed → window field never set |
| `test_rating_task_ignores_window_without_snapshot` | no `pre_election_rating_snapshot` → window field never set |
| `test_blacklist_history_has_no_lifted_at` | schema check — `BlacklistHistory` has no `lifted_at` attribute |

Admin action tests are out of scope — integration-heavy and best covered manually until a
dedicated admin test layer is built.

---

## What This Plan Does Not Include

- **Endorsement crawler** — fields added here but the 24–48hr crawl task is a separate
  feature (todo.md mismatch #2). `is_endorsed` is set manually by admin until the crawler exists.
- **0.25x points multiplier on vote declarations** — correct per design but vote declaration
  points are not yet implemented (todo.md: "Vote declaration points").
- **Profile page blacklist notice** — candidate profile page is a TODO stub.
- **All other todo.md items** — this plan is scoped to mismatch #1 only.

---

## File Summary

| File | Change |
|---|---|
| `polium/models.py` | Add 6 fields to `Candidate`; remove 2 fields, add 3 fields to `BlacklistHistory` |
| `polium/migrations/` | New migration for all model changes |
| `hf/settings/base.py` | Add `BLACKLIST_RATIO` and `BLACKLIST_SUSTAINED_DAYS` settings |
| `polium/task_views.py` | Remove auto-blacklist constants and logic; add scaled-threshold window tracking |
| `polium/admin.py` | Update `CandidateAdmin` fieldsets; add `initiate_blacklisting` action; update `BlacklistHistoryAdmin` |
| `polium/tests.py` | Remove 2 tests, rewrite 1, add 7 |

---

## Sequencing

1. ✅ **Settings** — add `BLACKLIST_RATIO` and `BLACKLIST_SUSTAINED_DAYS` to `base.py`
2. ✅ **Model changes + migration** — prerequisite for task and admin changes
3. ✅ **Task changes** — remove old logic, add scaled-threshold window tracking
4. ✅ **Admin changes** — fieldsets first (trivial), then the action with intermediate form
5. ✅ **Tests** — update alongside each section
