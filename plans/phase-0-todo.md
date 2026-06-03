# Phase 0 Todo — Local Development Environment

#### 0.1 — Python environment
- [x] Confirm `uv` is installed: `uv --version`. Install from https://docs.astral.sh/uv/ if missing.
- [x] Initialise project in-place (directory already exists): `uv init` from within `hf9/`
- [x] Add production dependencies (single `uv add` command — see §0.1 in plan.md)
- [x] Add dev dependencies: `uv add --dev ruff pytest pytest-django coverage`
- [x] Confirm `.venv/` was created and both `pyproject.toml` and `uv.lock` are present
- [x] Smoke-test the environment: `uv run python -c "import django; print(django.__version__)"`

#### 0.2 — Docker Compose
- [x] Confirm Docker Desktop is installed and running
- [x] Create `docker-compose.yml` in the project root
- [x] Start all services in the background: `docker compose up -d`
- [x] Confirm all three containers are healthy: `docker compose ps`
- [x] Confirm PostgreSQL is reachable: `docker compose exec db psql -U hf -d hf -c '\l'`
- [x] Confirm Redis is reachable: `docker compose exec redis redis-cli ping` — expect `PONG`
- [x] Confirm Mailpit web UI is accessible at http://localhost:8025

#### 0.3 — Environment variables
- [x] Create `.env` in the project root
- [x] Generate `SECRET_KEY` and paste into `.env`
- [x] Generate SQID salt for `Candidate` and paste into `.env`
- [x] Generate SQID salt for `Election`
- [x] Generate SQID salt for `Player`
- [x] Generate SQID salt for `Jurisdiction`
- [x] Leave GCP Task vars blank for now — `GCP_PROJECT`, `TASK_SERVICE_ACCOUNT`, etc. are unused while `DEBUG=True`
- [x] Confirm `.env` is git-ignored (`.gitignore` line 151)

#### Phase 0 complete when
- [x] `docker compose ps` shows all three containers as `running` (no `exited`)
- [x] `uv run python -c "import django"` exits without error
- [x] All four SQID salts are filled in `.env`
