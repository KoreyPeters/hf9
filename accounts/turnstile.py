"""Cloudflare Turnstile, guarding signup.

Added after twenty bot accounts registered against a harvested email list and
each one caused us to mail a real stranger — see `plans/bot-signups.md`. The
accounts were the visible part; the outbound mail was the damage.

Two properties matter more than the mechanism:

**It fails closed.** A missing secret in production means every signup is
refused, not that every signup is waved through. An abuse control that silently
disables itself on a misconfiguration is worse than none, because nothing looks
wrong until you read the user table. `check_turnstile_configured` makes the
misconfiguration loud at deploy time so that failing closed stays theoretical.

**It is skipped in development, and only there.** The skip is conditioned on
`DEBUG`, so it cannot follow a missing environment variable into production.
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from django.core.checks import Error, register

logger = logging.getLogger(__name__)

VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

# Short. This runs inside the signup request, and Cloudflare being slow should
# cost a player a retry rather than hold a worker open — there is one.
TIMEOUT_SECONDS = 5


def _siteverify(payload: dict) -> dict:
    """The network call, alone in a function so tests can replace it."""
    data = urllib.parse.urlencode(payload).encode()
    request = urllib.request.Request(VERIFY_URL, data=data)
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read())


def verify(token: str, remote_ip: str = "") -> bool:
    """Whether Cloudflare vouches for this submission.

    A network failure counts as a failure. The alternative — treating an
    unreachable Cloudflare as a pass — hands an attacker the bypass directly,
    since making a third party unreachable from our container is easier than
    solving the challenge.
    """
    secret = getattr(settings, "TURNSTILE_SECRET_KEY", "")
    if not secret:
        if settings.DEBUG:
            return True
        logger.error("Turnstile secret is not configured; refusing the signup.")
        return False

    if not token:
        return False

    payload = {"secret": secret, "response": token}
    if remote_ip:
        payload["remoteip"] = remote_ip

    try:
        result = _siteverify(payload)
    except urllib.error.URLError, TimeoutError, ValueError:
        logger.exception("Turnstile verification failed to complete.")
        return False

    if not result.get("success"):
        # Logged at info: a failed challenge is the system working, not a fault.
        logger.info(
            "Turnstile rejected a submission: %s", result.get("error-codes", [])
        )
        return False
    return True


@register()
def check_turnstile_configured(app_configs: object, **kwargs: object) -> list[Error]:
    """Refuse to start a production deploy with no Turnstile keys.

    This is what makes failing closed safe to choose. Without it, a missing
    secret would be discovered by players being unable to sign up; with it, the
    deploy never happens.
    """
    if settings.DEBUG:
        return []
    missing = [
        name
        for name in ("TURNSTILE_SITE_KEY", "TURNSTILE_SECRET_KEY")
        if not getattr(settings, name, "")
    ]
    if not missing:
        return []
    return [
        Error(
            f"Turnstile is not configured: {', '.join(missing)} is empty. "
            "Signup would refuse every submission.",
            id="accounts.E001",
        )
    ]
