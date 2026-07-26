"""Holds on suspicious purchases.

Two checks, both cheap and both deliberately blunt. The sophisticated version —
per-player risk tiers from percentile ranks across a dozen signals — cannot be
built yet, because the thresholds it needs can only be derived from real player
behaviour that does not exist. Guessing them now would produce controls
calibrated to an imagined population.

What these do instead is bound the obvious cases while that data accumulates.

Both **hold points, not receipts**. The receipt is still read, its products still
match, and it still counts toward ratings — the platform wants that data whether
or not the person supplying it turns out to be honest. Only the reward waits.
That asymmetry matters: a false positive that delays a payout is recoverable and
mildly annoying, where one that discards somebody's weekly shop is neither.

Deliberately absent: any retroactive clawback. Points are settled once, and a
hold that fires after payment would be reversing a reward the player has already
been told they earned.
"""

import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from .models import Purchase

logger = logging.getLogger(__name__)


def recent_submission_count(player: object, within_hours: int = 1) -> int:
    since = timezone.now() - timedelta(hours=within_hours)
    return Purchase.objects.filter(player=player, created_at__gte=since).count()


def exceeds_velocity(purchase: Purchase) -> bool:
    """Too many receipts from one player in an hour.

    Counts the current purchase, since it is already saved by the time
    processing runs. A shopper genuinely uploading a backlog will trip this and
    have their points released on review — which is the intended cost.
    """
    limit: int = settings.SPENDIUM["VELOCITY_LIMIT_PER_HOUR"]
    return recent_submission_count(purchase.player) > limit


def is_high_value(purchase: Purchase) -> bool:
    """A receipt large enough to be worth a human glance.

    Most legitimate ones — a big grocery shop, an appliance — clear review
    within a day and the player never notices. The threshold exists to bound
    what a single fabricated receipt could be worth.
    """
    threshold = Decimal(str(settings.SPENDIUM["HIGH_VALUE_HOLD"]))
    return purchase.total is not None and purchase.total >= threshold


def evaluate(purchase: Purchase) -> str:
    """Decide whether to hold this purchase's points. Returns the reason, or "".

    Velocity is checked first: someone submitting rapidly is a stronger signal
    than someone submitting once for a large amount, and only one reason is
    recorded so the review queue says something specific.
    """
    if purchase.hold_reason:
        return purchase.hold_reason

    reason = ""
    if exceeds_velocity(purchase):
        reason = Purchase.HOLD_VELOCITY
    elif is_high_value(purchase):
        reason = Purchase.HOLD_HIGH_VALUE

    if reason:
        purchase.hold_reason = reason
        purchase.held_at = timezone.now()
        purchase.save(update_fields=["hold_reason", "held_at"])
        logger.info("Held purchase %s for review: %s.", purchase.pk, reason)
    return reason


def release(purchase: Purchase) -> Decimal:
    """Clear a hold and pay out. Idempotent via the usual points guard."""
    from . import points

    purchase.hold_reason = ""
    purchase.held_at = None
    purchase.save(update_fields=["hold_reason", "held_at"])
    return points.award_for_purchase(purchase)


def held_purchases():
    """The review queue, oldest first — a held payout is a player waiting."""
    return (
        Purchase.objects.exclude(hold_reason="")
        .select_related("player", "store")
        .order_by("held_at")
    )
