from decouple import config

from .base import *

DEBUG = False

SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}

EMAIL_BACKEND = "anymail.backends.mailgun.EmailBackend"
ANYMAIL = {
    "MAILGUN_API_KEY": config("MAILGUN_API_KEY"),
    "MAILGUN_SENDER_DOMAIN": config("MAILGUN_SENDER_DOMAIN"),
}
DEFAULT_FROM_EMAIL = config("DEFAULT_FROM_EMAIL", default="noreply@humanflourish.ing")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "WARNING",
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console"],
            "level": "DEBUG",
            "propagate": False,
        },
    },
}

_bucket = config("GCS_BUCKET_NAME")
_media_bucket = config("GCS_MEDIA_BUCKET_NAME")

# Two buckets, and the split matters. `_bucket` is public by design — it serves
# static files and `allUsers` has objectViewer on it. Uploaded media must never
# land there: receipt images would be readable by anyone who guessed the path,
# and the paths are guessable, since Django only randomises a filename on
# collision. `_media_bucket` has public access prevention enforced.
#
# Configured per backend rather than through the shared GS_* settings, so that
# neither bucket can inherit the other's posture by omission.
STORAGES = {
    "default": {
        "BACKEND": "storages.backends.gcloud.GoogleCloudStorage",
        "OPTIONS": {
            "bucket_name": _media_bucket,
            "default_acl": None,
            # Nothing serves these over HTTP — extraction reads them
            # server-side and they are never rendered in a template. Signing is
            # set anyway so that if something ever does call `.url`, it fails
            # closed rather than emitting a public link.
            "querystring_auth": True,
        },
    },
    "staticfiles": {
        "BACKEND": "storages.backends.gcloud.GoogleCloudStorage",
        "OPTIONS": {
            "bucket_name": _bucket,
            "default_acl": None,
            "querystring_auth": False,
        },
    },
}
STATIC_URL = f"https://storage.googleapis.com/{_bucket}/static/"
