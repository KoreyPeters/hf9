# Implementation Plan — Human Flourishing Game Suite

## Platform Decision

**GCP** with **Mailgun** for email. The original plan targeted AWS, but GCP is the revised choice.

**GCP service mapping:**

| Concern | Service |
|---|---|
| Compute | Cloud Run |
| Database | Cloud SQL — PostgreSQL |
| Cache | Cloud Memorystore — Redis |
| Async tasks | Cloud Tasks |
| Scheduled tasks | Cloud Scheduler |
| Static files / media | Cloud Storage + Cloud CDN |
| Email | Mailgun (via `django-anymail`) |
| Secrets | Secret Manager |
| DNS | Cloud DNS |
| SSL | Google-managed certificates (via Cloud Run custom domain) |
| Container registry | Artifact Registry |

### Cloud Run and SSE

The original design doc noted that GCP Cloud Run is a poor fit for Datastar's server-sent events model. This concern was valid for default Cloud Run configuration. With deliberate configuration it works:

- **`--timeout 3600`** — Cloud Run's maximum request timeout. SSE connections are terminated after one hour by the platform. Datastar's client handles reconnection automatically; this is a minor player-visible interruption, not a failure.
- **`--min-instances 1`** — prevents Cloud Run from scaling to zero, which would terminate all open SSE connections. Required for any SSE-based application.
- **Keepalive every 25 seconds** — prevents the Google load balancer from treating idle connections as stale.

The one-hour connection ceiling is the only real limitation compared to ECS Fargate. For Stage 1 it is acceptable. If it proves disruptive, the path forward is GKE Autopilot, which imposes no connection timeout.

### Background tasks: Cloud Tasks + Cloud Scheduler

There is no Celery and no worker process. Background work is handled by two GCP services:

- **Cloud Tasks** — for tasks triggered by user actions (e.g. recomputing a candidate rating after a survey submission). The web view enqueues an HTTP task; Cloud Tasks delivers it as a POST to a Django endpoint on the same Cloud Run service.
- **Cloud Scheduler** — for periodic maintenance tasks (deprecation checks, deletion). Cloud Scheduler fires a POST on a cron schedule, authenticated with an OIDC token, to the same Django task endpoints.

Both services call identical Django views secured by OIDC token validation. In development, `enqueue()` calls the handler function directly and synchronously — no HTTP round-trip, no emulator required.

This eliminates the Compute Engine worker VM, simplifies infrastructure, and removes Redis as a task broker. Cloud Memorystore serves the Django cache only.

---

## Phase 0 — Local Development Environment

Todo list: [phase-0-todo.md](phase-0-todo.md)

### 0.1 Python environment

Use `uv` for dependency management. Fast, deterministic, and produces a lockfile.

```bash
uv init hf9
cd hf9
uv add django daphne psycopg[binary] redis "django-storages[google]" google-cloud-tasks \
       sqids django-anymail pillow python-decouple
uv add --dev ruff pytest pytest-django coverage
```

### 0.2 Docker Compose for local services

All external services run locally via Docker Compose. The Django app itself runs natively (not in Docker) during development for fast iteration.

```yaml
# docker-compose.yml
services:
  db:
    image: postgres:16
    environment:
      POSTGRES_DB: hf
      POSTGRES_USER: hf
      POSTGRES_PASSWORD: hf
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"

  mailpit:
    image: axllent/mailpit
    ports:
      - "1025:1025"   # SMTP
      - "8025:8025"   # Web UI

volumes:
  postgres_data:
```

```bash
docker compose up -d
```

### 0.3 Environment variables

Use `python-decouple` for environment variable management. Never hardcode secrets.

```ini
# .env  (gitignored — never committed)
SECRET_KEY=your-local-secret-key
DEBUG=True
DB_NAME=hf
DB_USER=hf
DB_PASSWORD=hf
DB_HOST=localhost
DB_PORT=5432
REDIS_URL=redis://localhost:6379/0
EMAIL_HOST=localhost
EMAIL_PORT=1025

# Cloud Tasks — unused in dev (DEBUG=True bypasses HTTP dispatch)
# Set these to real values before deploying to prod
GCP_PROJECT=
GCP_REGION=us-central1
CLOUD_TASKS_QUEUE=hf-tasks
TASK_BASE_URL=http://localhost:8000
TASK_SERVICE_ACCOUNT=

# SQID salts — generate with: python -c "import secrets; print(secrets.token_hex(32))"
SQID_SALT_CANDIDATE=
SQID_SALT_ELECTION=
SQID_SALT_PLAYER=
SQID_SALT_JURISDICTION=
```

---

## Phase 1 — Django Project Scaffold

### 1.1 Create the project

```bash
django-admin startproject hf .
```

The project root `hf/` becomes the settings package. Immediately split settings into a package:

```
hf/
  settings/
    __init__.py     # empty
    base.py         # shared across all environments
    dev.py          # local development overrides
    prod.py         # production overrides
```

Point `manage.py` and `asgi.py` at `hf.settings.dev` locally, `hf.settings.prod` in production, via the `DJANGO_SETTINGS_MODULE` environment variable.

### 1.2 Create all apps up front

```bash
python manage.py startapp core
python manage.py startapp accounts
python manage.py startapp surveys
python manage.py startapp points
python manage.py startapp lifecycle
python manage.py startapp evidence
python manage.py startapp polium
python manage.py startapp spendium
```

Move each app into the project root so the layout matches the design:

```
hf9/
  hf/                  # Django project package (settings, urls, asgi, task_urls)
  core/                # SqidMixin, tasks infrastructure, maturity guard
  accounts/
  surveys/
  points/
  lifecycle/
  evidence/
  polium/
  spendium/
  manage.py
  pyproject.toml
```

### 1.3 Base settings

