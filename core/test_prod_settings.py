"""The production settings module, exercised.

Tests run against hf.settings.dev, so prod.py was never imported by anything
until it reached Cloud Run — where a mistake in it is a site that will not boot.
These import the real module with stubbed environment and assert the handful of
properties that are expensive to get wrong.

Not a general test of Django configuration. Each assertion below corresponds to
something that has either broken in production or would not be noticed until it
had.
"""

import importlib

import pytest

REQUIRED_ENV = {
    "SECRET_KEY": "x",
    "ALLOWED_HOSTS": "example.com",
    "GCS_BUCKET_NAME": "assets-bucket",
    "GCS_MEDIA_BUCKET_NAME": "media-bucket",
    "MAILGUN_API_KEY": "k",
    "MAILGUN_SENDER_DOMAIN": "d",
    "GOOGLE_CLIENT_ID": "g",
    "GOOGLE_CLIENT_SECRET": "g",
    "APPLE_CLIENT_ID": "a",
    "APPLE_CLIENT_SECRET": "a",
    "APPLE_KEY_ID": "a",
    "APPLE_PRIVATE_KEY": "a",
    "WEBAUTHN_RP_ID": "r",
    "WEBAUTHN_ORIGIN": "o",
    "TASK_BASE_URL": "https://example.com",
    "TASK_SERVICE_ACCOUNT": "s@example.com",
    "LITESTREAM_GCS_BUCKET": "l",
    "TURNSTILE_SECRET_KEY": "t",
} | {
    f"SQID_SALT_{name}": "abcdefghijklmnopqrstuvwxyz0123456789"
    for name in (
        "CANDIDATE",
        "ELECTION",
        "PLAYER",
        "JURISDICTION",
        "MANUFACTURER",
        "PRODUCT",
        "STORE",
    )
}


@pytest.fixture
def prod(monkeypatch):
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    return importlib.reload(importlib.import_module("hf.settings.prod"))


def test_it_imports_at_all(prod) -> None:
    """The cheapest possible check, and the one that would have caught a
    settings typo before it became a container that will not start."""
    assert prod.DEBUG is False


def test_every_required_secret_is_declared_in_terraform(prod) -> None:
    """Terraform reads these as data sources, so one missing from Secret Manager
    fails `plan` before it can fail the app. Keeping the two lists in step is
    what stops a new setting reaching Cloud Run with nothing behind it."""
    import pathlib
    import re

    declared = set(
        re.findall(r'"([A-Z_]+)",', pathlib.Path("terraform/secrets.tf").read_text())
    )
    missing = {key for key in REQUIRED_ENV if key not in declared}
    assert not missing, f"required by prod.py but not in secrets.tf: {sorted(missing)}"


# ── Uploads must not land in the public bucket ────────────────────────────────


def test_media_and_static_use_different_buckets(prod) -> None:
    """The static bucket is public by design. Receipt images sharing it would be
    readable by anyone who guessed the path, and the paths are guessable."""
    default = prod.STORAGES["default"]["OPTIONS"]["bucket_name"]
    static = prod.STORAGES["staticfiles"]["OPTIONS"]["bucket_name"]

    assert default != static
    assert default == "media-bucket"
    assert static == "assets-bucket"


def test_uploads_are_not_served_with_unsigned_urls(prod) -> None:
    """Belt and braces: nothing calls `.url` on a receipt image today, but if
    something ever does it should fail closed rather than emit a public link."""
    assert prod.STORAGES["default"]["OPTIONS"]["querystring_auth"] is True


# ── Errors reach a human ──────────────────────────────────────────────────────


def test_unhandled_exceptions_are_mailed(prod) -> None:
    assert "mail_admins" in prod.LOGGING["loggers"]["django.request"]["handlers"]
    assert prod.ADMINS, "nobody would receive the mail"


def test_the_alert_mail_has_a_deliverable_sender(prod) -> None:
    """Django defaults SERVER_EMAIL to root@localhost, which Mailgun rejects —
    so the alert about the outage would itself silently fail."""
    assert prod.SERVER_EMAIL == prod.DEFAULT_FROM_EMAIL
    assert "@" in prod.SERVER_EMAIL
    assert "localhost" not in prod.SERVER_EMAIL


def test_routine_warnings_are_not_mailed(prod) -> None:
    """Mail arriving must mean something broke. Attaching the handler to the
    root logger would mail on every WARNING and train everyone to ignore it."""
    assert "mail_admins" not in prod.LOGGING["root"]["handlers"]
