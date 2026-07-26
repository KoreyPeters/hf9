"""Points for a purchase.

Spendium exists to reward ethical spending, so this is where the reward actually
happens. Everything before it — reading the receipt, matching the products,
asking the player — is machinery in service of getting this number right.

The formula follows the design:

    points per dollar = Σ (criterion value × criterion probability)

with **no ceiling**. That is the point: as more criteria are satisfied at higher
probabilities, points per dollar rises without limit, which is what drives the
ethical arms race. A rating expressed as a fraction would cap the reward at what
was spent and remove the incentive entirely.

Store and product contributions are additive, because they answer different
questions and the second is worth far more:

    store_points   = spend × ppd(store)                    where you shopped
    product_points = Σ (line total × ppd(product))         what you actually bought

`compute_declaration_points` already implements the Σ formula, complete with the
k-threshold, and Polium uses it for vote declarations. Reusing it means a
criterion value means the same thing in both games.
"""

import logging
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db.models import Sum

from points.service import award_points
from surveys.ratings import compute_declaration_points

from .models import Purchase, PurchaseLineItem

logger = logging.getLogger(__name__)


def eligible_spend(purchase: Purchase) -> Decimal:
    """The part of a receipt that earns.

    Negative lines — refunds and same-receipt returns — earn nothing. They are
    excluded rather than subtracted: a refund should not eat into points earned
    on the rest of the shop, and chasing return fraud is a deliberate
    non-priority at this stage.
    """
    total = purchase.line_items.filter(line_total__gt=0).aggregate(
        total=Sum("line_total")
    )["total"]
    return total or Decimal("0")


def store_points(purchase: Purchase) -> Decimal:
    """Earned for telling us where you shopped, and for how much."""
    if purchase.store is None:
        return Decimal("0")
    return eligible_spend(purchase) * compute_declaration_points(purchase.store)


def product_points(purchase: Purchase) -> Decimal:
    """Earned for telling us what you actually bought.

    Per line, because a receipt is a basket of different things and each carries
    its own rating. Unmatched or unrated lines contribute nothing here, but they
    still count toward the store component and the floor — a receipt full of
    products we cannot yet recognise is never worthless.
    """
    total = Decimal("0")
    lines = purchase.line_items.filter(line_total__gt=0).select_related("product")
    # Products repeat across a receipt, and each ppd costs a few queries.
    cache: dict[int, Decimal] = {}
    for line in lines:
        if line.product_id is None:
            continue
        if line.product_id not in cache:
            cache[line.product_id] = compute_declaration_points(line.product)
        total += line.line_total * cache[line.product_id]
    return total


def floor_points(purchase: Purchase) -> Decimal:
    """What participation is worth when nothing involved has been rated.

    Internet points are not scarce, and paying a genuine player nothing because
    the catalogue has not caught up yet is a worse failure than paying them
    something imprecise.
    """
    base = Decimal(str(settings.SPENDIUM["BASE_POINTS_PER_DOLLAR"]))
    return eligible_spend(purchase) * base


def verification_multiplier(purchase: Purchase) -> Decimal:
    """How the purchase was evidenced.

    This is what makes uploading a receipt worth doing on day one, before
    anything has been rated and while both rating components are still zero.
    """
    multipliers = settings.SPENDIUM["VERIFICATION_MULTIPLIERS"]
    return Decimal(str(multipliers.get(purchase.verification_method, "0.5")))


def calculate(purchase: Purchase) -> Decimal:
    """What this purchase is worth, before membership multipliers.

    The floor is applied with `max` rather than added, so it is overtaken by
    real ratings instead of permanently inflating them.
    """
    earned = store_points(purchase) + product_points(purchase)
    amount = max(floor_points(purchase), earned) * verification_multiplier(purchase)
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _describe(purchase: Purchase) -> str:
    """Context for the ledger entry, for as long as the purchase exists.

    Cleared when the purchase is anonymised. During the window this duplicates
    what the purchase row already holds, so it adds no retention; afterwards it
    would be the only remaining record of where and when somebody shopped, and
    years of those is a movement trace far more revealing than the basket it
    replaced.
    """
    store = purchase.store.name if purchase.store else "Unknown store"
    # The day is formatted separately because the strftime flag for an unpadded
    # day differs between platforms — %-d on Linux, %#d on Windows — and neither
    # works on the other.
    when = purchase.purchased_at
    return f"{store}, {when.day} {when:%B %Y}"


def award_for_purchase(purchase: Purchase) -> Decimal:
    """Pay out a purchase, once.

    `points_awarded` is the guard. Reprocessing, retro-matching and a redelivered
    Cloud Task must all be able to touch a purchase without paying for it again,
    and a flag on the row is a far more reliable check than hoping each caller
    remembers.

    Deliberately not gated on rating. Withholding points a player has already
    earned until they answer a survey would be withholding them for an action we
    rate-limit on purpose.
    """
    if purchase.points_awarded is not None:
        return Decimal("0")

    amount = calculate(purchase)
    if amount <= 0:
        # Still stamped, so an empty or all-refund receipt is settled rather
        # than being retried forever.
        purchase.points_awarded = Decimal("0")
        purchase.save(update_fields=["points_awarded"])
        return Decimal("0")

    awarded = award_points(
        purchase.player,
        amount,
        reason="purchase",
        source=purchase,
        description=_describe(purchase),
    )
    purchase.points_awarded = awarded
    purchase.save(update_fields=["points_awarded"])
    logger.info("Awarded %s points for purchase %s.", awarded, purchase.pk)
    return awarded


def negative_line_item_ids(purchase: Purchase) -> list[int]:
    """Lines excluded from earning. Kept for display, not for arithmetic."""
    return list(
        PurchaseLineItem.objects.filter(
            purchase=purchase, line_total__lt=0
        ).values_list("pk", flat=True)
    )
