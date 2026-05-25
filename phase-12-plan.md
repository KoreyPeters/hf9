# Phase 12 Todo — Migrations and Initial Data

Phase 12 applies all accumulated migrations to the database for the first time, creates the seed management command for Polium survey criteria, runs the seed, and creates the initial superuser for admin access.

---

#### Migration state at the start of Phase 12

All `makemigrations` work was completed as part of each app's phase. No new migration files need to be created here. Current state confirmed:

| App | Migration | Status |
|---|---|---|
| `accounts` | `0001_initial.py` | exists |
| `surveys` | `0001_initial.py` | exists |
| `points` | `0001_initial.py` | exists |
| `evidence` | `0001_initial.py` | exists |
| `polium` | `0001_initial.py` | exists |
| `core` | — | abstract model, no migration needed |
| `lifecycle` | — | abstract model, no migration needed |
| `spendium` | — | no models defined yet, no migration needed |

---

#### 12.1 — Pre-flight check

- [x] Confirm Docker Compose is running (PostgreSQL must be up before `migrate` will succeed):
  ```
  docker compose ps
  ```
  The `db` service must show `running`. If not: `docker compose up -d`
- [x] Confirm no pending unmade migrations remain:
  ```
  uv run python manage.py makemigrations --check --dry-run
  ```
  Expected output: `No changes detected`

---

#### 12.2 — Apply migrations

- [x] Run `uv run python manage.py migrate`
  - Django will apply migrations in dependency order automatically — no manual ordering needed
  - Expected: all Django built-in tables (auth, contenttypes, sessions, admin) plus the five app tables are created
  - Confirm the output ends with no errors and shows each migration as `OK`

---

#### 12.3 — Seed management command infrastructure

Django management commands require a specific directory structure with `__init__.py` files at each level. The `polium/management/` tree does not exist yet.

- [x] Create `polium/management/__init__.py` (empty file)
- [x] Create `polium/management/commands/__init__.py` (empty file)

---

#### 12.4 — seed_criteria management command (polium/management/commands/seed_criteria.py)

- [x] Create `polium/management/commands/seed_criteria.py`
- [x] Import `BaseCommand` from `django.core.management.base`; import `Category` and `Criterion` from `surveys.models`
- [x] Define `INITIAL_CRITERIA` as a module-level list of dicts — each dict has `category` (str), `game` (str), and `criteria` (list of `(question, weight)` tuples):
  ```python
  INITIAL_CRITERIA = [
      {
          "category": "Climate and Environment",
          "game": "polium",
          "criteria": [
              ("Has the candidate voted consistently to reduce carbon emissions?", 2.0),
              ("Has the candidate opposed subsidies for fossil fuel industries?", 1.5),
          ],
      },
  ]
  ```
- [x] Define `Command(BaseCommand)`:
  - `help = "Seed initial survey criteria for Polium"`
  - `handle(self, *args: object, **options: object) -> None`:
    - Iterate `INITIAL_CRITERIA`
    - For each block: `Category.objects.get_or_create(name=block["category"], game=block["game"], defaults={"description": ""})`
    - For each `(question, weight)` in `block["criteria"]`: `Criterion.objects.get_or_create(category=cat, question=question, defaults={"weight": weight})`
    - After all blocks: `self.stdout.write(self.style.SUCCESS("Criteria seeded."))`
    - Use `get_or_create` throughout so the command is idempotent — safe to run multiple times without creating duplicates

---

#### 12.5 — Run system check and seed command

- [x] Run `uv run python manage.py check` — must be clean (confirms the new management command loads without errors)
- [x] Run `uv run python manage.py seed_criteria`
  - Expected output: `Criteria seeded.`
  - Confirm in the database: one `Category` row ("Climate and Environment" / "polium") and two `Criterion` rows

---

#### 12.6 — Create superuser

- [x] Run `uv run python manage.py createsuperuser`
  - Follow the prompts for username, email, and password
  - The created user is a `Player` instance (since `AUTH_USER_MODEL = "accounts.Player"`) with `is_staff=True` and `is_superuser=True`

---

#### 12.7 — Smoke-test admin

- [x] Start the development server: `uv run python manage.py runserver` (or `daphne`)
- [x] Open `http://localhost:8000/admin/` in a browser
- [x] Log in with the superuser credentials from §12.6
- [x] Confirm the following app groups appear in the admin index:
  - **Accounts**: Player, Membership
  - **Evidence**: Evidence
  - **Points**: Point transactions
  - **Polium**: Jurisdiction, Candidate, Election
  - **Surveys**: Category, Criterion, Survey responses
- [x] Confirm the seeded Category and Criteria are visible under Surveys → Categories

---

#### Phase 12 complete when
- [x] `uv run python manage.py makemigrations --check --dry-run` → `No changes detected`
- [x] `uv run python manage.py migrate` completes with all migrations `OK`
- [x] `polium/management/commands/seed_criteria.py` exists with `INITIAL_CRITERIA`, `Command`, and idempotent `get_or_create` logic
- [x] `uv run python manage.py seed_criteria` runs cleanly and outputs `Criteria seeded.`
- [x] A superuser `Player` exists in the database
- [x] Django admin is reachable at `/admin/` and shows all registered models
