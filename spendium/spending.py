"""The guard behind the emergency stop.

Enforced at the point where a model client is constructed, because that is the
only place money is actually committed and it is a choke point both callers pass
through. Checking anywhere earlier would leave a path open the day somebody adds
a third caller.

`process_receipt` checks separately and earlier, so a stopped system leaves
receipts pending rather than marking them failed. That distinction is what makes
the stop safe to pull for a short outage: a pending receipt simply waits and is
read when the stop clears, where a failed one costs the player an upload.

A long outage cannot be made invisible that way, because images are deleted 24
hours after upload no matter what. `uploads_paused` handles that case by
refusing new uploads once the stop has run that long.
"""

import logging
from datetime import timedelta

logger = logging.getLogger(__name__)


class SpendingStoppedError(RuntimeError):
    """The emergency stop is on. Nothing should be calling a model."""


def is_stopped() -> bool:
    from .models import EmergencyStop

    return EmergencyStop.get().is_stopped


def uploads_paused() -> bool:
    """True once the stop has outlived the image retention window.

    A receipt uploaded into a stop is only safe while its image survives, and
    images are deleted 24 hours after upload regardless — the commitment is
    published, and an outage is the worst possible reason to quietly hold player
    photos longer. So a stop that runs longer than that destroys everything
    uploaded into it.

    Rather than accept receipts we can already tell we will bin, uploads are
    refused once the stop has been on that long: the player keeps their photo
    and can come back. A short stop stays invisible to them, which is the point
    of queueing in the first place.
    """
    from django.conf import settings
    from django.utils import timezone

    from .models import EmergencyStop

    state = EmergencyStop.get()
    if not state.is_stopped or state.stopped_at is None:
        return False
    hours: int = settings.SPENDIUM["IMAGE_RETENTION_HOURS"]
    return timezone.now() - state.stopped_at >= timedelta(hours=hours)


def guard() -> None:
    """Raise if spending is stopped.

    Called from every `_client()`. A raise rather than a silent no-op: code that
    asked for a model client and got `None` would fail somewhere further along,
    with a message about the wrong thing entirely.
    """
    if is_stopped():
        logger.warning("Refused to build a model client: emergency stop is on.")
        raise SpendingStoppedError(
            "AI spending is stopped. Untick the emergency stop in admin to resume."
        )
