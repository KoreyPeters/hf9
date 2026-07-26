"""Throttling for error alert email.

Django mails a full traceback on every unhandled exception, which is exactly
what you want for the first one and not what you want for the four hundredth. A
crawler walking a broken URL, or a dependency failing on every request, turns
error reporting into an inbox that gets muted — and a muted alert is worse than
none, because it still looks like coverage.

Keyed on where the error happened rather than on the request, so one broken view
sends one email however many people hit it, while a genuinely different failure
still gets through immediately.
"""

import hashlib
import logging

WINDOW_SECONDS = 600


class ThrottleDuplicates(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        from django.core.cache import cache

        exc = record.exc_info
        site = f"{record.pathname}:{record.lineno}"
        if exc and exc[0] is not None:
            # Group by exception type as well, so two different failures in one
            # view are not collapsed into one alert.
            site = f"{site}:{exc[0].__name__}"
        key = "alert:" + hashlib.sha1(site.encode()).hexdigest()

        try:
            # `add` is atomic and only succeeds when the key is absent, so
            # concurrent workers cannot each decide they are the first.
            return bool(cache.add(key, 1, timeout=WINDOW_SECONDS))
        except Exception:
            # Cache trouble must not swallow an error report. Fail open: a
            # duplicate email is a far smaller problem than a silent 500.
            return True
