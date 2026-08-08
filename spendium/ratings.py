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
from .models import AnonymisedLineItem, Product, Purchase, PurchaseLineItem, Store

RATING_WINDOW_DAYS = 365


@dataclass(frozen=True)
class SubjectRating:
    """A rating and everything needed to judge how much it means.

    Shared between products and stores. `purchase_count` and `publishable` are
    product concerns — a sparse published aggregate could expose an individual
    basket — but they stay on the shared shape so a store gate can be added
    later without changing every call site. For stores `publishable` simply
    equals `displayable`; see `compute_store`.
    """

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


# The old name, kept because it reads better at product call sites and because
# renaming it everywhere would be churn with no reader benefit.
ProductRating = SubjectRating


def _weighted_score(responses: list[tuple[int, bool]]) -> tuple[Decimal | None, int]:
    """The verified/unverified weighted mean, shared by products and stores.

    An unverified response is somebody with an opinion rather than a receipt,
    and that discount means the same thing whichever subject is being rated —
    so it lives in one place rather than being reimplemented per subject.

    Returns the score and the number of verified responses behind it.
    """
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
    return score, len(verified_ids)


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


def _min_responses() -> int:
    """How many verified responses before a rating is worth showing.

    Spendium's own knob rather than `SurveyConfig.min_survey_threshold`, which
    Polium also reads to decide which criteria count toward its points. The two
    games start at different times with different amounts of data, and the
    number that lets a new catalogue show anything at all is not the number that
    should govern an established one's payouts.

    Admin-editable rather than a settings constant: it is meant to be ratcheted
    up as players arrive, and needing a deploy each time it moves is how a
    bootstrapping value stays wrong for a year. Zero means follow the shared
    value, so nothing changes until it is deliberately set.
    """
    from .models import MatchConfig

    configured = MatchConfig.get().min_rating_responses
    if configured:
        return configured
    return SurveyConfig.get().min_survey_threshold


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

    The display threshold counts **all** responses, not just verified ones.
    It used to count verified only, which was coherent while a purchase was
    required to rate at all — but anyone signed in may now rate anything, and
    counting verified alone would mean a product twenty people had rated showed
    nothing until somebody uploaded a receipt. The score is already weighted, so
    an unverified response appears in the number having counted for less.
    """
    responses = list(_responses(product).values_list("pk", "is_verified"))
    if not responses:
        return SubjectRating(
            score=None,
            response_count=0,
            verified_count=0,
            purchase_count=purchase_count(product),
            displayable=False,
            publishable=False,
        )

    score, verified_count = _weighted_score(responses)
    purchases = purchase_count(product)
    # Looked up once. Called twice it doubled the config query, which is
    # invisible for one product and linear for anything looping over many.
    threshold = _min_responses()
    enough_responses = score is not None and len(responses) >= threshold

    return SubjectRating(
        score=score,
        response_count=len(responses),
        verified_count=verified_count,
        purchase_count=purchases,
        displayable=enough_responses,
        publishable=enough_responses and purchases >= _publish_threshold(product),
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


# ── Stores ────────────────────────────────────────────────────────────────────


def _store_responses(store: Store):
    """Every response for this store inside the rating window.

    No merge group, unlike products. `Store` has no `merged_into` and stores are
    deduplicated only by case-insensitive printed name, so "LOBLAWS" and
    "LOBLAWS #1234" accumulate separate ratings and pay different rates for the
    same chain. That is a known limit rather than an oversight — item 8 in
    plans/operational-debt.md — and rating stores is what makes it cost real
    points rather than merely looking untidy.
    """
    cutoff = timezone.now() - timedelta(days=RATING_WINDOW_DAYS)
    return SurveyResponse.objects.filter(
        content_type=ContentType.objects.get_for_model(Store),
        object_id=store.pk,
        submitted_at__gte=cutoff,
    )


def compute_store(store: Store) -> SubjectRating:
    """A retailer's rating.

    **No publish gate.** For products the purchase-count threshold stops a
    sparse aggregate exposing an individual basket; "people who shopped at
    Loblaws think X" carries no such risk, and a k-anonymity gate here would
    contradict the decision to show a rating from the first response. So
    `publishable` equals `displayable`. The field stays on the shared shape so
    products are unaffected and a store gate can be added later if this turns
    out to be wrong.

    `purchase_count` is reported as zero rather than counted: nothing consumes
    it for stores, and counting every line item at a chain would be an expensive
    answer to a question nobody asks.
    """
    responses = list(_store_responses(store).values_list("pk", "is_verified"))
    if not responses:
        return SubjectRating(
            score=None,
            response_count=0,
            verified_count=0,
            purchase_count=0,
            displayable=False,
            publishable=False,
        )

    score, verified_count = _weighted_score(responses)
    enough_responses = score is not None and len(responses) >= _min_responses()

    return SubjectRating(
        score=score,
        response_count=len(responses),
        verified_count=verified_count,
        purchase_count=0,
        displayable=enough_responses,
        publishable=enough_responses,
    )


def store_points_per_dollar(store: Store) -> Decimal:
    """What shopping here is worth, per dollar spent.

    The figure the design wants shown prominently: "29 points per dollar versus
    4" is the comparison that moves behaviour, where a percentage is a
    judgement. Uncapped by design — see `surveys.ratings.compute_declaration_points`.
    """
    from surveys.ratings import compute_declaration_points

    return compute_declaration_points(store)


def player_has_shopped_at(player: object, store: Store) -> bool:
    """Whether this response is anchored to a receipt.

    Not an eligibility check — anyone signed in may rate any store. This decides
    `is_verified`, which is what the weighting acts on.

    Only the player-linked layer counts, for the same reason as products: once a
    purchase is anonymised there is deliberately no way to tell whose it was.
    """
    return Purchase.objects.filter(player=player, store=store).exists()


def manufacturer_rating(manufacturer: object) -> ProductRating:
    """Roll a manufacturer's products up into one figure.

    The manufacturer is the entity accountability actually attaches to — it is
    what a pressure campaign addresses — so this aggregates across products
    rather than averaging their scores, which would let a manufacturer dilute a
    bad product by selling many uncontroversial ones.

    **Not yet fit for a view.** This issues a handful of queries per product, so
    a manufacturer with two hundred products costs well over a thousand. Nothing
    calls it outside tests today; batching the per-product work is a
    prerequisite for putting it on a page.
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
    threshold = _min_responses()
    enough_responses = score is not None and verified >= threshold
    return ProductRating(
        score=score,
        response_count=responses,
        verified_count=verified,
        purchase_count=purchases,
        displayable=enough_responses,
        publishable=enough_responses and purchases >= settings.SPENDIUM["PUBLISH_K"],
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
                "purchase_count": rating.purchase_count,
            },
        )
        written += 1

    prune_snapshots()
    return written


