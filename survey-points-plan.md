# Survey Points Plan

Implements mismatch #3 from `todo.md`: survey submission must award points — 100 for the
first survey of a subject, 50 for the second, 25 permanently thereafter.

---

## What Exists

| Component | State |
|---|---|
| `SurveyConfig.survey_points_first/second/subsequent` | ✅ Built — 100/50/25 defaults, admin-configurable |
| `award_points(player, amount, reason, source)` | ✅ Built — gated on `email_verified`, atomic |
| `submit_survey()` cooldown enforcement | ✅ Built — raises `CoolDownError` within window |
| `submit_survey()` one-per-subject replace | ✅ Built — reuses existing row on re-survey |
| Survey order tracking | ❌ Missing — no field records how many times a player has surveyed a subject |
| `award_points()` call in `submit_survey()` | ❌ Missing |

---

## The Core Problem

`submit_survey()` reuses the same `SurveyResponse` row when a player re-surveys — it
updates `submitted_at` and replaces answers in-place. This means
`SurveyResponse.objects.filter(player=player, ...).count()` always returns 0 or 1, not
the number of times the player has surveyed that subject.

To award the correct tier (first/second/subsequent), `submit_survey()` must know
the ordinal position of the current submission. The cleanest solution is a
`submit_count` integer on `SurveyResponse` that starts at 1 and increments on each
successful re-survey. This is the authoritative counter.

---

## What Changes

### §1 — `SurveyResponse.submit_count` field

Add to `surveys/models.py`:

```python
submit_count = models.PositiveIntegerField(default=1)
```

Semantics:
- New response: starts at `1` (first survey)
- On each successful re-survey: incremented by 1 before saving
- Never decremented — it is a permanent, monotonically increasing count

`default=1` means existing rows in the database are treated as first-surveys, which is
the conservative correct assumption for any rows that predate this migration.

One migration for this field.

### §2 — `submit_survey()` — increment count, award points

After the existing cooldown check and create-or-update logic, the service must:

1. **Determine the count** — `1` for new responses, `existing.submit_count + 1` for
   re-surveys. This value is set before saving.

2. **Award points** — call `award_points()` with the tier amount from `SurveyConfig`:

```python
from points.service import award_points

config = SurveyConfig.get()
if existing is not None:
    new_count = existing.submit_count + 1
    existing.submit_count = new_count
    existing.submitted_at = timezone.now()
    existing.save(update_fields=["submitted_at", "submit_count"])
    response = existing
else:
    new_count = 1
    response = SurveyResponse.objects.create(
        player=player,
        content_type=ct,
        object_id=subject.pk,
    )

CriterionAnswer.objects.bulk_create([...])

if new_count == 1:
    amount = config.survey_points_first
elif new_count == 2:
    amount = config.survey_points_second
else:
    amount = config.survey_points_subsequent

award_points(player, amount, "survey", source=response)
```

`award_points()` is already gated on `player.email_verified` — no additional guard
needed here. If the player is unverified, the call is a silent no-op. `submit_count`
still increments correctly regardless of verification status, so the point tier is
always accurate when points eventually do accrue.

The `source=response` argument links the `PointTransaction` to the `SurveyResponse`
via `GenericForeignKey`, making it auditable.

All of this happens inside the existing `@transaction.atomic` decorator on
`submit_survey()` — the point award and the response save are a single atomic unit.

### §3 — Tests

Add to `surveys/tests.py`:

| Test | What it covers |
|---|---|
| `test_first_survey_awards_100_points` | New survey on subject → `award_points` called with `survey_points_first` |
| `test_second_survey_awards_50_points` | Re-survey after cooldown expiry → `survey_points_second` |
| `test_third_survey_awards_25_points` | Third survey → `survey_points_subsequent` |
| `test_subsequent_surveys_award_25_points` | Fourth+ surveys still award `survey_points_subsequent` |
| `test_submit_count_increments_on_resubmit` | `submit_count` starts at 1, becomes 2 after re-survey |
| `test_points_not_awarded_to_unverified_player` | Unverified player → `PointTransaction` count stays 0 |
| `test_points_source_is_survey_response` | `PointTransaction.object_id == response.pk` |
| `test_cooldown_error_does_not_award_points` | `CoolDownError` → no transaction created |
| `test_config_values_used_for_points` | Custom `SurveyConfig` values (e.g. 200/75/30) are used |

Tests use `player` fixture from `conftest.py` (already `email_verified=True`).
The unverified test creates a player with `email_verified=False` directly.

---

## What Does Not Change

- `SurveyConfig` model — point values already there, already configurable
- `award_points()` in `points/service.py` — no changes needed
- `check_cooldown()` — no changes needed
- The existing nine tests in `surveys/tests.py` — all remain valid

---

## File Summary

| File | Change |
|---|---|
| `surveys/models.py` | Add `submit_count = PositiveIntegerField(default=1)` to `SurveyResponse` |
| `surveys/migrations/0003_surveyresponse_submit_count.py` | New migration |
| `surveys/service.py` | Increment `submit_count`, call `award_points()` with tier amount |
| `surveys/tests.py` | Add 9 tests for point award behaviour |

---

## Sequencing

1. ✅ **Model + migration** — add `submit_count`
2. ✅ **Service** — increment count, call `award_points()`
3. ✅ **Tests** — verify all tiers, unverified player, source linkage, config values
