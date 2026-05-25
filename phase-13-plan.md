# Phase 13 Todo — Testing Strategy

Phase 13 adds a pytest suite covering the seven key risk areas identified in plan.md. No new Django models, views, or migrations are introduced. All test files replace the empty `tests.py` stubs that `startapp` generated.

---

#### 13.1 — pytest configuration (pyproject.toml)

`pyproject.toml` has `pytest` and `pytest-django` in dev dependencies but no `[tool.pytest.ini_options]` section. Without it, every `pytest` invocation requires the `--ds` flag manually.

- [x] Add `[tool.pytest.ini_options]` to `pyproject.toml`:
  ```toml
  [tool.pytest.ini_options]
  DJANGO_SETTINGS_MODULE = "hf.settings.dev"
  python_files = ["tests.py", "test_*.py"]
  ```
  This makes `uv run pytest` work without any flags.

---

#### 13.2 — conftest.py (project root)

plan.md shows a `conftest.py` with two fixtures. The `player` fixture is straightforward. The `mature_player` fixture in plan.md only sets `date_joined` — but `account_is_mature` requires **both** age AND survey response count (`MATURITY_SURVEY_COUNT = 3`), so the fixture must also create three `SurveyResponse` rows.

- [x] Create `conftest.py` at the project root
- [x] Add imports:
  - `import pytest`
  - `from datetime import timedelta`
  - `from django.contrib.contenttypes.models import ContentType`
  - `from django.utils import timezone`
  - `from accounts.models import Player`
  - `from surveys.models import SurveyResponse`
- [x] Write `player(db)` fixture:
  - `return Player.objects.create_user(username="testplayer", password="pass")`
  - `Player` extends `AbstractUser`, so `create_user` is available directly — no separate `User` object needed
- [x] Write `mature_player(db, player)` fixture:
  - Set `date_joined` to 8 days ago: `Player.objects.filter(pk=player.pk).update(date_joined=timezone.now() - timedelta(days=8))`
  - Create three `SurveyResponse` rows (using the player itself as the subject via GenericForeignKey — semantically arbitrary, but valid for counting):
    ```python
    ct = ContentType.objects.get_for_model(Player)
    for _ in range(3):
        SurveyResponse.objects.create(player=player, content_type=ct, object_id=player.pk)
    ```
  - Call `player.refresh_from_db()` and return `player`

---

#### 13.3 — Rating calculator tests (surveys/tests.py)

Tests for `surveys.ratings.compute_rating`. Replace the empty stub.

- [x] Replace `surveys/tests.py` with tests for `compute_rating`
- [x] Import: `pytest`, `Decimal`, `timedelta`, `timezone`, `ContentType`, `compute_rating`, `Category`, `Criterion`, `SurveyResponse`, `CriterionAnswer`, `Player`
- [x] Write a shared helper (or fixture) that creates a `Category`, a `Criterion`, a `Candidate`-like subject, and returns a function for submitting answers — reduces boilerplate across tests
- [x] `test_returns_none_with_no_responses` — call `compute_rating(subject)` with no SurveyResponse rows; assert result is `None`
- [x] `test_correct_weighted_average` — create one response with two answers (one criterion weight=2.0 True, one weight=1.0 False); assert result ≈ `2.0 / 3.0`
- [x] `test_excludes_responses_older_than_365_days` — create a response with `submitted_at` set to 366 days ago via `.update()`; assert `compute_rating` returns `None` (the old response is excluded)
- [x] `test_excludes_inactive_criteria` — create a response with an answer whose criterion has `is_active=False`; assert result is `None` (no active criteria contribute weight)
- [x] `test_returns_none_when_total_weight_is_zero` — all active criteria have `weight=0`; assert result is `None`

---

#### 13.4 — Blacklist engine tests (polium/tests.py)

Tests for the blacklist entry/exit logic inside `update_candidate_rating`. Use `unittest.mock.patch` to control what `compute_rating` returns — this isolates the blacklist branching logic from the rating calculation logic (tested separately in §13.3).

- [x] Replace `polium/tests.py` with blacklist tests
- [x] Import: `pytest`, `Decimal`, `patch` from `unittest.mock`, `Candidate`, `BlacklistHistory`, `Jurisdiction`; import `polium.task_views` to trigger `@task` registration; import `_registry` from `core.tasks`
- [x] Write a `candidate` fixture (scoped to the test module or added to `conftest.py`):
  - Creates a `Jurisdiction` (needed as FK), then a `Candidate` pointing to it
