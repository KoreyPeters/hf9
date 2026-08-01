import string
from pathlib import Path

from decouple import Csv, config

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Secrets on this project have arrived carrying a UTF-8 BOM and CRLF more than
# once, because writing one from PowerShell adds both. The failure is silent and
# nasty: an ALLOWED_HOSTS entry that never matches, a WEBAUTHN_RP_ID that never
# matches its domain, a TASK_BASE_URL that produces a malformed task target. So
# every string that comes from the environment is cleaned on the way in.
_JUNK = string.whitespace + "﻿"


def clean(value: str) -> str:
    return value.strip(_JUNK)


SECRET_KEY = config("SECRET_KEY")
ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="localhost", cast=Csv(strip=_JUNK))

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "allauth.socialaccount.providers.apple",
    "anymail",
    "core",
    "accounts",
    "surveys",
    "points",
    "lifecycle",
    "evidence",
    "polium",
    "spendium",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "hf.urls"
WSGI_APPLICATION = "hf.wsgi.application"
ASGI_APPLICATION = "hf.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "spendium.context_processors.action_centre_badge",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": config("DB_PATH", default=BASE_DIR / "db.sqlite3"),
        "OPTIONS": {
            "init_command": (
                "PRAGMA journal_mode=WAL; "
                "PRAGMA synchronous=NORMAL; "
                "PRAGMA foreign_keys=ON; "
                "PRAGMA cache_size=-32000; "
                "PRAGMA temp_store=MEMORY; "
                "PRAGMA mmap_size=134217728;"
            ),
            # Take the write lock at BEGIN rather than upgrading to it later.
            #
            # Without this Django issues a bare `BEGIN`, which is DEFERRED: the
            # connection takes no lock, becomes a reader at its first SELECT —
            # pinning a WAL snapshot — and only tries to become a writer when it
            # first writes. If anything else committed in between, that upgrade
            # fails outright with SQLITE_BUSY_SNAPSHOT, which SQLite reports
            # using the same words as ordinary contention: "database is locked".
            #
            # It is not contention, and the difference matters because the two
            # want opposite fixes. A contended writer waits; a stale snapshot
            # fails instantly, because SQLite does not call the busy handler for
            # it — no amount of waiting can rescue a snapshot, only a rollback.
            # That is why raising the timeout below from 20s to 60s bought
            # nothing at all.
            #
            # This made every receipt upload fail: the upload redirects to the
            # detail page, SESSION_SAVE_EVERY_REQUEST rewrites a session row on
            # that request, and the process-receipt task was sitting in an open
            # transaction waiting on Gemini. Deterministic, not a race.
            # See plans/receipt-upload-database-locked.md, and
            # core/test_sqlite_transaction_mode.py, which fails without this.
            #
            # The cost is that every atomic block now serialises on the write
            # lock, including read-only ones. At one instance and one worker
            # that is not a real cost. It would become one if the worker count
            # rises — which is already load-bearing for other reasons; see
            # item 1 in plans/operational-debt.md.
            "transaction_mode": "IMMEDIATE",
            # How long a writer waits for the lock before giving up. Only
            # meaningful alongside the setting above: until it was added, the
            # failure this was raised to prevent bypassed the busy handler
            # entirely, so this number governed nothing. Now it governs a real
            # queue. 60s is generous for a live request — see the open question
            # in plans/receipt-upload-database-locked.md.
            "timeout": 15,
        },
    }
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": config("REDIS_URL", default="redis://localhost:6379/0"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# Uploaded files. In production `default` storage is GCS (see prod.py); locally
# this keeps receipt images out of the working tree root. Nothing here is
# long-lived — receipt images are deleted within 24 hours of processing.
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.Player"
SITE_ID = 1

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/polium/"

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "sesame.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

