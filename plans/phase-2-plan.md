# Phase 2 Todo — Core App

Phase 2 produces two files: `core/models.py` (the `SqidMixin` abstract model) and `core/maturity.py` (the account maturity guard). Both are foundational — every later phase depends on one or both of them.

#### 2.1 — SqidMixin (core/models.py)

- [x] Replace the auto-generated placeholder content in `core/models.py` with the `SqidMixin` implementation
- [x] Add imports: `django.db.models`, `sqids.Sqids`, `django.conf.settings`
- [x] Define `SqidMixin(models.Model)` with:
  - `sqid = models.CharField(max_length=20, unique=True, blank=True, db_index=True)`
  - `generate_sqid(self)` — raises `NotImplementedError` with a message directing subclasses to implement it using their own salt. Not decorated `@abstractmethod` — Django's model metaclass does not interact cleanly with ABC.
  - `save(self, *args, **kwargs)` override — call `super().save(*args, **kwargs)` **first** (so the PK is assigned), then check `if not self.sqid`, generate and persist: `type(self).objects.filter(pk=self.pk).update(sqid=self.sqid)`. Use `.update()` not `.save()` to avoid recursing back into this method.
  - `class Meta: abstract = True`
- [x] Confirm `uv run python manage.py makemigrations core` prints `No changes detected` — SqidMixin is abstract and produces no migration
- [x] Confirm `uv run python manage.py check` is clean

#### 2.2 — Account maturity guard (core/maturity.py)

- [x] Create `core/maturity.py`
- [x] Write `account_is_mature(player)`:
  - Do **not** import `Player` directly — `accounts` will import from `core`, so importing `accounts` here creates a circular dependency. The function duck-types the argument; use `TYPE_CHECKING` with a forward-reference string annotation if a type hint is added.
  - Read thresholds from `settings.LIFECYCLE`: `MATURITY_ACCOUNT_AGE_DAYS` and `MATURITY_SURVEY_COUNT`
  - `age_ok`: `(timezone.now() - player.date_joined).days >= cfg['MATURITY_ACCOUNT_AGE_DAYS']`
  - `surveys_ok`: `player.survey_responses.count() >= cfg['MATURITY_SURVEY_COUNT']`
  - Both conditions must be satisfied — return `age_ok and surveys_ok`
- [x] Confirm `uv run python manage.py check` remains clean after adding this file

#### A note on tests

Tests for both components require `Player` (to create a concrete `SqidMixin` subclass and to call `account_is_mature()`). `Player` is defined in Phase 3. Tests for this module are therefore written in Phase 13 using the fixtures from `conftest.py`. No test file is written in this phase.

#### Phase 2 complete when
- [x] `core/models.py` contains `SqidMixin` as an abstract model with `sqid`, `generate_sqid()`, and the `save()` override
- [x] `core/maturity.py` contains `account_is_mature()` reading thresholds from `settings.LIFECYCLE`
- [x] `uv run python manage.py makemigrations core` → `No changes detected`
- [x] `uv run python manage.py check` → `System check identified no issues`
