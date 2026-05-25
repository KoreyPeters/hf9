# Phase 5 Todo — Points App

Phase 5 produces the points ledger — an append-only audit trail of every points event — and the `award_points` service that writes to it atomically. Two files: `points/models.py` and `points/service.py`, plus admin and migration.

---

#### 5.1 — PointTransaction model (points/models.py)

- [x] Replace the auto-generated placeholder in `points/models.py`
- [x] Define `PointTransaction(models.Model)`:
  - `player` FK: use `settings.AUTH_USER_MODEL` — **not** a direct import of `Player`. Same reasoning as Phase 4: cross-app FKs to the user model must use the string form. Set `on_delete=models.CASCADE, related_name="point_transactions"`.
  - `amount = models.DecimalField(max_digits=10, decimal_places=2)` — no positivity constraint; negative values are valid for future correction/reversal entries. No default — every transaction must state an explicit amount.
  - `reason = models.CharField(max_length=100)` — human-readable label such as `"vote_declaration"`, `"vote_declaration_social"`, `"survey"`, `"purchase"`. No `choices` constraint; new reason codes can be added without a migration.
  - `content_type = models.ForeignKey(ContentType, on_delete=models.SET_NULL, null=True)` — nullable because not all transactions have a linked source object
  - `object_id = models.PositiveIntegerField(null=True)` — nullable for the same reason
  - `source = GenericForeignKey("content_type", "object_id")` — not a DB column; does not appear in migrations
  - `created_at = models.DateTimeField(auto_now_add=True)` — no `updated_at`; the ledger is append-only
  - `class Meta: indexes = [models.Index(fields=["player", "created_at"])]`
  - `__str__` returning something minimal is sufficient, e.g. `f"{self.player} +{self.amount} ({self.reason})"`
- [x] The model must never be updated after creation — this is an application-level constraint enforced by `award_points`, not a DB constraint. No `save()` override is needed; the constraint lives in the service.

---

#### 5.2 — award_points service (points/service.py)

- [x] Create `points/service.py`
- [x] Write `award_points(player, amount: Decimal, reason: str, source: Model | None = None) -> None`:
  - Import `Decimal` from `decimal`, `Model` from `django.db.models`, `F` from `django.db.models`, `atomic` from `django.db.transaction`
  - For the `player` parameter type: use `TYPE_CHECKING` to import `Player` from `accounts.models` for the annotation without creating a runtime circular import risk. Annotate as `player: Player`.
  - For the ORM update call (`Player.objects.filter(...).update(...)`) use `get_user_model()` from `django.contrib.auth` — this avoids importing `Player` at runtime while still referencing the correct model class.
  - Wrap the entire function body in `with atomic():`
  - Inside the transaction:
    1. `PointTransaction.objects.create(player=player, amount=amount, reason=reason, content_type=..., object_id=...)`
       - `content_type`: `ContentType.objects.get_for_model(source) if source is not None else None`
       - `object_id`: `source.pk if source is not None else None`
    2. `get_user_model().objects.filter(pk=player.pk).update(total_points=F("total_points") + amount)`
       - The `F()` expression performs an atomic increment at the database level, preventing race conditions that would occur if you read then wrote `player.total_points` in Python.
  - Do **not** call `player.refresh_from_db()` — the caller is responsible for re-fetching if they need the updated total. The service is intentionally fire-and-forget.

---

#### 5.3 — Admin (points/admin.py)

- [x] Replace the auto-generated placeholder in `points/admin.py`
- [x] Register `PointTransaction` with a `ModelAdmin`:
  - `list_display = ("player", "amount", "reason", "content_type", "object_id", "created_at")`
  - `list_filter = ("reason",)`
  - `readonly_fields = ("player", "amount", "reason", "content_type", "object_id", "source", "created_at")` — all fields readonly to enforce the append-only contract in the admin UI
  - Override `has_add_permission` to return `False` — point transactions must only be created via `award_points`, never directly through the admin
  - Override `has_delete_permission` to return `False` — the ledger is permanent; deletions must never happen through the admin

---

#### 5.4 — Migration

- [x] Run `uv run python manage.py makemigrations points`
  - Expect one new table: `points_pointtransaction`
  - `source` (GenericForeignKey) must not appear in the migration — confirm it is absent
  - Confirm `content_type` and `object_id` are both nullable in the migration
- [x] Run `uv run python manage.py check` — must be clean

---

#### Phase 5 complete when
- [x] `points/models.py` defines `PointTransaction` with `settings.AUTH_USER_MODEL` FK, nullable GenericForeignKey source, and `(player, created_at)` index
- [x] `points/service.py` defines `award_points` that writes the ledger and updates `total_points` atomically using `F()`
- [x] `points/admin.py` registers `PointTransaction` as fully read-only with `has_add_permission` and `has_delete_permission` returning `False`
- [x] `points/migrations/0001_initial.py` exists and was generated without errors
- [x] `uv run python manage.py check` → `System check identified no issues`