```python
# hf/settings/base.py
from decouple import config, Csv
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = config('SECRET_KEY')
ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='localhost', cast=Csv())

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # third-party
    'anymail',
    # HF apps
    'core',
    'accounts',
    'surveys',
    'points',
    'lifecycle',
    'evidence',
    'polium',
    'spendium',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'hf.urls'
WSGI_APPLICATION = 'hf.wsgi.application'
ASGI_APPLICATION = 'hf.asgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': config('DB_NAME', default='hf'),
        'USER': config('DB_USER', default='hf'),
        'PASSWORD': config('DB_PASSWORD', default='hf'),
        'HOST': config('DB_HOST', default='localhost'),
        'PORT': config('DB_PORT', default='5432'),
    }
}

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': config('REDIS_URL', default='redis://localhost:6379/0'),
    }
}

# Cloud Tasks
GCP_PROJECT = config('GCP_PROJECT', default='')
GCP_REGION = config('GCP_REGION', default='us-central1')
CLOUD_TASKS_QUEUE = config('CLOUD_TASKS_QUEUE', default='hf-tasks')
TASK_BASE_URL = config('TASK_BASE_URL', default='http://localhost:8000')
TASK_SERVICE_ACCOUNT = config('TASK_SERVICE_ACCOUNT', default='')

# SQID salts — treated as secrets
SQID_SALTS = {
    'candidate':    config('SQID_SALT_CANDIDATE'),
    'election':     config('SQID_SALT_ELECTION'),
    'player':       config('SQID_SALT_PLAYER'),
    'jurisdiction': config('SQID_SALT_JURISDICTION'),
}

# Lifecycle thresholds — configurable without code deployment
LIFECYCLE = {
    'DEPRECATION_RATIO': 10,          # flag_count >= active_engagement / this
    'DELETION_DAYS': 180,             # days of zero engagement before auto-delete
    'MATURITY_ACCOUNT_AGE_DAYS': 7,
    'MATURITY_SURVEY_COUNT': 3,
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
```

```python
# hf/settings/dev.py
from .base import *

DEBUG = True
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = 'localhost'
EMAIL_PORT = 1025
```

```python
# hf/settings/prod.py
from .base import *

DEBUG = False
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# Email via Mailgun
EMAIL_BACKEND = 'anymail.backends.mailgun.EmailBackend'
ANYMAIL = {
    'MAILGUN_API_KEY': config('MAILGUN_API_KEY'),
    'MAILGUN_SENDER_DOMAIN': config('MAILGUN_SENDER_DOMAIN'),
}
DEFAULT_FROM_EMAIL = config('DEFAULT_FROM_EMAIL', default='noreply@humanflourishing.org')

# GCS for static files and media
STORAGES = {
    'default': {'BACKEND': 'storages.backends.gcloud.GoogleCloudStorage'},
    'staticfiles': {'BACKEND': 'storages.backends.gcloud.GoogleCloudStorage'},
}
GS_BUCKET_NAME = config('GCS_BUCKET_NAME')
GS_DEFAULT_ACL = None        # use uniform bucket-level access, not per-object ACLs
GS_QUERYSTRING_AUTH = False  # public bucket — no signed URLs needed for static assets
STATIC_URL = f"https://storage.googleapis.com/{config('GCS_BUCKET_NAME')}/static/"
```

### 1.4 ASGI configuration

Daphne serves the application over ASGI. No channels required — Datastar's SSE responses are plain async Django views using `StreamingHttpResponse`.

```python
# hf/asgi.py
import os
from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'hf.settings.dev')
application = get_asgi_application()
```

Run locally with:
```bash
daphne -b 0.0.0.0 -p 8000 hf.asgi:application
```

### 1.5 Cloud Tasks infrastructure

There is no `celery.py`. Background task dispatch is handled by `core/tasks.py`, which provides a `@task` decorator and an `enqueue()` function.

**The `@task` decorator** does two things at once:

1. Registers the decorated function in a local registry (keyed by URL path), so `enqueue()` can call it directly in development.
2. Wraps the function as a Django view — the view validates the OIDC token from Cloud Tasks or Cloud Scheduler and then calls the function with the decoded JSON payload. This view is what you wire into `hf/task_urls.py`.

**`enqueue()`** checks `DEBUG`: in development it calls the registered function directly and synchronously; in production it creates a Cloud Tasks HTTP request targeting the same URL path on the live Cloud Run service.

```python
# core/tasks.py
import json
import logging
from functools import wraps
from django.conf import settings
from django.http import JsonResponse, HttpResponseForbidden
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

logger = logging.getLogger(__name__)
_registry: dict = {}


def task(url_path: str):
    """
    Marks a function as a Cloud Tasks / Cloud Scheduler handler.

    Registers the function for direct local calls in dev.
    Wraps it as a Django view for HTTP delivery in prod.
    Wire the returned view into hf/task_urls.py.
    """
    def decorator(fn):
        _registry[url_path] = fn

        @wraps(fn)
        @csrf_exempt
        @require_POST
        def view(request):
            if not settings.DEBUG and not _verify_oidc(request):
                return HttpResponseForbidden()
            payload = json.loads(request.body or '{}')
            fn(**payload)
            return JsonResponse({'status': 'ok'})

        return view

    return decorator


def enqueue(url_path: str, payload: dict = None):
    """
    Dispatch a background task.

    In dev (DEBUG=True): calls the handler function directly (synchronous).
    In prod: enqueues a Cloud Tasks HTTP request to the Cloud Run service.
    """
    if payload is None:
        payload = {}

    if settings.DEBUG:
        fn = _registry.get(url_path)
        if fn is None:
            raise ValueError(
                f"No task registered for '{url_path}'. "
                f"Ensure the task_views module containing it is imported at startup."
            )
        fn(**payload)
        return

    from google.cloud import tasks_v2
    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(
        settings.GCP_PROJECT,
        settings.GCP_REGION,
        settings.CLOUD_TASKS_QUEUE,
    )
    client.create_task(request={
        'parent': parent,
        'task': {
            'http_request': {
                'http_method': tasks_v2.HttpMethod.POST,
                'url': f"{settings.TASK_BASE_URL}/tasks/{url_path}/",
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps(payload).encode(),
                'oidc_token': {
                    'service_account_email': settings.TASK_SERVICE_ACCOUNT,
                },
            }
        }
    })


def _verify_oidc(request) -> bool:
    """
    Verify the OIDC token attached by Cloud Tasks or Cloud Scheduler.
    Both services use the same token format and the same service account.
    The expected audience is TASK_BASE_URL (the Cloud Run service URL).
    """
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return False
    token = auth[7:]
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token
        id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            audience=settings.TASK_BASE_URL,
        )
        return True
    except Exception:
        logger.warning('Task endpoint received invalid or missing OIDC token.')
        return False
```