- [x] `test_blacklist_entry_below_threshold` — patch `compute_rating` to return `0.10`; call `_registry["update-candidate-rating"](candidate_id=candidate.pk)`; assert `candidate.is_blacklisted` is True; assert one `BlacklistHistory` row exists with `rating_at_blacklist = Decimal("0.1")`
- [x] `test_blacklist_not_triggered_above_entry` — patch to return `0.30`; assert `is_blacklisted` remains False; assert no `BlacklistHistory` row
- [x] `test_blacklist_lift_above_exit_threshold` — set `candidate.is_blacklisted=True`, create an open `BlacklistHistory` row; patch to return `0.60`; call handler; assert `is_blacklisted` is False; assert `BlacklistHistory.lifted_at` is set
- [x] `test_blacklist_not_lifted_between_thresholds` — set `is_blacklisted=True`; patch to return `0.40` (above entry 0.25, below exit 0.50); assert still blacklisted (asymmetric threshold — does not un-blacklist below 0.50)
- [x] `test_no_rating_returns_early` — patch `compute_rating` to return `None`; call handler; assert `Candidate.objects.get(pk=candidate.pk).current_rating == 0` (unchanged); assert no `BlacklistHistory` rows

---

#### 13.5 — SQID generation tests (core/tests.py)

Tests for `SqidMixin.save()` and the concrete `generate_sqid()` implementations.

- [x] Replace `core/tests.py` with SQID tests
- [x] Import: `pytest`, `Player`, `Jurisdiction` (for a second model type)
- [x] `test_sqid_generated_on_save` — create a `Player` via `create_user`; assert `player.sqid` is not None and not empty
- [x] `test_sqid_is_deterministic` — create two players; assert their sqids differ (different PKs → different sqids); then verify a fresh DB fetch returns the same sqid (deterministic: same PK + same salt → same sqid on re-encode)
- [x] `test_sqid_not_overwritten_on_resave` — save a player, record `sqid`; call `player.save()` again; assert `sqid` is unchanged (the `if not self.sqid` guard prevents re-generation)
- [x] `test_different_models_produce_different_sqids_for_same_pk` — find or create a Player and a Jurisdiction that share the same PK value; assert their sqids differ (different salts → different output for the same integer)

---

#### 13.6 — Maturity guard tests (core/tests.py)

Append to the same file as §13.5 (both are `core` tests), or keep in `core/tests.py` together.

- [x] `test_immature_new_player` — use the `player` fixture (freshly created, 0 days old, 0 surveys); assert `account_is_mature(player)` is `False`
- [x] `test_immature_old_player_insufficient_surveys` — set `date_joined` to 8 days ago but create only 2 survey responses; assert `False`
- [x] `test_immature_player_enough_surveys_too_young` — create 3 survey responses but do not change `date_joined` (player is 0 days old); assert `False`
- [x] `test_mature_player` — use the `mature_player` fixture; assert `account_is_mature(player)` is `True`

---

#### 13.7 — Lifecycle deprecation tests (lifecycle/tests.py)

Tests for `LifecycleMixin.should_deprecate()` and `Jurisdiction.delete()`. Use `Jurisdiction` as the concrete model — it is the only concrete `LifecycleMixin` model currently defined.

- [x] Replace `lifecycle/tests.py` with deprecation tests
- [x] Import: `pytest`, `Player`, `Jurisdiction`, `JurisdictionDuplicateFlag`, `JurisdictionFollow`, `LifecycleMixin`
- [x] `test_should_deprecate_false_with_no_flags_no_engagement` — create a Jurisdiction with `active_engagement=0`, no flags; assert `should_deprecate()` is `False`
- [x] `test_should_deprecate_true_with_flag_and_zero_engagement` — add one `JurisdictionDuplicateFlag`; assert `should_deprecate()` is `True` (any flag triggers deprecation when `active_engagement == 0`)
- [x] `test_should_deprecate_false_below_ratio` — set `active_engagement=10`; add 0 flags; assert `False` (0 < 10 / 10 = 1.0)
- [x] `test_should_deprecate_true_at_ratio` — set `active_engagement=10`; add 1 flag; assert `True` (1 >= 10 / 10 = 1.0)
- [x] `test_should_deprecate_false_just_below_ratio` — set `active_engagement=20`; add 1 flag; assert `False` (1 < 20 / 10 = 2.0)
- [x] `test_delete_migrates_children_to_winning_jurisdiction` — create parent Jurisdiction, a child Jurisdiction, a duplicate flag pointing to a winning Jurisdiction; call `parent.delete()`; assert the child's `parent` FK now points to the winning Jurisdiction
- [x] `test_delete_migrates_followers_to_winning_jurisdiction` — create a Jurisdiction with a `JurisdictionFollow` and a duplicate flag; call `delete()`; assert the follow now points to the winning Jurisdiction
- [x] `test_delete_with_no_flags_removes_cleanly` — create a Jurisdiction with no duplicate flags; call `delete()`; assert no error and the object is gone

