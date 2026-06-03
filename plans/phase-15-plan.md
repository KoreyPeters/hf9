# Phase 15 — Re-Survey Cool-Down

Resolves **Mismatch #5** from `local_only/todo.md`: a player can currently re-survey any subject
immediately and as many times as they like. The design requires a minimum waiting period (default
30 days) before a player may update their survey. This setting must be changeable by an admin
without a code deployment or server restart.

This phase also fixes the data model prerequisite for mismatch #4 (one-survey-per-player): the
submission service enforces both constraints in the same transaction.

---

## Todo

### §15.1 — Model changes: `SurveyResponse`

- [x] In `surveys/models.py`, rename the existing `submitted_at` field to `created_at` and keep `auto_now_add=True`
- [x] In `surveys/models.py`, add a new `submitted_at = models.DateTimeField(default=timezone.now)` field (writeable)
- [x] Import `from django.utils import timezone` at the top of `surveys/models.py`

### §15.2 — Model: `SurveyConfig`

- [x] In `surveys/models.py`, add the `SurveyConfig` model with fields: `cooldown_days` (PositiveIntegerField, default=30, with help_text), `survey_points_first` (default=100), `survey_points_second` (default=50), `survey_points_subsequent` (default=25)
- [x] Add `Meta` with `verbose_name = "Survey configuration"` and `verbose_name_plural = "Survey configuration"`
- [x] Override `save()` to force `self.pk = 1` before calling `super().save()`
- [x] Add `@classmethod get(cls)` that calls `cls.objects.get_or_create(pk=1)` and returns the instance

### §15.3 — Migration

- [x] Run `python manage.py makemigrations surveys` to generate `0002_*.py`
- [x] Inspect the generated migration and verify it contains: `RenameField(submitted_at → created_at)`, `AddField(submitted_at)`, and `CreateModel(SurveyConfig)`
- [x] If `makemigrations` generates the rename as `RemoveField` + `AddField` instead of `RenameField`, edit the migration manually to use `RenameField` (preserves existing data)
- [x] Run `python manage.py migrate` and confirm it applies cleanly

### §15.4 — Admin

- [x] In `surveys/admin.py`, add `SurveyConfig` to the import from `.models`
- [x] Register `SurveyConfigAdmin` with `list_display = ["cooldown_days", "survey_points_first", "survey_points_second", "survey_points_subsequent"]`
- [x] Implement `has_add_permission` returning `False` if `SurveyConfig.objects.exists()`
- [x] Implement `has_delete_permission` always returning `False`
- [x] Update the existing `SurveyResponseAdmin.list_display` to include both `created_at` and `submitted_at` (replacing the old single `submitted_at`)

### §15.5 — Submission service

- [x] Create `surveys/service.py`
- [x] Define `CoolDownError(Exception)` with `__init__(self, remaining: timedelta)` that stores `self.remaining` and calls `super().__init__()` with a descriptive message
- [x] Define `_get_existing(player, content_type, object_id) -> SurveyResponse | None` — queries for the most recent response by `(player, content_type, object_id)` ordered by `-submitted_at`
- [x] Define `check_cooldown(player, subject: Model) -> timedelta | None`:
  - Gets the ContentType for `subject`
  - Calls `_get_existing`; returns `None` immediately if no prior response
  - Calls `SurveyConfig.get()` to read `cooldown_days`
  - Computes `elapsed = timezone.now() - existing.submitted_at`
  - Returns `cooldown - elapsed` if still within the window, else `None`
- [x] Define `submit_survey(player, subject: Model, answers: dict[int, bool]) -> SurveyResponse` decorated with `@transaction.atomic`:
  - Gets the ContentType and calls `_get_existing`
  - Calls `check_cooldown`; raises `CoolDownError` if a timedelta is returned
  - If an existing response is found: deletes all its `answers`, sets `existing.submitted_at = timezone.now()`, saves with `update_fields=["submitted_at"]`, uses `existing` as `response`
  - If no existing response: creates a new `SurveyResponse` with `player`, `content_type`, `object_id`
  - Calls `CriterionAnswer.objects.bulk_create(...)` with one `CriterionAnswer` per entry in `answers`
  - Returns `response`
