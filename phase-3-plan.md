# Phase 3 Todo — Accounts App

Phase 3 defines the custom user model and the membership model. The `AUTH_USER_MODEL` setting must be added to `base.py` before any migration is run anywhere in the project — this step is the prerequisite for everything else in this phase.

---

#### 3.0 — AUTH_USER_MODEL setting (prerequisite — do this first)

- [x] Add `AUTH_USER_MODEL = "accounts.Player"` to `hf/settings/base.py`
  - Place it near `DEFAULT_AUTO_FIELD` at the bottom of the shared settings block
  - This must be present before `makemigrations` is run for any app that touches a user FK — including `accounts` itself

---

#### 3.1 — Player model (accounts/models.py)

- [x] Replace the auto-generated placeholder in `accounts/models.py` with the `Player` and `Membership` models
- [x] Import `AbstractUser` from `django.contrib.auth.models` — do **not** import Django's built-in `User`
- [x] Import `SqidMixin` from `core.models`
- [x] Define `Player(SqidMixin, AbstractUser)`:
  - MRO is Player → SqidMixin → AbstractUser → AbstractBaseUser → Model. `SqidMixin.save()` wraps `AbstractUser.save()` correctly: `super().save()` traverses the full chain and writes to the DB before the sqid is generated. Do not override `save()` again on `Player`.
  - Add only `total_points = models.DecimalField(max_digits=12, decimal_places=2, default=0)` — all other fields (`username`, `email`, `password`, `date_joined`, `last_login`, `is_active`, `groups`, `user_permissions`) are inherited from `AbstractUser` and must not be redefined
  - Implement `generate_sqid(self) -> str` using `settings.SQID_SALTS["player"]`
  - `class Meta`: add `indexes = [models.Index(fields=["total_points"])]`. Do **not** set `app_label` — it is inferred from the app
  - `__str__` returns `self.username`

#### 3.2 — Membership model (accounts/models.py)

- [x] Define `Membership(models.Model)` in the same file, below `Player`:
  - `player = models.OneToOneField(Player, on_delete=models.CASCADE, related_name="membership")` — direct import of `Player` is fine here since `Membership` lives in the same app
  - `started_at = models.DateTimeField(auto_now_add=True)`
  - `expires_at = models.DateTimeField()`
  - `is_active = models.BooleanField(default=True)`
  - `__str__` returns `f"{self.player} membership (expires {self.expires_at.date()})"`

---

#### 3.3 — Admin registration (accounts/admin.py)

- [x] Register `Player` with the Django admin using `UserAdmin` from `django.contrib.auth.admin`
  - Django automatically registers the built-in `User` model with `UserAdmin`; since we have replaced `User`, that registration no longer exists. Without registering `Player`, the admin has no user management interface.
  - Subclass `UserAdmin` as `PlayerAdmin` and add `total_points` and `sqid` to `readonly_fields` so they are visible in the admin detail view without being editable in ways that bypass model logic
  - `fieldsets`: extend `UserAdmin.fieldsets` to include a `"HF"` section with `("total_points", "sqid")`
  - Register: `admin.site.register(Player, PlayerAdmin)`
- [x] Register `Membership` with a basic `ModelAdmin`

---

#### 3.4 — Migration

- [x] Run `uv run python manage.py makemigrations accounts`
  - This produces the first migration in the project. It will create the `accounts_player` table (replacing `auth_user`) and the `accounts_membership` table.
  - Confirm the generated migration file exists at `accounts/migrations/0001_initial.py`
  - Confirm the migration references `AbstractUser` fields — open the file and verify it includes `username`, `email`, `password`, `date_joined` etc. as part of the `accounts_player` table, not as references to a separate `auth_user` table
- [x] Run `uv run python manage.py check` — must be clean

> **Do not run `migrate` yet** — `python manage.py migrate` is deferred to Phase 12 when all app migrations exist. Only `makemigrations` is run per phase.

---

#### Phase 3 complete when
- [x] `AUTH_USER_MODEL = "accounts.Player"` is present in `hf/settings/base.py`
- [x] `accounts/models.py` defines `Player(SqidMixin, AbstractUser)` with `total_points` and `generate_sqid()`, and `Membership` linked to `Player`
- [x] `accounts/admin.py` registers `Player` via a `PlayerAdmin(UserAdmin)` subclass and `Membership` via a basic `ModelAdmin`
- [x] `accounts/migrations/0001_initial.py` exists and was generated without errors
- [x] `uv run python manage.py check` → `System check identified no issues`