Task views are imported at startup via `hf/task_urls.py` — this is what populates the registry before any `enqueue()` call can occur.

---

## Phase 2 — Core App

```python
# core/models.py
from django.db import models
from sqids import Sqids
from django.conf import settings


class SqidMixin(models.Model):
    sqid = models.CharField(max_length=20, unique=True, blank=True, db_index=True)

    def generate_sqid(self):
        raise NotImplementedError(
            "Subclasses must implement generate_sqid() "
            "using their own salt from settings."
        )

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not self.sqid:
            self.sqid = self.generate_sqid()
            type(self).objects.filter(pk=self.pk).update(sqid=self.sqid)

    class Meta:
        abstract = True
```

```python
# core/maturity.py
from django.conf import settings
from django.utils import timezone


def account_is_mature(player):
    """
    Returns True if the player meets the configured maturity threshold.
    Required for protected actions: duplicate flagging, office history edits.
    """
    cfg = settings.LIFECYCLE
    age_ok = (timezone.now() - player.created_at).days >= cfg['MATURITY_ACCOUNT_AGE_DAYS']
    surveys_ok = player.survey_responses.count() >= cfg['MATURITY_SURVEY_COUNT']
    return age_ok and surveys_ok
```

---

## Phase 3 — Accounts App

```python
# accounts/models.py
from django.db import models
from django.contrib.auth.models import User
from django.conf import settings
from sqids import Sqids
from core.models import SqidMixin


class Player(SqidMixin, models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='player')
    total_points = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def generate_sqid(self):
        sqids = Sqids(alphabet=settings.SQID_SALTS['player'])
        return sqids.encode([self.pk])

    class Meta:
        indexes = [models.Index(fields=['total_points'])]

    def __str__(self):
        return self.user.username


class Membership(models.Model):
    player = models.OneToOneField(Player, on_delete=models.CASCADE, related_name='membership')
    started_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.player} membership (expires {self.expires_at.date()})"
```

---

## Phase 4 — Surveys App

```python
# surveys/models.py
from django.db import models
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from accounts.models import Player


class Category(models.Model):
    name = models.CharField(max_length=200)
    description = models.TextField()
    game = models.CharField(max_length=50)  # 'polium', 'spendium', 'humanium'

    class Meta:
        verbose_name_plural = 'categories'

    def __str__(self):
        return f"{self.game} / {self.name}"


class Criterion(models.Model):
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name='criteria')
    question = models.TextField()
    weight = models.DecimalField(max_digits=5, decimal_places=2, default=1.0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.question[:80]


class SurveyResponse(models.Model):
    player = models.ForeignKey(
        Player, on_delete=models.SET_NULL, null=True, related_name='survey_responses'
    )
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    subject = GenericForeignKey('content_type', 'object_id')
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['content_type', 'object_id'])]


class CriterionAnswer(models.Model):
    survey_response = models.ForeignKey(
        SurveyResponse, on_delete=models.CASCADE, related_name='answers'
    )
    criterion = models.ForeignKey(
        Criterion, on_delete=models.PROTECT, related_name='answers'
    )
    answer = models.BooleanField()  # True = yes, False = no
```

### Rating calculation

The rating calculator is a shared utility, not a method on any model. This keeps the logic testable and reusable across games.

```python
# surveys/ratings.py
from django.utils import timezone
from datetime import timedelta
from .models import CriterionAnswer, SurveyResponse
from django.contrib.contenttypes.models import ContentType


def compute_rating(subject) -> float | None:
    """
    Weighted average of all survey responses for subject in the past 12 months.
    Returns a float between 0.0 and 1.0, or None if no responses exist.
    """
    cutoff = timezone.now() - timedelta(days=365)
    ct = ContentType.objects.get_for_model(subject)

    responses = SurveyResponse.objects.filter(
        content_type=ct,
        object_id=subject.pk,
        submitted_at__gte=cutoff,
    )
    if not responses.exists():
        return None

    answers = CriterionAnswer.objects.filter(
        survey_response__in=responses,
        criterion__is_active=True,
    ).select_related('criterion')

    total_weight = 0.0
    weighted_sum = 0.0
    for answer in answers:
        w = float(answer.criterion.weight)
        total_weight += w
        weighted_sum += w * (1.0 if answer.answer else 0.0)

    if total_weight == 0:
        return None
    return weighted_sum / total_weight
```

---

## Phase 5 — Points App

```python
# points/models.py
from django.db import models
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from accounts.models import Player


class PointTransaction(models.Model):
    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name='point_transactions')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    reason = models.CharField(max_length=100)
    # e.g. 'vote_declaration', 'vote_declaration_social', 'survey', 'purchase'
    content_type = models.ForeignKey(ContentType, on_delete=models.SET_NULL, null=True)
    object_id = models.PositiveIntegerField(null=True)
    source = GenericForeignKey('content_type', 'object_id')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['player', 'created_at'])]
```

