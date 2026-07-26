"""Product ratings.

Reuses the generic survey engine — `SurveyResponse` attaches to any model
through a GenericForeignKey, so `Product` needed no registration. What this
module adds is the two things the generic engine cannot know about products:

**Merges.** A rating attaches to whichever record existed when it was given, so
aggregating a single product id would silently drop every rating left on a
record that has since been merged away. Aggregation spans the merge group.

**Two different gates.** A rating is shown once enough people have answered —
that is about the number meaning something. A rating is *published* as an
aggregate only once enough purchases sit behind it — that is about nobody being
able to reverse-engineer an individual basket from sparse data. They answer
different questions and neither substitutes for the other.
"""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db.models import Count, Q
from django.utils import timezone

from surveys.models import CriterionAnswer, SurveyConfig, SurveyResponse

from . import catalogue
from .models import AnonymisedLineItem, Product, PurchaseLineItem

RATING_WINDOW_DAYS = 365


@dataclass(frozen=True)
class ProductRating:
    """A product's rating and everything needed to judge how much it means."""

    score: Decimal | None
    response_count: int
    verified_count: int
    purchase_count: int
    displayable: bool
    publishable: bool

    @property
    def percentage(self) -> int | None:
        if self.score is None:
            return None
        return int(round(float(self.score) * 100))


def _responses(product: Product):
    """Every response for this product, across everything merged into it."""
    cutoff = timezone.now() - timedelta(days=RATING_WINDOW_DAYS)
    return SurveyResponse.objects.filter(
        content_type=ContentType.objects.get_for_model(Product),
        object_id__in=catalogue.merge_group_ids(product),
        submitted_at__gte=cutoff,
    )


def purchase_count(product: Product) -> int:
    """How many purchases sit behind this product, in both layers.

    Counts line items rather than ratings, because k-anonymity is about the
    baskets a published aggregate could expose, not about how many people
    happened to answer a survey.
    """
    ids = catalogue.merge_group_ids(product)
    return (
        PurchaseLineItem.objects.filter(product_id__in=ids).count()
        + AnonymisedLineItem.objects.filter(product_id__in=ids).count()
    )


def _publish_threshold(product: Product) -> int:
    sensitive = product.category is not None and product.category.is_sensitive
    key = "PUBLISH_K_SENSITIVE" if sensitive else "PUBLISH_K"
    return settings.SPENDIUM[key]


def compute(product: Product) -> ProductRating:
    """Aggregate a product's rating, weighting unverified responses lower.

    A receipt-anchored response is evidence the player actually bought the
    thing. An unverified one may be anyone with an opinion, which is worth
    something but not the same, and is what a manufacturer disputing a rating
    would attack first.

    Unverified responses can never clear the display threshold alone: the
    threshold counts verified responses only, so a product cannot be given a
    public rating by people who never bought it.
    """
    responses = list(_responses(product).values_list("pk", "is_verified"))
    if not responses:
        return ProductRating(
            score=None,
            response_count=0,
            verified_count=0,
            purchase_count=purchase_count(product),
            displayable=False,
            publishable=False,
        )

    verified_ids = {pk for pk, verified in responses if verified}
    unverified_weight = Decimal(str(settings.SPENDIUM["UNVERIFIED_RATING_WEIGHT"]))

    answers = CriterionAnswer.objects.filter(
        survey_response_id__in=[pk for pk, _ in responses],
        criterion__is_active=True,
    ).select_related("criterion")

    total_weight = Decimal("0")
    weighted_sum = Decimal("0")
    for answer in answers:
        weight = Decimal(str(answer.criterion.weight))
        if answer.survey_response_id not in verified_ids:
            weight *= unverified_weight
        total_weight += weight
        if answer.answer:
            weighted_sum += weight

    score = (weighted_sum / total_weight) if total_weight else None
    verified_count = len(verified_ids)
    purchases = purchase_count(product)

    return ProductRating(
        score=score,
        response_count=len(responses),
        verified_count=verified_count,
        purchase_count=purchases,
        displayable=(
            score is not None
            and verified_count >= SurveyConfig.get().min_survey_threshold
        ),
        publishable=(
            score is not None
            and verified_count >= SurveyConfig.get().min_survey_threshold
            and purchases >= _publish_threshold(product)
        ),
    )


