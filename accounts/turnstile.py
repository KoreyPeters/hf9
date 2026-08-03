"""Cloudflare Turnstile, guarding signup.

Added after twenty bot accounts registered against a harvested email list and
each one caused us to mail a real stranger — see `plans/bot-signups.md`. The
accounts were the visible part; the outbound mail was the damage.

Two properties matter more than the mechanism:

**It fails closed.** A missing secret in production means every signup is
refused, not that every signup is waved through. An abuse control that silently
disables itself on a misconfiguration is worse than none, because nothing looks
wrong until you read the user table.

**And it fails closed only on signup.** The first version of this raised an
`Error` system check, which stopped the container booting at all — a missing
signup key taking down receipt processing and Polium with it. See
`check_turnstile_configured` for why that is now a warning.

**It is skipped in development, and only there.** The skip is conditioned on
`DEBUG`, so it cannot follow a missing environment variable into production.
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from django.core.checks import Warning, register

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
def check_turnstile_configured(app_configs: object, **kwargs: object) -> list[Warning]:
    """Say loudly that Turnstile is unconfigured. Do not stop the boot.

    A Warning rather than an Error, and the distinction was learned the hard
    way. `migrate` inherits `requires_system_checks = "__all__"` and runs at
    container start (`start.sh:10`), so an Error here is not advice — it exits
    non-zero, the container never listens on the port, and Cloud Run
    crash-loops the revision. That took a missing key for a signup widget and
    turned it into every part of the service failing to start: Polium, receipt
    processing, the task endpoints, all of it.

    Wrong trade. The blast radius of the guard has to be no larger than the
    thing it guards. So:

    * this warns, and the deploy proceeds;
    * `verify` still fails closed, so signup refuses rather than letting bots
      through;
    * the error it logs reaches `mail_admins` (see `LOGGING` in prod), so the
      first person to attempt a signup turns the misconfiguration into an email
      rather than a line in a log nobody is reading.

    Loud, contained, and detectable — which is what "fail closed" was supposed
    to buy in the first place.
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
        Warning(
            f"Turnstile is not configured: {', '.join(missing)} is empty. "
            "Signup will refuse every submission until this is set.",
            hint="Set TURNSTILE_SITE_KEY (tfvars) and TURNSTILE_SECRET_KEY "
            "(Secret Manager), then terraform apply.",
            id="accounts.W001",
        )
    ]
