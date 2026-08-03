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

# Who hears about unhandled exceptions. Django mails these a full traceback,
# which is the part Cloud Monitoring cannot give you — its alert says a 5xx
# happened and leaves you to go and find out why.
ADMINS = [("HF errors", config("ERROR_EMAIL", default="me@koreypeters.org"))]
# The From address on those mails. Without it Django uses root@localhost, which
# Mailgun will not accept.
SERVER_EMAIL = DEFAULT_FROM_EMAIL

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "throttle_duplicates": {
            "()": "core.logging.ThrottleDuplicates",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
        },
        # Fires on any unhandled exception — i.e. anything that returns a 500.
        # Deliberately not attached to the root logger: that would mail on every
        # WARNING, and the point is that arriving mail means something broke.
        "mail_admins": {
            "class": "django.utils.log.AdminEmailHandler",
            "level": "ERROR",
            "include_html": False,
            "filters": ["throttle_duplicates"],
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
            "handlers": ["console", "mail_admins"],
            "level": "DEBUG",
            "propagate": False,
        },
        # Failures the app handles rather than raising. `django.request` only
        # covers what reaches Django as a 500, so anything we deliberately
        # absorb — a task the queue would not take, say — leaves no trace beyond
        # a console line nobody is reading. Deciding a failure should not reach
        # the player is not the same as deciding nobody needs to know about it.
        # ERROR and above only; the handler's own level enforces that.
        "spendium": {
            "handlers": ["console", "mail_admins"],
            "level": "INFO",
            "propagate": False,
        },
        # Same reasoning as spendium, and one specific case it exists for: a
        # missing Turnstile secret makes signup refuse everybody, and refusing
        # everybody is not something that raises. Without this the only trace
        # would be a console line, and the symptom — nobody signing up — is
        # indistinguishable from nobody trying.
        "accounts": {
            "handlers": ["console", "mail_admins"],
            "level": "INFO",
            "propagate": False,
        },
    },
}

_bucket = config("GCS_BUCKET_NAME", cast=clean)
_media_bucket = config("GCS_MEDIA_BUCKET_NAME", cast=clean)

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