```python
# points/service.py
from decimal import Decimal
from django.db import models, transaction as db_transaction
from .models import PointTransaction
from accounts.models import Player
from django.contrib.contenttypes.models import ContentType


def award_points(player: Player, amount: Decimal, reason: str, source=None):
    """
    Append a point transaction and update the denormalised total atomically.
    The ledger is the source of truth; total_points is a cache.
    """
    with db_transaction.atomic():
        PointTransaction.objects.create(
            player=player,
            amount=amount,
            reason=reason,
            content_type=ContentType.objects.get_for_model(source) if source else None,
            object_id=source.pk if source else None,
        )
        Player.objects.filter(pk=player.pk).update(
            total_points=models.F('total_points') + amount
        )
```

---

## Phase 6 — Lifecycle App

The lifecycle pattern is shared infrastructure. The abstract model lives here; each game's self-managed entities compose from it. Periodic maintenance tasks are defined here using `@task` and wired into `hf/task_urls.py`.

```python
# lifecycle/models.py
from django.db import models
from django.conf import settings


class LifecycleMixin(models.Model):
    """
    Abstract mixin providing the shared deprecation/deletion lifecycle.
    Concrete models must define `active_engagement` as a PositiveIntegerField.
    """
    STATUS_ACTIVE = 'active'
    STATUS_DEPRECATED = 'deprecated'
    STATUS_DELETED = 'deleted'
    STATUS_CHOICES = [
        (STATUS_ACTIVE, 'Active'),
        (STATUS_DEPRECATED, 'Deprecated'),
        (STATUS_DELETED, 'Deleted'),
    ]

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    active_engagement = models.PositiveIntegerField(default=0)
    deprecated_at = models.DateTimeField(null=True, blank=True)

    @property
    def flag_count(self):
        raise NotImplementedError("Concrete models must implement flag_count.")

    def should_deprecate(self):
        ratio = settings.LIFECYCLE['DEPRECATION_RATIO']
        if self.active_engagement == 0:
            return self.flag_count > 0
        return self.flag_count >= self.active_engagement / ratio

    class Meta:
        abstract = True
```

```python
# lifecycle/task_views.py
from django.utils import timezone
from datetime import timedelta
from django.conf import settings
from core.tasks import task


@task('check-deprecations')
def check_deprecations():
    """
    Scan all lifecycle-managed entities and deprecate those that have crossed
    the flag threshold. Triggered hourly by Cloud Scheduler.
    """
    from polium.models import Jurisdiction
    for obj in Jurisdiction.objects.filter(status='active'):
        if obj.should_deprecate():
            Jurisdiction.objects.filter(pk=obj.pk).update(
                status='deprecated',
                deprecated_at=timezone.now(),
            )


@task('check-deletions')
def check_deletions():
    """
    Delete deprecated entities with zero engagement past the configured
    retention period. Triggered daily by Cloud Scheduler.
    """
    from polium.models import Jurisdiction
    threshold = timezone.now() - timedelta(days=settings.LIFECYCLE['DELETION_DAYS'])
    for obj in Jurisdiction.objects.filter(
        status='deprecated', active_engagement=0, deprecated_at__lte=threshold
    ):
        obj.delete()  # concrete model handles cascade logic in its delete()
```

---

## Phase 7 — Evidence App

```python
# evidence/models.py
from django.db import models
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from accounts.models import Player
from surveys.models import Criterion


class Evidence(models.Model):
    STATUS_VISIBLE = 'visible'
    STATUS_HIDDEN = 'hidden'
    STATUS_REMOVED = 'removed'
    STATUS_CHOICES = [
        (STATUS_VISIBLE, 'Visible'),
        (STATUS_HIDDEN, 'Hidden'),
        (STATUS_REMOVED, 'Permanently removed'),
    ]

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    subject = GenericForeignKey('content_type', 'object_id')

    submitted_by = models.ForeignKey(
        Player, on_delete=models.SET_NULL, null=True, related_name='evidence_submitted'
    )
    url = models.URLField()
    note = models.TextField()
    criterion = models.ForeignKey(
        Criterion, on_delete=models.SET_NULL, null=True, blank=True, related_name='evidence'
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_VISIBLE)
    net_usefulness_score = models.IntegerField(default=0)
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['content_type', 'object_id', 'status', 'net_usefulness_score']),
        ]


class EvidenceUsefulness(models.Model):
    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name='evidence_usefulness_votes')
    evidence = models.ForeignKey(Evidence, on_delete=models.CASCADE, related_name='usefulness_votes')
    is_useful = models.BooleanField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [['player', 'evidence']]


class EvidenceFlag(models.Model):
    REASON_CHOICES = [
        ('irrelevant', 'Irrelevant'),
        ('low_quality', 'Low quality'),
        ('misleading', 'Misleading'),
        ('malicious', 'Malicious'),
    ]

    flagging_player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name='evidence_flags')
    evidence = models.ForeignKey(Evidence, on_delete=models.CASCADE, related_name='flags')
    reason = models.CharField(max_length=20, choices=REASON_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [['flagging_player', 'evidence']]
```

```python
# evidence/service.py
from .models import Evidence


def recompute_evidence_status(evidence: Evidence):
    """
    Hide evidence when flag_count >= net_usefulness_score / 10.
    Called after every new flag submission.
    """
    flag_count = evidence.flags.count()
    threshold = max(1, evidence.net_usefulness_score / 10)
    if flag_count >= threshold and evidence.status == Evidence.STATUS_VISIBLE:
        Evidence.objects.filter(pk=evidence.pk).update(status=Evidence.STATUS_HIDDEN)


def recompute_usefulness_score(evidence: Evidence):
    """Recompute net_usefulness_score after any vote change."""
    useful = evidence.usefulness_votes.filter(is_useful=True).count()
    not_useful = evidence.usefulness_votes.filter(is_useful=False).count()
    Evidence.objects.filter(pk=evidence.pk).update(net_usefulness_score=useful - not_useful)
```