---

#### 13.8 — Points ledger tests (points/tests.py)

Tests for `points.service.award_points`.

- [x] Replace `points/tests.py` with ledger tests
- [x] Import: `pytest`, `Decimal`, `award_points`, `PointTransaction`, `Player`; use the `player` fixture from `conftest.py`
- [x] `test_award_points_creates_transaction` — call `award_points(player, Decimal("10.00"), "test")`; assert `PointTransaction.objects.filter(player=player).count() == 1`; assert the transaction has the correct `amount` and `reason`
- [x] `test_award_points_increments_total` — call `award_points` twice with amounts 10 and 5; `player.refresh_from_db()`; assert `player.total_points == Decimal("15.00")`
- [x] `test_award_points_with_source_sets_content_type` — call `award_points(player, Decimal("1.00"), "test", source=player)` (player as its own source for simplicity); assert the transaction's `content_type` is the `Player` ContentType and `object_id == player.pk`
- [x] `test_award_points_is_atomic` — patch `Player.objects.filter(...).update` to raise an exception after `PointTransaction.objects.create`; assert the transaction is rolled back (i.e., no `PointTransaction` row remains after the exception)

---

#### 13.9 — Task handler tests (polium/tests.py)

Append task handler tests to the same file as §13.4 (blacklist tests), since they share the `candidate` setup and both relate to `polium/task_views.py`.

- [x] Ensure `import polium.task_views` appears at module level in the test file — this triggers the `@task` decorator which registers all handlers in `_registry`; without this import the registry is empty and `_registry["update-candidate-rating"]` raises a `KeyError`
- [x] `test_registry_contains_update_candidate_rating` — assert `"update-candidate-rating" in _registry`; confirms the handler is registered and callable via `enqueue()` in dev
- [x] `test_task_updates_current_rating` — patch `compute_rating` to return `0.75`; call `_registry["update-candidate-rating"](candidate_id=candidate.pk)`; refresh from DB; assert `candidate.current_rating == Decimal("0.75")`
- [x] `test_task_callable_directly_without_http` — assert calling `_registry["update-candidate-rating"](candidate_id=candidate.pk)` does not raise (no `HttpRequest` required — proves the registry stores the raw function, not the view wrapper)

---

#### 13.10 — Run the full suite

- [x] Run `uv run pytest -v` — all tests must pass with no errors or failures
- [x] Run `uv run pytest --tb=short -q` — confirm clean summary output

---

#### Phase 13 complete when
- [x] `pyproject.toml` has `[tool.pytest.ini_options]` with `DJANGO_SETTINGS_MODULE`
- [x] `conftest.py` defines `player` and `mature_player` fixtures; `mature_player` satisfies both age and survey count conditions
- [x] `surveys/tests.py` covers: no responses → None, weighted average, 365-day cutoff, inactive criteria
- [x] `polium/tests.py` covers: blacklist entry, blacklist no-trigger, lift at exit threshold, asymmetric threshold hold, None rating early exit, registry presence, direct call without HTTP
- [x] `core/tests.py` covers: sqid generated on save, deterministic, not overwritten on resave, different models differ; plus maturity: immature new, immature old+few surveys, immature young+enough surveys, mature
- [x] `lifecycle/tests.py` covers: should_deprecate all ratio cases; delete migrates children; delete migrates followers; delete with no flags
- [x] `points/tests.py` covers: creates transaction, increments total, sets content_type with source, is atomic
- [x] `uv run pytest -v` → all tests pass
