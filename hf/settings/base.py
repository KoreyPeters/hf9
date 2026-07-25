from pathlib import Path

from decouple import Csv, config

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = config("SECRET_KEY")
ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="localhost", cast=Csv())

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
            "timeout": 20,
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
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
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
WEBAUTHN_RP_ID = config("WEBAUTHN_RP_ID", default="localhost")
WEBAUTHN_RP_NAME = "Human Flourishing"
WEBAUTHN_ORIGIN = config("WEBAUTHN_ORIGIN", default="http://localhost:8000")

BLACKLIST_RATIO = config("BLACKLIST_RATIO", default=0.50, cast=float)
BLACKLIST_SUSTAINED_DAYS = config("BLACKLIST_SUSTAINED_DAYS", default=90, cast=int)

GCP_PROJECT = config("GCP_PROJECT", default="")
GCP_REGION = config("GCP_REGION", default="us-central1")
CLOUD_TASKS_QUEUE = config("CLOUD_TASKS_QUEUE", default="hf-tasks")
TASK_BASE_URL = config("TASK_BASE_URL", default="http://localhost:8000")
TASK_SERVICE_ACCOUNT = config("TASK_SERVICE_ACCOUNT", default="")

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
    # Days a purchase stays player-linked. At expiry the row is copied to the
    # anonymous layer and deleted. This is also the window in which a player may
    # rate and disambiguate their purchases.
    "PURCHASE_RETENTION_DAYS": config("PURCHASE_RETENTION_DAYS", default=30, cast=int),
    # Receipt images are a published commitment: deleted within 24 hours of
    # processing, regardless of account status. See templates/spendium/privacy.html.
    "IMAGE_RETENTION_HOURS": config("IMAGE_RETENTION_HOURS", default=24, cast=int),
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
}

MEMBER_MULTIPLIER: float = config("MEMBER_MULTIPLIER", default=1.5, cast=float)
SUSTAINING_MEMBER_MULTIPLIER: float = config("SUSTAINING_MEMBER_MULTIPLIER", default=2.0, cast=float)
