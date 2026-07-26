"""Error alert email.

Added after /accounts/login/ returned 500 in production and the only way to find
out was a person clicking the link and saying so.

Two things are worth asserting: that an unhandled exception actually produces
mail, and that a repeated one does not produce it four hundred times. The second
matters as much as the first — an alert that floods gets filtered, and a filtered
alert looks like coverage while providing none.
"""

import logging
import logging.config

import pytest
from django.core import mail
from django.core.cache import cache
from django.http import HttpRequest
from django.test import Client
from django.urls import path

from core.logging import ThrottleDuplicates


def boom(request: HttpRequest) -> None:
    raise ValueError("something broke")


urlpatterns = [path("boom/", boom)]


ALERT_LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"throttle": {"()": "core.logging.ThrottleDuplicates"}},
    "handlers": {
        "mail_admins": {
            "class": "django.utils.log.AdminEmailHandler",
            "level": "ERROR",
            "include_html": False,
            "filters": ["throttle"],
        },
    },
    "loggers": {
        "django.request": {"handlers": ["mail_admins"], "level": "ERROR"},
    },
}


@pytest.fixture(autouse=True)
def clear_throttle():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def quiet_client() -> Client:
    """Lets the exception reach the logging machinery instead of the test."""
    return Client(raise_request_exception=False)


@pytest.fixture
def alerting(settings):
    settings.ADMINS = [("HF errors", "me@koreypeters.org")]
    settings.SERVER_EMAIL = "noreply@humanflourish.ing"
    settings.ROOT_URLCONF = "core.test_error_alerts"
    settings.LOGGING = ALERT_LOGGING
    logging.config.dictConfig(ALERT_LOGGING)
    yield
    logging.config.dictConfig(settings.LOGGING)


# ── It reports ────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_an_unhandled_exception_emails_the_admins(quiet_client, alerting) -> None:
    quiet_client.get("/boom/")

    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["me@koreypeters.org"]


@pytest.mark.django_db
def test_the_email_carries_the_traceback(quiet_client, alerting) -> None:
    """The whole reason for mailing from inside Django rather than relying on
    the Cloud Monitoring alert, which can only say that a 5xx happened."""
    quiet_client.get("/boom/")

    body = mail.outbox[0].body
    assert "ValueError" in body
    assert "something broke" in body
    assert "/boom/" in body


# ── It does not flood ─────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_the_same_error_repeated_sends_one_email(quiet_client, alerting) -> None:
    """A crawler on a broken URL should cost one alert, not one per request."""
    for _ in range(5):
        quiet_client.get("/boom/")

    assert len(mail.outbox) == 1


def test_a_different_failure_still_gets_through() -> None:
    """Throttling must not hide a genuinely new problem behind an old one."""
    throttle = ThrottleDuplicates()

    def record(exc_type: type[Exception], lineno: int) -> logging.LogRecord:
        r = logging.LogRecord(
            "django.request",
            logging.ERROR,
            "views.py",
            lineno,
            "boom",
            None,
            (exc_type, exc_type("x"), None),
        )
        return r

    assert throttle.filter(record(ValueError, 10)) is True
    assert throttle.filter(record(ValueError, 10)) is False
    # Same place, different exception.
    assert throttle.filter(record(KeyError, 10)) is True
    # Same exception, different place.
    assert throttle.filter(record(ValueError, 99)) is True


def test_it_fails_open_when_the_cache_is_unavailable(monkeypatch) -> None:
    """Redis being down must not silence error reporting. A duplicate email is a
    far smaller problem than a 500 nobody hears about."""

    def explode(*args: object, **kwargs: object) -> None:
        raise ConnectionError("redis is gone")

    monkeypatch.setattr("django.core.cache.cache.add", explode)
    rec = logging.LogRecord("django.request", logging.ERROR, "v.py", 1, "x", None, None)

    assert ThrottleDuplicates().filter(rec) is True