# Sessions. A year, and rolling: every request pushes the expiry back, so anyone
# who opens the app even occasionally is never logged out. Django's fortnight
# default suits a site people visit deliberately, and this is the opposite —
# scanning a receipt is a ten-second errand, and a login screen in front of it is
# enough friction to lose the scan. The cookie is already Secure, HttpOnly and
# SameSite=Lax in prod, which is what keeps a long life from being a long
# exposure; logging out still deletes the session server-side.
#
# The cost is a session write per request rather than per login, which on SQLite
# means a row rewritten on every authenticated page view. Small, but it lands on
# a single-writer database, so it is the first thing to look at if write
# contention ever shows up.
SESSION_COOKIE_AGE = 60 * 60 * 24 * 365
SESSION_SAVE_EVERY_REQUEST = True

# django-sesame — magic link tokens
SESAME_MAX_AGE = 900
SESAME_ONE_TIME = True
SESAME_INVALIDATE_ON_EMAIL_CHANGE = True

# django-allauth
ACCOUNT_EMAIL_VERIFICATION = "none"
ACCOUNT_SIGNUP_FIELDS = ["email*"]
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_ADAPTER = "accounts.adapters.AccountAdapter"
SOCIALACCOUNT_AUTO_SIGNUP = True
SOCIALACCOUNT_EMAIL_REQUIRED = True
SOCIALACCOUNT_ADAPTER = "accounts.adapters.SocialAccountAdapter"

SOCIALACCOUNT_PROVIDERS = {
    "google": {
        "APP": {
            "client_id": config("GOOGLE_CLIENT_ID", default=""),
            "secret": config("GOOGLE_CLIENT_SECRET", default=""),
        },
        "SCOPE": ["profile", "email"],
        "AUTH_PARAMS": {"access_type": "online"},
    },
    "apple": {
        "APP": {
            "client_id": config("APPLE_CLIENT_ID", default=""),
            "secret": config("APPLE_CLIENT_SECRET", default=""),
            "key": config("APPLE_KEY_ID", default=""),
            "certificate_key": config("APPLE_PRIVATE_KEY", default=""),
        },
    },
}

# Email verification
EMAIL_VERIFICATION_TTL_HOURS = 48

# Passkey (WebAuthn)
WEBAUTHN_RP_ID = config("WEBAUTHN_RP_ID", default="localhost", cast=clean)
WEBAUTHN_RP_NAME = "Human Flourishing"
WEBAUTHN_ORIGIN = config("WEBAUTHN_ORIGIN", default="http://localhost:8000", cast=clean)

BLACKLIST_RATIO = config("BLACKLIST_RATIO", default=0.50, cast=float)
BLACKLIST_SUSTAINED_DAYS = config("BLACKLIST_SUSTAINED_DAYS", default=90, cast=int)

GCP_PROJECT = config("GCP_PROJECT", default="", cast=clean)
GCP_REGION = config("GCP_REGION", default="us-central1")
CLOUD_TASKS_QUEUE = config("CLOUD_TASKS_QUEUE", default="hf-tasks")
TASK_BASE_URL = config("TASK_BASE_URL", default="http://localhost:8000", cast=clean)
TASK_SERVICE_ACCOUNT = config("TASK_SERVICE_ACCOUNT", default="", cast=clean)

SQID_SALTS = {
    "candidate": config("SQID_SALT_CANDIDATE"),
    "election": config("SQID_SALT_ELECTION"),
    "player": config("SQID_SALT_PLAYER"),
    "jurisdiction": config("SQID_SALT_JURISDICTION"),
    "manufacturer": config("SQID_SALT_MANUFACTURER"),
    "product": config("SQID_SALT_PRODUCT"),
    "store": config("SQID_SALT_STORE"),
}

LIFECYCLE = {
    "DEPRECATION_RATIO": 10,
    "DELETION_DAYS": 180,
    "MATURITY_ACCOUNT_AGE_DAYS": 7,
    "MATURITY_SURVEY_COUNT": 3,
}

