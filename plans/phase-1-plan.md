# Phase 1 Todo — Django Project Scaffold

#### 1.1 — Create the Django project
- [x] Run `uv run django-admin startproject hf .` from the project root
- [x] Confirm `manage.py` and the `hf/` package directory were created
- [x] Delete the auto-generated `hf/settings.py` — it will be replaced by a settings package in §1.3

#### 1.2 — Create all apps
- [x] `uv run python manage.py startapp core`
- [x] `uv run python manage.py startapp accounts`
- [x] `uv run python manage.py startapp surveys`
- [x] `uv run python manage.py startapp points`
- [x] `uv run python manage.py startapp lifecycle`
- [x] `uv run python manage.py startapp evidence`
- [x] `uv run python manage.py startapp polium`
- [x] `uv run python manage.py startapp spendium`
- [x] Confirm each app directory exists at the project root with `models.py`, `apps.py`, `admin.py`, `views.py`, `tests.py`, `migrations/`

#### 1.3 — Split settings into a package
- [x] Create `hf/settings/` directory
- [x] Create an empty `hf/settings/__init__.py`
- [x] Write `hf/settings/base.py` — all shared settings:
  - `SECRET_KEY`, `ALLOWED_HOSTS` via `python-decouple`
  - `INSTALLED_APPS` — all Django builtins, `anymail`, and all 8 HF apps
  - `MIDDLEWARE` — standard Django middleware stack
  - `ROOT_URLCONF = 'hf.urls'`
  - `WSGI_APPLICATION`, `ASGI_APPLICATION = 'hf.asgi.application'`
  - `TEMPLATES` — standard Django template config with `APP_DIRS = True`
  - `DATABASES` — PostgreSQL via `python-decouple` (`DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`)
  - `CACHES` — Redis backend via `REDIS_URL` from decouple
  - `AUTH_PASSWORD_VALIDATORS` — standard Django validators
  - `LANGUAGE_CODE`, `TIME_ZONE = 'UTC'`, `USE_I18N = True`, `USE_TZ = True`
  - `STATIC_URL = 'static/'`
  - `DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'`
  - GCP / Cloud Tasks vars: `GCP_PROJECT`, `GCP_REGION`, `CLOUD_TASKS_QUEUE`, `TASK_BASE_URL`, `TASK_SERVICE_ACCOUNT` — all via decouple with safe defaults
  - `SQID_SALTS` dict — four keys (`candidate`, `election`, `player`, `jurisdiction`), each reading from decouple
  - `LIFECYCLE` dict — `DEPRECATION_RATIO = 10`, `DELETION_DAYS = 180`, `MATURITY_ACCOUNT_AGE_DAYS = 7`, `MATURITY_SURVEY_COUNT = 3`
- [x] Write `hf/settings/dev.py`:
  - `from .base import *`
  - `DEBUG = True`
  - `EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'`
  - `EMAIL_HOST = 'localhost'`, `EMAIL_PORT = 1025` (Mailpit)
- [x] Write `hf/settings/prod.py`:
  - `from .base import *`
  - `DEBUG = False`
  - `SECURE_SSL_REDIRECT = True`, `SESSION_COOKIE_SECURE = True`, `CSRF_COOKIE_SECURE = True`
  - `EMAIL_BACKEND = 'anymail.backends.mailgun.EmailBackend'`
  - `ANYMAIL` dict — `MAILGUN_API_KEY` and `MAILGUN_SENDER_DOMAIN` via decouple
  - `DEFAULT_FROM_EMAIL` via decouple
  - `STORAGES` — GCS backend for both `default` and `staticfiles` (via `django-storages`)
  - `GS_BUCKET_NAME`, `GS_DEFAULT_ACL = None`, `GS_QUERYSTRING_AUTH = False`
  - `STATIC_URL` pointing at the GCS bucket
- [x] Update `manage.py` — set default `DJANGO_SETTINGS_MODULE` to `hf.settings.dev`
- [x] Update `hf/wsgi.py` — set default `DJANGO_SETTINGS_MODULE` to `hf.settings.dev`

#### 1.4 — ASGI configuration
- [x] Write `hf/asgi.py` — `get_asgi_application()` with `DJANGO_SETTINGS_MODULE` defaulting to `hf.settings.dev`
- [x] Smoke-test: `uv run daphne -b 0.0.0.0 -p 8000 hf.asgi:application` starts without error
- [x] Confirm http://localhost:8000 returns a Django response (404 or welcome page — either is fine)

#### 1.5 — URL routing scaffold
- [x] Write `hf/urls.py` — wire in `admin/`, `accounts/`, `polium/`, `spendium/`, and `tasks/` namespaces (stub includes are fine; the target `urls.py` files don't need to exist yet — use a placeholder or empty include)
- [x] Write `hf/task_urls.py` — empty `urlpatterns = []` for now; this file is where `@task`-decorated views will be registered in later phases
- [x] Create stub `urls.py` in `accounts/`, `polium/`, and `spendium/` apps with empty `urlpatterns = []` so the includes in `hf/urls.py` resolve without error

#### 1.6 — Cloud Tasks infrastructure
- [x] Write `core/tasks.py` — three components:
  - `_registry: dict` — module-level dict mapping URL path strings to handler functions
  - `@task(url_path)` decorator — registers the function in `_registry`; wraps it as a CSRF-exempt POST view that validates the OIDC token in prod and calls the function with the decoded JSON payload
  - `enqueue(url_path, payload)` — in dev (`DEBUG=True`) calls the registered function directly and synchronously; in prod creates a Cloud Tasks HTTP request via `google.cloud.tasks_v2`
  - `_verify_oidc(request)` helper — extracts the Bearer token, verifies it with `google.oauth2.id_token`, uses `TASK_BASE_URL` as the expected audience; returns `False` (not an exception) on any failure

#### Phase 1 complete when
- [x] `uv run python manage.py check --settings=hf.settings.dev` exits with `System check identified no issues`
- [x] `uv run daphne -b 0.0.0.0 -p 8000 hf.asgi:application` starts and serves a response at http://localhost:8000
- [x] All 8 HF apps appear in `INSTALLED_APPS` in `base.py`
- [x] `core/tasks.py` exists with `task`, `enqueue`, and `_verify_oidc` defined
