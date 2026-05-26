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
        "ENGINE": "django.db.backends.postgresql",
        "NAME": config("DB_NAME", default="hf"),
        "USER": config("DB_USER", default="hf"),
        "PASSWORD": config("DB_PASSWORD", default="hf"),
        "HOST": config("DB_HOST", default="localhost"),
        "PORT": config("DB_PORT", default="5432"),
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
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

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
}

LIFECYCLE = {
    "DEPRECATION_RATIO": 10,
    "DELETION_DAYS": 180,
    "MATURITY_ACCOUNT_AGE_DAYS": 7,
    "MATURITY_SURVEY_COUNT": 3,
}