---

## Phase 8 — Polium App

### 8.1 Jurisdiction models

```python
# polium/models.py
from django.db import models
from django.conf import settings
from sqids import Sqids
from core.models import SqidMixin
from lifecycle.models import LifecycleMixin
from accounts.models import Player


class Jurisdiction(SqidMixin, LifecycleMixin, models.Model):
    name = models.CharField(max_length=300)
    level = models.CharField(max_length=100)
    parent = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='children'
    )
    created_by = models.ForeignKey(
        Player, on_delete=models.SET_NULL, null=True, related_name='jurisdictions_created'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def generate_sqid(self):
        sqids = Sqids(alphabet=settings.SQID_SALTS['jurisdiction'])
        return sqids.encode([self.pk])

    @property
    def flag_count(self):
        return self.duplicate_flags.count()

    def delete(self, *args, **kwargs):
        winning = self._winning_jurisdiction()
        if winning:
            self.children.all().update(parent=winning)
            for follow in self.followers.all():
                follow.jurisdiction = winning
                follow.save()
        super().delete(*args, **kwargs)

    def _winning_jurisdiction(self):
        flag = self.duplicate_flags.order_by('-created_at').first()
        return flag.points_to if flag else None

    class Meta:
        indexes = [
            models.Index(fields=['status', 'active_engagement']),
            models.Index(fields=['parent', 'status']),
        ]

    def __str__(self):
        return f"{self.name} ({self.level})"


class JurisdictionDuplicateFlag(models.Model):
    flagging_player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name='jurisdiction_flags')
    flagged_jurisdiction = models.ForeignKey(Jurisdiction, on_delete=models.CASCADE, related_name='duplicate_flags')
    points_to = models.ForeignKey(Jurisdiction, on_delete=models.CASCADE, related_name='flagged_as_duplicate_of')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [['flagging_player', 'flagged_jurisdiction']]


class JurisdictionFollow(models.Model):
    DEPTH_CHOICES = [
        ('this', 'This level only'),
        ('all', 'This level and below'),
    ]
    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name='followed_jurisdictions')
    jurisdiction = models.ForeignKey(Jurisdiction, on_delete=models.CASCADE, related_name='followers')
    depth = models.CharField(max_length=10, choices=DEPTH_CHOICES, default='all')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [['player', 'jurisdiction']]
```

### 8.2 Election and Candidate models

```python
class Election(SqidMixin, models.Model):
    name = models.CharField(max_length=300)
    jurisdiction = models.ForeignKey(
        Jurisdiction, on_delete=models.SET_NULL, null=True, related_name='elections'
    )
    election_date = models.DateField()
    created_by = models.ForeignKey(
        Player, on_delete=models.SET_NULL, null=True, related_name='elections_created'
    )
    external_reference = models.URLField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def generate_sqid(self):
        sqids = Sqids(alphabet=settings.SQID_SALTS['election'])
        return sqids.encode([self.pk])

    class Meta:
        indexes = [models.Index(fields=['jurisdiction', 'election_date'])]

    def __str__(self):
        return self.name


class Candidate(SqidMixin, models.Model):
    name = models.CharField(max_length=300)
    jurisdiction = models.ForeignKey(
        Jurisdiction, on_delete=models.SET_NULL, null=True, related_name='candidates'
    )
    office = models.CharField(max_length=200)
    election = models.ForeignKey(
        Election, on_delete=models.SET_NULL, null=True, related_name='candidates'
    )
    created_by = models.ForeignKey(
        Player, on_delete=models.SET_NULL, null=True, related_name='candidates_created'
    )
    external_reference = models.URLField(blank=True)
    bio = models.TextField(blank=True)
    current_rating = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    is_blacklisted = models.BooleanField(default=False)
    blacklisted_at = models.DateTimeField(null=True, blank=True)
    engagement_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    # Duplicate linking — symmetrical, self-referential M2M
    duplicates = models.ManyToManyField('self', blank=True, symmetrical=True)

    def generate_sqid(self):
        sqids = Sqids(alphabet=settings.SQID_SALTS['candidate'])
        return sqids.encode([self.pk])

    class Meta:
        indexes = [
            models.Index(fields=['jurisdiction', 'current_rating']),
            models.Index(fields=['engagement_count']),
            models.Index(fields=['is_blacklisted']),
        ]

    def __str__(self):
        return self.name


class OfficeHistory(models.Model):
    """
    Append-only log of offices held by a candidate.
    Protected field — editable only by sufficiently mature accounts.
    Every change is stored with the editing player and timestamp.
    """
    candidate = models.ForeignKey(Candidate, on_delete=models.CASCADE, related_name='office_history')
    office = models.CharField(max_length=300)
    jurisdiction = models.CharField(max_length=300)
    started_at = models.DateField()
    ended_at = models.DateField(null=True, blank=True)
    added_by = models.ForeignKey(
        Player, on_delete=models.SET_NULL, null=True, related_name='office_history_entries'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-started_at']
        verbose_name_plural = 'office histories'


class BlacklistHistory(models.Model):
    """Permanent, immutable record of blacklist events. Never deleted."""
    candidate = models.ForeignKey(Candidate, on_delete=models.CASCADE, related_name='blacklist_history')
    blacklisted_at = models.DateTimeField()
    lifted_at = models.DateTimeField(null=True, blank=True)
    rating_at_blacklist = models.DecimalField(max_digits=5, decimal_places=2)
    rating_at_lift = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)


class VoteDeclaration(models.Model):
    """
    A player's declared intention to vote for a candidate.
    Unverified by design. One declaration per player per election.
    """
    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name='vote_declarations')
    candidate = models.ForeignKey(Candidate, on_delete=models.CASCADE, related_name='vote_declarations')
    election = models.ForeignKey(Election, on_delete=models.CASCADE, related_name='vote_declarations')
    shared_on_social = models.BooleanField(default=False)
    shared_at = models.DateTimeField(null=True, blank=True)
    declared_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [['player', 'election']]
```