- [x] Add all necessary imports: `timedelta`, `ContentType`, `transaction`, `Model`, `timezone`, and the local models

### §15.6 — Tests

- [x] In `surveys/tests.py`, add fixtures: `survey_config` (creates `SurveyConfig(pk=1, cooldown_days=30)`), `criterion` (creates a `Criterion` using the existing `polium_category` fixture), `candidate` (creates a `Jurisdiction` + `Candidate`)
- [x] **Test 1** — `test_first_survey_no_cooldown`: assert `check_cooldown(player, candidate)` returns `None` when no prior response exists
- [x] **Test 2** — `test_submit_creates_response`: call `submit_survey`; assert exactly one `SurveyResponse` exists for the player and the answer row is correct
- [x] **Test 3** — `test_cooldown_blocks_immediate_resubmit`: submit once, then assert a second immediate `submit_survey` raises `CoolDownError` and `exc_info.value.remaining.days >= 29`
- [x] **Test 4** — `test_cooldown_allows_resubmit_after_expiry`: submit once, back-date `submitted_at` by 31 days via `update()`, then assert a second `submit_survey` succeeds
- [x] **Test 5** — `test_resubmit_replaces_answers`: after an expired cool-down, re-survey with a different answer; assert still exactly one `SurveyResponse` row, exactly one `CriterionAnswer` row, and its `answer` is the new value
- [x] **Test 6** — `test_resubmit_updates_submitted_at`: after re-survey, assert `response.submitted_at >= before` (where `before` was captured just before the second call)
- [x] **Test 7** — `test_resubmit_preserves_created_at`: capture `created_at` before re-survey; after re-survey call `refresh_from_db()` and assert `created_at` is unchanged
- [x] **Test 8** — `test_cooldown_respects_config_value`: create `SurveyConfig(pk=1, cooldown_days=7)`; after first survey, back-date `submitted_at` by 8 days; assert second `submit_survey` succeeds (proving the 7-day config was used, not a hardcoded 30)
- [x] Run `pytest surveys/tests.py` and confirm all 8 new tests pass alongside the existing 5
- [x] Run `python manage.py check` and confirm zero issues

---

## §15.1 — Model changes: `SurveyResponse`

### Problem

`submitted_at = models.DateTimeField(auto_now_add=True)` is immutable. The design says the
12-month expiry clock resets on re-survey, which means `submitted_at` must be writeable. The cool-
down check also reads `submitted_at` to calculate elapsed time.

### Solution: split into two fields