def snapshot_all_stores() -> int:
    """Record today's rating and payout rate for every store with one.

    Skips stores with no responses rather than writing zeroes, so a chain nobody
    has rated leaves no trend line at all — which is honest, where a flat line at
    zero would read as "rated badly" rather than "not rated".
    """
    from .models import StoreRatingSnapshot

    today = timezone.now().date()
    written = 0
    for store in Store.objects.iterator():
        rating = compute_store(store)
        if rating.score is None:
            continue
        StoreRatingSnapshot.objects.update_or_create(
            store=store,
            taken_on=today,
            defaults={
                "score": rating.score,
                "response_count": rating.response_count,
                "verified_count": rating.verified_count,
                "points_per_dollar": store_points_per_dollar(store),
            },
        )
        written += 1
    return written


def prune_snapshots() -> int:
    """Drop snapshots older than the trend window can display.

    Pruned by the same task that writes them, so retention cannot drift away
    from the thing producing the rows.
    """
    from .models import ProductRatingSnapshot

    days: int = settings.SPENDIUM["SNAPSHOT_RETENTION_DAYS"]
    cutoff = (timezone.now() - timedelta(days=days)).date()
    removed, _ = ProductRatingSnapshot.objects.filter(taken_on__lt=cutoff).delete()
    return removed


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


def top_rated(limit: int = 12) -> list:
    """Publishable products, best first, for a public listing.

    Read from the daily snapshots rather than computed live. `compute` is a
    handful of queries per product, which is fine on a product page and is not
    fine on a listing — and the snapshot already holds everything the two gates
    need.

    Both gates are applied here, not just the display one. A listing is an
    aggregate publication, so a product bought by too few people to be
    anonymous must not appear on it however good its score.
    """
    from django.db.models import Max, Q

    from .models import Product, ProductRatingSnapshot

    latest = ProductRatingSnapshot.objects.aggregate(Max("taken_on"))["taken_on__max"]
    if latest is None:
        return []

    sensitive = Q(product__category__is_sensitive=True)
    return list(
        ProductRatingSnapshot.objects.filter(taken_on=latest)
        .filter(verified_count__gte=_min_responses())
        .filter(
            # Sensitive categories carry a higher bar. A null category is not
            # sensitive, so it falls to the ordinary threshold.
            (
                sensitive
                & Q(purchase_count__gte=settings.SPENDIUM["PUBLISH_K_SENSITIVE"])
            )
            | (~sensitive & Q(purchase_count__gte=settings.SPENDIUM["PUBLISH_K"]))
        )
        .exclude(product__status=Product.STATUS_RETIRED)
        .select_related("product", "product__category")
        .order_by("-score")[:limit]
    )