### 8.3 Blacklist engine (task view)

Rating recalculation is triggered by a view call via `enqueue()` after each survey submission. The `@task` decorator registers it for direct local calls and wraps it as a Cloud Tasks-authenticated view.

```python
# polium/task_views.py
from decimal import Decimal
from django.utils import timezone
from core.tasks import task
from surveys.ratings import compute_rating

BLACKLIST_ENTRY = Decimal('0.25')
BLACKLIST_EXIT = Decimal('0.50')


@task('update-candidate-rating')
def update_candidate_rating(candidate_id: int):
    """
    Recompute a candidate's rating after a new survey response.
    Apply or lift blacklist status as thresholds are crossed.
    Triggered via enqueue() from the survey submission view.
    """
    from .models import Candidate, BlacklistHistory

    candidate = Candidate.objects.select_for_update().get(pk=candidate_id)
    rating = compute_rating(candidate)
    if rating is None:
        return

    new_rating = Decimal(str(round(rating, 2)))
    Candidate.objects.filter(pk=candidate_id).update(current_rating=new_rating)

    if not candidate.is_blacklisted and new_rating < BLACKLIST_ENTRY:
        now = timezone.now()
        Candidate.objects.filter(pk=candidate_id).update(is_blacklisted=True, blacklisted_at=now)
        BlacklistHistory.objects.create(
            candidate=candidate,
            blacklisted_at=now,
            rating_at_blacklist=new_rating,
        )
    elif candidate.is_blacklisted and new_rating >= BLACKLIST_EXIT:
        now = timezone.now()
        Candidate.objects.filter(pk=candidate_id).update(is_blacklisted=False, blacklisted_at=None)
        BlacklistHistory.objects.filter(
            candidate=candidate, lifted_at__isnull=True
        ).update(lifted_at=now, rating_at_lift=new_rating)
```

---

## Phase 9 — Frontend: Datastar + PWA

Datastar replaces the need for a separate JS framework. The server pushes HTML fragments over SSE; the client applies them to the DOM.

### 9.1 SSE view pattern

```python
# core/sse.py
import json
from django.http import StreamingHttpResponse


def sse_response(generator):
    """Wrap a generator of (event, data) tuples as an SSE StreamingHttpResponse."""
    def stream():
        for event, data in generator:
            yield f"event: {event}\n"
            yield f"data: {json.dumps(data)}\n\n"
    response = StreamingHttpResponse(stream(), content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response
```

Keepalive to prevent the Cloud Run load balancer from closing idle SSE connections:

```python
# core/sse.py — keepalive addition
import asyncio


async def keepalive_generator(real_generator):
    """Interleave SSE keepalives with real events."""
    async for event in real_generator:
        yield event
    while True:
        await asyncio.sleep(25)
        yield ": keepalive\n\n"
```

### 9.2 Base template with Datastar

```html
<!-- templates/base.html -->
<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{% block title %}Human Flourishing{% endblock %}</title>
  <script type="module" src="https://cdn.jsdelivr.net/npm/@starfederation/datastar@latest/dist/datastar.js"></script>
  <link rel="manifest" href="/manifest.json">
</head>
<body>
  {% block content %}{% endblock %}
</body>
</html>
```

### 9.3 PWA manifest

```json
{
  "name": "Human Flourishing",
  "short_name": "HF",
  "start_url": "/",
  "display": "standalone",
  "background_color": "#ffffff",
  "theme_color": "#000000",
  "icons": [
    { "src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png" },
    { "src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png" }
  ]
}
```

---

## Phase 10 — URL Routing

```python
# hf/urls.py
from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/', include('accounts.urls')),
    path('polium/', include('polium.urls')),
    path('spendium/', include('spendium.urls')),
    path('tasks/', include('hf.task_urls')),
]
```

```python
# hf/task_urls.py
#
# Importing these modules registers their @task-decorated functions
# in core.tasks._registry, which enqueue() uses for direct dev calls.
# All task endpoints live under /tasks/<url_path>/.
#
import lifecycle.task_views as _lifecycle
import polium.task_views as _polium
from django.urls import path

urlpatterns = [
    path('check-deprecations/', _lifecycle.check_deprecations,  name='task_check_deprecations'),
    path('check-deletions/',    _lifecycle.check_deletions,     name='task_check_deletions'),
    path('update-candidate-rating/', _polium.update_candidate_rating, name='task_update_candidate_rating'),
]
```

Public-facing Polium URLs use SQIDs, never PKs:

```python
# polium/urls.py
from django.urls import path
from . import views

app_name = 'polium'

urlpatterns = [
    path('candidates/<str:sqid>/', views.candidate_detail, name='candidate_detail'),
    path('elections/<str:sqid>/', views.election_detail, name='election_detail'),
    path('jurisdictions/<str:sqid>/', views.jurisdiction_detail, name='jurisdiction_detail'),
    path('candidates/<str:sqid>/survey/', views.submit_survey, name='submit_survey'),
    path('candidates/<str:sqid>/declare/', views.declare_vote, name='declare_vote'),
]
```

```python
# polium/views.py
from django.shortcuts import get_object_or_404
from .models import Candidate


def get_candidate_by_sqid(sqid: str) -> Candidate:
    return get_object_or_404(Candidate, sqid=sqid)
```

---

## Phase 11 — GCP Infrastructure

### 11.1 Overview