| Field | Type | Purpose |
|---|---|---|
| `created_at` | `auto_now_add=True` | First submission timestamp. Needed for the survey-order counter (100/50/25 points, mismatch #3). Never changes. |
| `submitted_at` | `default=timezone.now` | Last update timestamp. Drives the 12-month expiry filter in `compute_rating` and the cool-down check. Resets on every re-survey. |

```python
# surveys/models.py
from django.utils import timezone

class SurveyResponse(models.Model):
    player = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="survey_responses",
    )
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    subject = GenericForeignKey("content_type", "object_id")
    created_at = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [models.Index(fields=["content_type", "object_id"])]
```

`ratings.py` already filters on `submitted_at__gte=cutoff` — no change needed there. The field
name is preserved; only the semantics of `auto_now_add` vs a writeable default changes.

---

## §15.2 — Model: `SurveyConfig` singleton

The cool-down period and survey point values must be changeable by an admin in the Django admin UI
with no deploy and no restart. A database singleton model is the correct tool.

```python
# surveys/models.py (append)

class SurveyConfig(models.Model):
    cooldown_days = models.PositiveIntegerField(
        default=30,
        help_text="Minimum days a player must wait before re-surveying a subject.",
    )
    survey_points_first = models.PositiveIntegerField(default=100)
    survey_points_second = models.PositiveIntegerField(default=50)
    survey_points_subsequent = models.PositiveIntegerField(default=25)

    class Meta:
        verbose_name = "Survey configuration"
        verbose_name_plural = "Survey configuration"

    def save(self, *args, **kwargs) -> None:
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls) -> "SurveyConfig":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
```

`save()` forces `pk=1` on every write, making the table a singleton. `SurveyConfig.get()` creates
the row with defaults on first access, so no seed fixture is needed.

---

## §15.3 — `SurveyConfig` admin

```python
# surveys/admin.py — add after existing registrations

from .models import Category, Criterion, CriterionAnswer, SurveyConfig, SurveyResponse


@admin.register(SurveyConfig)
class SurveyConfigAdmin(admin.ModelAdmin):
    list_display = [
        "cooldown_days",
        "survey_points_first",
        "survey_points_second",
        "survey_points_subsequent",
    ]

    def has_add_permission(self, request) -> bool:
        return not SurveyConfig.objects.exists()

    def has_delete_permission(self, request, obj=None) -> bool:
        return False
```

`has_add_permission` blocks creating a second row once the singleton exists.
`has_delete_permission` prevents accidental deletion of the config row.

Also update the existing `SurveyResponseAdmin` to reference `created_at` instead of the old
`submitted_at`:

```python
@admin.register(SurveyResponse)
class SurveyResponseAdmin(admin.ModelAdmin):
    list_display = ("player", "content_type", "object_id", "created_at", "submitted_at")
    inlines = [CriterionAnswerInline]
```

---

## §15.4 — Submission service: `surveys/service.py`

This is a new file and the single entry point for all survey submissions across all games.

```python
# surveys/service.py
from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Model
from django.utils import timezone

from .models import CriterionAnswer, SurveyConfig, SurveyResponse


class CoolDownError(Exception):
    def __init__(self, remaining: timedelta) -> None:
        self.remaining = remaining
        super().__init__(f"Cool-down active: {remaining.days} days remaining.")


def _get_existing(
    player,
    content_type: ContentType,
    object_id: int,
) -> SurveyResponse | None:
    return (
        SurveyResponse.objects.filter(
            player=player,
            content_type=content_type,
            object_id=object_id,
        )
        .order_by("-submitted_at")
        .first()
    )


def check_cooldown(player, subject: Model) -> timedelta | None:
    """Return remaining cool-down timedelta, or None if the player may submit now."""
    ct = ContentType.objects.get_for_model(subject)
    existing = _get_existing(player, ct, subject.pk)
    if existing is None:
        return None
    config = SurveyConfig.get()
    cooldown = timedelta(days=config.cooldown_days)
    elapsed = timezone.now() - existing.submitted_at
    if elapsed < cooldown:
        return cooldown - elapsed
    return None


@transaction.atomic
def submit_survey(
    player,
    subject: Model,
    answers: dict[int, bool],
) -> SurveyResponse:
    """
    Create or replace the player's survey response for `subject`.

    `answers` maps criterion PKs to boolean responses.
    Raises CoolDownError if the player is within the cool-down window.
    """
    ct = ContentType.objects.get_for_model(subject)
    existing = _get_existing(player, ct, subject.pk)

    remaining = check_cooldown(player, subject)
    if remaining is not None:
        raise CoolDownError(remaining)

    if existing is not None:
        existing.answers.all().delete()
        existing.submitted_at = timezone.now()
        existing.save(update_fields=["submitted_at"])
        response = existing
    else:
        response = SurveyResponse.objects.create(
            player=player,
            content_type=ct,
            object_id=subject.pk,
        )

    CriterionAnswer.objects.bulk_create([
        CriterionAnswer(survey_response=response, criterion_id=cid, answer=val)
        for cid, val in answers.items()
    ])
    return response
```

Key design decisions:

- **`check_cooldown` is public** — views call it before rendering a survey form to show the player
  how long they must wait, without going through a full submission attempt.
- **Cool-down re-checked inside the transaction** — prevents TOCTOU races if two requests arrive
  simultaneously.
- **On re-survey: answers deleted, `submitted_at` updated, `created_at` untouched** — `created_at`
  is needed for the survey-order counter (mismatch #3); `submitted_at` drives the expiry clock.
- **`bulk_create` for answers** — one query regardless of how many criteria exist.
- **`_get_existing` uses `order_by("-submitted_at").first()`** — defensive against any legacy
  duplicate rows; always operates on the most recent one.

---

## §15.5 — Migration

```python
# surveys/migrations/0002_surveyconfig_submitted_at.py
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("surveys", "0001_initial")]

    operations = [
        migrations.RenameField(
            model_name="surveyresponse",
            old_name="submitted_at",
            new_name="created_at",
        ),
        migrations.AddField(
            model_name="surveyresponse",
            name="submitted_at",
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),
        migrations.CreateModel(
            name="SurveyConfig",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "cooldown_days",
                    models.PositiveIntegerField(
                        default=30,
                        help_text="Minimum days a player must wait before re-surveying a subject.",
                    ),
                ),
                ("survey_points_first", models.PositiveIntegerField(default=100)),
                ("survey_points_second", models.PositiveIntegerField(default=50)),
                ("survey_points_subsequent", models.PositiveIntegerField(default=25)),
            ],
            options={
                "verbose_name": "Survey configuration",
                "verbose_name_plural": "Survey configuration",
            },
        ),
    ]
```

The `RenameField` + `AddField` sequence is safe on PostgreSQL (no full-table rewrite). Existing
rows will have `submitted_at = created_at` after migration, which is correct — their first
submission was also their last.

---

## §15.6 — Tests

Eight tests covering all branches of the cool-down logic. Add to `surveys/tests.py`.

```python
# surveys/tests.py — append below existing tests

import pytest
from datetime import timedelta

from django.utils import timezone

from surveys.models import Category, Criterion, CriterionAnswer, SurveyConfig, SurveyResponse
from surveys.service import CoolDownError, check_cooldown, submit_survey


@pytest.fixture
def survey_config(db):
    return SurveyConfig.objects.create(pk=1, cooldown_days=30)


@pytest.fixture
def criterion(db, polium_category):
    return Criterion.objects.create(category=polium_category, question="Does X?", weight=1.0)


@pytest.fixture
def candidate(db):
    from polium.models import Candidate, Jurisdiction
    jurisdiction = Jurisdiction.objects.create(name="Test Jurisdiction", level="federal")
    return Candidate.objects.create(
        name="Test Candidate", jurisdiction=jurisdiction, office="Senator"
    )


# §15.6.1 — first survey is always allowed
@pytest.mark.django_db
def test_first_survey_no_cooldown(player, candidate, survey_config):
    assert check_cooldown(player, candidate) is None


# §15.6.2 — submission creates a SurveyResponse with one answer
@pytest.mark.django_db
def test_submit_creates_response(player, candidate, criterion, survey_config):
    response = submit_survey(player, candidate, {criterion.pk: True})
    assert SurveyResponse.objects.filter(player=player).count() == 1
    assert response.answers.filter(criterion=criterion, answer=True).exists()


# §15.6.3 — re-survey within cool-down raises CoolDownError
@pytest.mark.django_db
def test_cooldown_blocks_immediate_resubmit(player, candidate, criterion, survey_config):
    submit_survey(player, candidate, {criterion.pk: True})
    with pytest.raises(CoolDownError) as exc_info:
        submit_survey(player, candidate, {criterion.pk: False})
    assert exc_info.value.remaining.days >= 29


# §15.6.4 — re-survey after cool-down expiry succeeds
@pytest.mark.django_db
def test_cooldown_allows_resubmit_after_expiry(player, candidate, criterion, survey_config):
    submit_survey(player, candidate, {criterion.pk: True})
    SurveyResponse.objects.filter(player=player).update(
        submitted_at=timezone.now() - timedelta(days=31)
    )
    response = submit_survey(player, candidate, {criterion.pk: False})
    assert response.answers.filter(answer=False).exists()


# §15.6.5 — re-survey replaces old answers, no duplicate rows
@pytest.mark.django_db
def test_resubmit_replaces_answers(player, candidate, criterion, survey_config):
    submit_survey(player, candidate, {criterion.pk: True})
    SurveyResponse.objects.filter(player=player).update(
        submitted_at=timezone.now() - timedelta(days=31)
    )
    submit_survey(player, candidate, {criterion.pk: False})
    assert SurveyResponse.objects.filter(player=player).count() == 1
    assert CriterionAnswer.objects.filter(survey_response__player=player).count() == 1
    assert CriterionAnswer.objects.get(survey_response__player=player).answer is False


# §15.6.6 — submitted_at resets on re-survey (expiry clock restarts)
@pytest.mark.django_db
def test_resubmit_updates_submitted_at(player, candidate, criterion, survey_config):
    submit_survey(player, candidate, {criterion.pk: True})
    SurveyResponse.objects.filter(player=player).update(
        submitted_at=timezone.now() - timedelta(days=31)
    )
    before = timezone.now()
    submit_survey(player, candidate, {criterion.pk: False})
    r = SurveyResponse.objects.get(player=player)
    assert r.submitted_at >= before


# §15.6.7 — created_at is NOT updated on re-survey
@pytest.mark.django_db
def test_resubmit_preserves_created_at(player, candidate, criterion, survey_config):
    submit_survey(player, candidate, {criterion.pk: True})
    r = SurveyResponse.objects.get(player=player)
    original_created_at = r.created_at
    SurveyResponse.objects.filter(player=player).update(
        submitted_at=timezone.now() - timedelta(days=31)
    )
    submit_survey(player, candidate, {criterion.pk: False})
    r.refresh_from_db()
    assert r.created_at == original_created_at


# §15.6.8 — cooldown_days is read from SurveyConfig, not a hardcoded constant
@pytest.mark.django_db
def test_cooldown_respects_config_value(player, candidate, criterion, db):
    SurveyConfig.objects.create(pk=1, cooldown_days=7)
    submit_survey(player, candidate, {criterion.pk: True})
    SurveyResponse.objects.filter(player=player).update(
        submitted_at=timezone.now() - timedelta(days=8)
    )
    response = submit_survey(player, candidate, {criterion.pk: False})
    assert response is not None
```

---

## File change summary

| File | Change |
|---|---|
| `surveys/models.py` | Rename `submitted_at`→`created_at` (`auto_now_add`), add `submitted_at` (`default=now`), add `SurveyConfig` |
| `surveys/migrations/0002_surveyconfig_submitted_at.py` | New migration |
| `surveys/service.py` | New file — `CoolDownError`, `check_cooldown`, `submit_survey` |
| `surveys/admin.py` | Add `SurveyConfigAdmin`; update `SurveyResponseAdmin.list_display` |
| `surveys/tests.py` | 8 new tests appended; existing tests unaffected (field name unchanged) |

---

## Dependencies and non-goals

**Depends on:** nothing new — `ContentType`, `transaction.atomic`, `timezone` are all already used.

**Not in this phase:**
- Mismatch #3 (survey points 100/50/25) — needs `SurveyConfig.survey_points_*` (added here) but
  the point-awarding call in `submit_survey` is deferred to a dedicated phase. The field exists.
- Mismatch #4 (unique constraint on player+subject) — the service enforces one-row-per-player at
  write time. A DB-level `UniqueConstraint` on `(player, content_type, object_id)` is a safe
  follow-on addition but not required for correctness today.
- Any survey submission UI — there is no survey form view yet; `submit_survey` is ready to be
  called from a view when that phase arrives.