def player_has_bought(player: object, product: Product) -> bool:
    """Whether this player has a live purchase containing this product.

    Only the player-linked layer counts. Once a purchase is anonymised there is
    deliberately no way to tell whose it was, which is the whole point — so
    verification is something that can only be established inside the window.
    """
    return PurchaseLineItem.objects.filter(
        purchase__player=player,
        product_id__in=catalogue.merge_group_ids(product),
    ).exists()


def rateable_products(player: object) -> list[Product]:
    """Products the player can rate: everything on their live purchases."""
    ids = (
        PurchaseLineItem.objects.filter(purchase__player=player, product__isnull=False)
        .values_list("product_id", flat=True)
        .distinct()
    )
    return list(
        Product.objects.filter(pk__in=ids).exclude(status=Product.STATUS_RETIRED)
    )


def manufacturer_rating(manufacturer: object) -> ProductRating:
    """Roll a manufacturer's products up into one figure.

    The manufacturer is the entity accountability actually attaches to — it is
    what a pressure campaign addresses — so this aggregates across products
    rather than averaging their scores, which would let a manufacturer dilute a
    bad product by selling many uncontroversial ones.
    """
    products = list(
        Product.objects.filter(manufacturer=manufacturer).exclude(
            status=Product.STATUS_RETIRED
        )
    )
    if not products:
        return ProductRating(None, 0, 0, 0, False, False)

    total_weight = Decimal("0")
    weighted_sum = Decimal("0")
    responses = verified = purchases = 0

    for product in products:
        rating = compute(product)
        if rating.score is None:
            continue
        # Weight by how much evidence each product carries, so a product with
        # forty responses counts for more than one with five.
        weight = Decimal(rating.verified_count or rating.response_count)
        total_weight += weight
        weighted_sum += rating.score * weight
        responses += rating.response_count
        verified += rating.verified_count
        purchases += rating.purchase_count

    score = (weighted_sum / total_weight) if total_weight else None
    return ProductRating(
        score=score,
        response_count=responses,
        verified_count=verified,
        purchase_count=purchases,
        displayable=score is not None
        and verified >= SurveyConfig.get().min_survey_threshold,
        publishable=score is not None
        and verified >= SurveyConfig.get().min_survey_threshold
        and purchases >= settings.SPENDIUM["PUBLISH_K"],
    )


def snapshot_all() -> int:
    """Record today's rating for every product with one. Returns how many.

    Trend needs history, and a rating recomputed from a rolling window cannot be
    reconstructed after the fact — responses age out of it. Snapshots are the
    only way to know later what a rating was at the time.
    """
    from .models import ProductRatingSnapshot

    today = timezone.now().date()
    written = 0
    for product in Product.objects.exclude(status=Product.STATUS_RETIRED).iterator():
        rating = compute(product)
        if rating.score is None:
            continue
        ProductRatingSnapshot.objects.update_or_create(
            product=product,
            taken_on=today,
            defaults={
                "score": rating.score,
                "response_count": rating.response_count,
                "verified_count": rating.verified_count,
            },
        )
        written += 1
    return written


def trend(product: Product, months: int = 24) -> list[tuple[str, Decimal]]:
    """Snapshots for a sparkline, oldest first."""
    from .models import ProductRatingSnapshot

    cutoff = (timezone.now() - timedelta(days=months * 31)).date()
    rows = ProductRatingSnapshot.objects.filter(
        product_id__in=catalogue.merge_group_ids(product), taken_on__gte=cutoff
    ).order_by("taken_on")
    return [(row.taken_on.isoformat(), row.score) for row in rows]


def response_breakdown(product: Product) -> dict[str, int]:
    """Verified against unverified, for showing what a rating rests on."""
    counts = _responses(product).aggregate(
        verified=Count("pk", filter=Q(is_verified=True)),
        unverified=Count("pk", filter=Q(is_verified=False)),
    )
    return {
        "verified": counts["verified"] or 0,
        "unverified": counts["unverified"] or 0,
    }