POLIUM = {
    "ENDORSED_MULTIPLIER": config("ENDORSED_MULTIPLIER", default=2.0, cast=float),
    "BLACKLIST_MULTIPLIER": config("BLACKLIST_MULTIPLIER", default=0.25, cast=float),
}

SPENDIUM = {
    # Independent confirmations required before a product alias is treated as
    # authoritative and stops prompting. See plans/spendium-product-identity-and-ratings.md.
    "ALIAS_CONFIRMATIONS_REQUIRED": config(
        "ALIAS_CONFIRMATIONS_REQUIRED", default=2, cast=int
    ),
    # Contradictions from this many distinct players means the string is
    # genuinely disputed rather than mis-tapped, and needs a human.
    "ALIAS_REVIEW_CONTRADICTIONS": config(
        "ALIAS_REVIEW_CONTRADICTIONS", default=2, cast=int
    ),
    # Days a purchase stays player-linked. At expiry the row is copied to the
    # anonymous layer and deleted. This is also the window in which a player may
    # rate and disambiguate their purchases.
    "PURCHASE_RETENTION_DAYS": config("PURCHASE_RETENTION_DAYS", default=30, cast=int),
    # Receipt images are a published commitment: deleted within 24 hours of
    # processing, regardless of account status. See templates/spendium/privacy.html.
    "IMAGE_RETENTION_HOURS": config("IMAGE_RETENTION_HOURS", default=24, cast=int),
    # How long a fresh upload is left to its own task before the sweep will also
    # pick it up. The sweep is a backstop, not a second runner: the cases it
    # exists for — a dropped task, a receipt that waited out an emergency stop —
    # are minutes old at least, while a receipt uploaded seconds ago already has
    # a task in flight. Both running at once costs a duplicate Gemini extraction
    # and a fight over the single SQLite writer.
    "SWEEP_GRACE_MINUTES": config("SWEEP_GRACE_MINUTES", default=5, cast=int),
    # Receipt extraction. Vertex AI is reached through the Gen AI SDK; the old
    # vertexai.generative_models path is retired.
    "GEMINI_MODEL": config("GEMINI_MODEL", default="gemini-2.5-flash"),
    "GEMINI_LOCATION": config("GEMINI_LOCATION", default="us-central1"),
    # Extraction is not a creative task — the model should be decisive about
    # what is printed on the receipt rather than exploratory.
    "GEMINI_TEMPERATURE": config("GEMINI_TEMPERATURE", default=0.1, cast=float),
    # Money read off a receipt should reconcile to the cent; the tolerance
    # absorbs rounding on weight-priced lines, not genuine misreads.
    "ARITHMETIC_TOLERANCE": config("ARITHMETIC_TOLERANCE", default="0.05"),
    # A receipt older than the retention window can never be rated, so there is
    # no point accepting one.
    "MAX_RECEIPT_AGE_DAYS": config("MAX_RECEIPT_AGE_DAYS", default=30, cast=int),
    # Upload limits. Phone cameras produce large files, but a receipt does not
    # need to be one — and the cap is what stops an upload endpoint being used
    # as free storage.
    "MAX_UPLOAD_BYTES": config("MAX_UPLOAD_BYTES", default=10 * 1024 * 1024, cast=int),
    "ALLOWED_UPLOAD_TYPES": ("image/jpeg", "image/png", "image/webp", "image/heic"),
    # Perceptual hashes within this many bits of each other are treated as the
    # same receipt. Difference hashing is tolerant of scale and lighting, so a
    # small distance still means "a photo of the same piece of paper".
    "DUPLICATE_HASH_DISTANCE": config("DUPLICATE_HASH_DISTANCE", default=5, cast=int),
    # How far back to look for a duplicate. Long enough to catch a resubmission,
    # short enough that a genuinely repeated shop is not blocked.
    "DUPLICATE_LOOKBACK_DAYS": config("DUPLICATE_LOOKBACK_DAYS", default=90, cast=int),
    # Ratings.
    # A response not anchored to a receipt still counts, but less. It may be
    # anyone with an opinion, and it is the first thing a manufacturer disputing
    # a rating would attack.
    "UNVERIFIED_RATING_WEIGHT": config("UNVERIFIED_RATING_WEIGHT", default="0.4"),
    # Receipts a player may scan before membership is required. Membership is
    # still what pays for scanning; the trial exists so nobody meets that wall
    # before they have any reason to care about it. Enough for a few weeks of
    # ordinary shopping — long enough for the loop to prove itself, short enough
    # to still mean something.
    "FREE_TRIAL_UPLOADS": config("FREE_TRIAL_UPLOADS", default=10, cast=int),
    # The display threshold is not here — it lives on `MatchConfig` so it can be
    # ratcheted from the admin as players arrive, without a deploy.
    # k-anonymity before an aggregate may be published. Distinct from the
    # display threshold: this one is about nobody reconstructing an individual
    # basket from sparse data, not about the number being meaningful.
    "PUBLISH_K": config("PUBLISH_K", default=10, cast=int),
    "PUBLISH_K_SENSITIVE": config("PUBLISH_K_SENSITIVE", default=25, cast=int),
    # The floor on points per dollar, so participating always pays something
    # even when nothing involved has been rated yet. A floor rather than a
    # bonus: it is overtaken by real ratings rather than added to them, so it
    # never inflates a mature payout or muddies the ethical signal.
    "BASE_POINTS_PER_DOLLAR": config("BASE_POINTS_PER_DOLLAR", default="2"),
    # What makes a product "hot" — worth interrupting a player about. These are
    # the plan's open question on hot thresholds: they cannot be set sensibly
    # before real purchase volume exists, so they are config rather than
    # constants.
    "HOT_TRENDING_DAYS": config("HOT_TRENDING_DAYS", default=7, cast=int),
    "HOT_TRENDING_PURCHASES": config("HOT_TRENDING_PURCHASES", default=25, cast=int),
    # A rating moving this far, in either direction, means something happened.
    "HOT_RATING_MOVE": config("HOT_RATING_MOVE", default="0.15"),
    "HOT_RATING_WINDOW_DAYS": config("HOT_RATING_WINDOW_DAYS", default=30, cast=int),
    # How long a computed hot flag lasts before it has to re-earn attention.
    "HOT_DURATION_DAYS": config("HOT_DURATION_DAYS", default=14, cast=int),
    # Email restraint. One a week at most, and only for things that genuinely
    # warrant interrupting someone.
    "ONBOARDING_EMAILS": config("ONBOARDING_EMAILS", default=2, cast=int),
    "EMAIL_MIN_GAP_DAYS": config("EMAIL_MIN_GAP_DAYS", default=7, cast=int),
    # Abuse controls. Both hold points for review rather than rejecting the
    # receipt: the data is still worth having, and a false positive that
    # delays a reward is recoverable where one that discards a shop is not.
    "VELOCITY_LIMIT_PER_HOUR": config("VELOCITY_LIMIT_PER_HOUR", default=5, cast=int),
    # Both snapshot tables gain a row per subject per day, forever. Trends are
    # read over two years at most, so anything older is storage with no reader.
    "SNAPSHOT_RETENTION_DAYS": config("SNAPSHOT_RETENTION_DAYS", default=800, cast=int),
    "HIGH_VALUE_HOLD": config("HIGH_VALUE_HOLD", default="500"),
    # How the purchase was evidenced. A photographed till roll is worth more
    # than an unevidenced claim.
    "VERIFICATION_MULTIPLIERS": {
        "receipt": "1.0",
        "qr": "1.0",
        "self_report": "0.5",
        "online": "0.5",
    },
}

MEMBER_MULTIPLIER: float = config("MEMBER_MULTIPLIER", default=1.5, cast=float)
SUSTAINING_MEMBER_MULTIPLIER: float = config(
    "SUSTAINING_MEMBER_MULTIPLIER", default=2.0, cast=float
)