```
User → Cloud DNS → Cloud Run (Daphne, custom domain + Google-managed SSL)
                        │                    ▲              ▲
                Direct VPC Egress     Cloud Tasks    Cloud Scheduler
                ┌───────┴───────┐     (async tasks)  (hourly/daily)
                ▼               ▼
          Cloud SQL       Cloud Memorystore
          (PostgreSQL)    (Redis — cache only)

          Cloud Storage + Cloud CDN (static / media)
          Mailgun (transactional email)
          Artifact Registry (Docker images)
          Secret Manager (secrets)
```

Cloud Tasks and Cloud Scheduler both call back into the same Cloud Run service via `/tasks/*` endpoints. Cloud Run handles async task processing the same way it handles regular HTTP requests — no separate worker service, no polling process.

### 11.2 Dockerfile

```dockerfile
# Dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN pip install uv && uv sync --frozen

COPY . .

RUN python manage.py collectstatic --noinput

EXPOSE 8000
CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "hf.asgi:application"]
```

Build and push to Artifact Registry:

```bash
gcloud auth configure-docker us-central1-docker.pkg.dev
docker build -t us-central1-docker.pkg.dev/PROJECT_ID/hf/hf:latest .
docker push us-central1-docker.pkg.dev/PROJECT_ID/hf/hf:latest
```

### 11.3 Cloud Run — web service

```bash
gcloud run deploy hf-web \
  --image us-central1-docker.pkg.dev/PROJECT_ID/hf/hf:latest \
  --region us-central1 \
  --platform managed \
  --allow-unauthenticated \
  --timeout 3600 \
  --min-instances 1 \
  --max-instances 10 \
  --concurrency 1000 \
  --memory 4Gi \
  --cpu 2 \
  --vpc-egress all-traffic \
  --network default \
  --subnet default \
  --set-env-vars DJANGO_SETTINGS_MODULE=hf.settings.prod \
  --set-secrets "SECRET_KEY=hf-secret-key:latest,\
DB_PASSWORD=hf-db-password:latest,\
SQID_SALT_CANDIDATE=hf-sqid-candidate:latest,\
SQID_SALT_ELECTION=hf-sqid-election:latest,\
SQID_SALT_PLAYER=hf-sqid-player:latest,\
SQID_SALT_JURISDICTION=hf-sqid-jurisdiction:latest,\
MAILGUN_API_KEY=hf-mailgun-key:latest"
```

**Critical flags:**
- `--timeout 3600` — maximum Cloud Run request timeout; SSE connections are terminated after 1 hour
- `--min-instances 1` — prevents scale-to-zero from killing open SSE connections
- `--concurrency 1000` — Daphne's async event loop handles many concurrent connections in one process
- `--vpc-egress all-traffic` — routes outbound traffic through the VPC to reach Cloud SQL and Memorystore via private IP

### 11.4 Cloud SQL

```bash
gcloud sql instances create hf-db \
  --database-version POSTGRES_16 \
  --tier db-g1-small \
  --region us-central1 \
  --network default \
  --no-assign-ip

gcloud sql databases create hf --instance hf-db
gcloud sql users create hf --instance hf-db --password <password>
```

Set `DB_HOST` in the Cloud Run environment to the Cloud SQL private IP. Direct VPC Egress handles routing — no Auth Proxy required.

### 11.5 Cloud Memorystore (Redis — cache only)

```bash
gcloud redis instances create hf-redis \
  --size 1 \
  --region us-central1 \
  --redis-version redis_7_0 \
  --network default
```

Set `REDIS_URL` to `redis://<memorystore-ip>:6379/0`. This is the Django cache only — there is no task broker.

> **Stage 1 cost note:** Cloud Memorystore minimum is 1GB (~$35/month). If this is too expensive at Stage 1, run Redis on a small Compute Engine VM (e2-micro) instead. Migrate to Memorystore when managed reliability matters.

### 11.6 Cloud Tasks queue

```bash
gcloud tasks queues create hf-tasks \
  --location us-central1 \
  --max-concurrent-dispatches 100 \
  --max-dispatches-per-second 500
```

Grant the Cloud Run service account permission to enqueue tasks:

```bash
gcloud tasks queues add-iam-policy-binding hf-tasks \
  --location us-central1 \
  --member serviceAccount:hf-web@PROJECT_ID.iam.gserviceaccount.com \
  --role roles/cloudtasks.enqueuer
```

Grant the task service account permission to invoke the Cloud Run service (so OIDC tokens it attaches are accepted):

```bash
gcloud run services add-iam-policy-binding hf-web \
  --region us-central1 \
  --member serviceAccount:hf-tasks@PROJECT_ID.iam.gserviceaccount.com \
  --role roles/run.invoker
```

### 11.7 Cloud Scheduler (periodic tasks)

Cloud Scheduler calls the task endpoints directly with an OIDC token. The `_verify_oidc()` function in `core/tasks.py` validates both Cloud Tasks tokens and Cloud Scheduler tokens identically — both are signed JWTs from the same service account.

```bash
# check-deprecations — every hour
gcloud scheduler jobs create http hf-check-deprecations \
  --location us-central1 \
  --schedule "0 * * * *" \
  --uri "https://YOUR_CLOUD_RUN_URL/tasks/check-deprecations/" \
  --http-method POST \
  --message-body '{}' \
  --oidc-service-account-email hf-tasks@PROJECT_ID.iam.gserviceaccount.com \
  --oidc-token-audience "https://YOUR_CLOUD_RUN_URL"

# check-deletions — daily at midnight UTC
gcloud scheduler jobs create http hf-check-deletions \
  --location us-central1 \
  --schedule "0 0 * * *" \
  --uri "https://YOUR_CLOUD_RUN_URL/tasks/check-deletions/" \
  --http-method POST \
  --message-body '{}' \
  --oidc-service-account-email hf-tasks@PROJECT_ID.iam.gserviceaccount.com \
  --oidc-token-audience "https://YOUR_CLOUD_RUN_URL"
```

`YOUR_CLOUD_RUN_URL` must match `TASK_BASE_URL` in settings — `_verify_oidc()` uses it as the expected OIDC audience.

### 11.8 Cloud Storage + Cloud CDN

```bash
gcloud storage buckets create gs://hf-static \
  --location us-central1 \
  --uniform-bucket-level-access

gcloud storage buckets add-iam-policy-binding gs://hf-static \
  --member allUsers \
  --role roles/storage.objectViewer
```

`collectstatic` uploads to GCS via `django-storages`. Run it as part of the deployment pipeline before deploying a new Cloud Run revision.

### 11.9 Custom domain and SSL

```bash
gcloud run domain-mappings create \
  --service hf-web \
  --domain humanflourishing.org \
  --region us-central1
```

Google-managed SSL is provisioned automatically. No manual certificate work required.

### 11.10 Secret Manager

```bash
echo -n "your-secret-key" | gcloud secrets create hf-secret-key --data-file=-
echo -n "your-db-password" | gcloud secrets create hf-db-password --data-file=-
# repeat for each SQID salt and Mailgun key

gcloud secrets add-iam-policy-binding hf-secret-key \
  --member serviceAccount:hf-web@PROJECT_ID.iam.gserviceaccount.com \
  --role roles/secretmanager.secretAccessor
# repeat for each secret
```

### 11.11 Cost profile (Stage 1)

| Service | Approx monthly cost |
|---|---|
| Cloud Run (1 min instance, 2vCPU/4GB) | $40–60 |
| Cloud SQL (db-g1-small) | $25 |
| Cloud Memorystore (1GB) | $35 |
| Cloud Tasks | <$1 at Stage 1 volume |
| Cloud Scheduler | $0 (first 3 jobs free) |
| Cloud Storage + CDN | <$5 |
| **Total** | **~$105–125/month** |

The Compute Engine worker VM from the previous Celery design is gone entirely. Cloud Tasks and Cloud Scheduler add negligible cost at Stage 1 volume.

---

## Phase 12 — Migrations and Initial Data

Run migrations in this order (respects foreign key dependencies):

```bash
python manage.py makemigrations core
python manage.py makemigrations accounts
python manage.py makemigrations surveys
python manage.py makemigrations points
python manage.py makemigrations lifecycle
python manage.py makemigrations evidence
python manage.py makemigrations polium
python manage.py makemigrations spendium
python manage.py migrate
```

Seed initial Polium survey criteria via a management command:

```python
# polium/management/commands/seed_criteria.py
from django.core.management.base import BaseCommand
from surveys.models import Category, Criterion


INITIAL_CRITERIA = [
    {
        'category': 'Climate and Environment',
        'game': 'polium',
        'criteria': [
            ('Has the candidate voted consistently to reduce carbon emissions?', 2.0),
            ('Has the candidate opposed subsidies for fossil fuel industries?', 1.5),
        ],
    },
]


class Command(BaseCommand):
    help = 'Seed initial survey criteria for Polium'

    def handle(self, *args, **kwargs):
        for block in INITIAL_CRITERIA:
            cat, _ = Category.objects.get_or_create(
                name=block['category'], game=block['game'],
                defaults={'description': ''}
            )
            for question, weight in block['criteria']:
                Criterion.objects.get_or_create(
                    category=cat, question=question,
                    defaults={'weight': weight}
                )
        self.stdout.write(self.style.SUCCESS('Criteria seeded.'))
```

---

## Phase 13 — Testing Strategy

```python
# conftest.py
import pytest
from django.contrib.auth.models import User
from accounts.models import Player


@pytest.fixture
def player(db):
    user = User.objects.create_user(username='testplayer', password='pass')
    return Player.objects.create(user=user)


@pytest.fixture
def mature_player(db, player):
    from django.utils import timezone
    from datetime import timedelta
    Player.objects.filter(pk=player.pk).update(
        created_at=timezone.now() - timedelta(days=8)
    )
    player.refresh_from_db()
    return player
```

Key test areas:
- **Rating calculator** — correct weighted average, rolling 12-month cutoff
- **Blacklist engine** — entry at 25%, exit at 50%, asymmetric threshold
- **SQID generation** — deterministic, unique per model, uses correct salt
- **Account maturity guard** — protected actions rejected for immature accounts
- **Lifecycle deprecation** — formula fires at the right ratio, cascade on deletion
- **Points ledger** — `total_points` on Player stays consistent with ledger sum
- **Task handlers** — call `update_candidate_rating.fn(candidate_id=...)` directly, bypassing the view wrapper, to test task logic in isolation

Run with:
```bash
pytest --ds=hf.settings.dev
```

---

## Implementation Order

1. **Phase 0** — Local environment up (Docker Compose, `.env`)
2. **Phase 1** — Django scaffold, settings split, ASGI config, Cloud Tasks infrastructure (`core/tasks.py`)
3. **Phase 2** — `core` app: `SqidMixin`, maturity guard
4. **Phase 3** — `accounts` app: `Player`, `Membership`
5. **Phase 4** — `surveys` app: models + rating calculator
6. **Phase 5** — `points` app: ledger + `award_points` service
7. **Phase 6** — `lifecycle` app: abstract mixin + task views
8. **Phase 7** — `evidence` app: all three models + status service
9. **Phase 8** — `polium` app: all models + task view
10. **Phase 9** — Datastar base template + PWA manifest
11. **Phase 10** — URL routing (including `hf/task_urls.py`)
12. **Phase 12** — Migrations + seed criteria
13. **Phase 13** — Test suite
14. **Phase 11** — GCP infrastructure (when ready to deploy)
