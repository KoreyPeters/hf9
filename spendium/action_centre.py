"""The Action Centre: everything a player has outstanding, in one place.

Three sections, in descending order of why anyone should care:

1. **Hot products they bought** — something has happened to a product they own.
2. **Unresolved disambiguations** — we could not tell what they bought.
3. **Unrated products** — everything else still rateable in their window.

This is the engagement surface. The navbar badge and the weekly email exist only
to bring people here; neither tries to reproduce it, because a notification that
contains the whole answer is one nobody clicks through.

Everything is derived rather than stored. The one thing that is persisted is
what the player has already *seen* — novelty is a fact about a person, not about
an item, so it cannot live on the items.

The anonymisation boundary applies without exception. A product is only ever
surfaced to a player whose live purchase contains it, which means the Action
Centre empties itself as purchases age out — by design, not by omission.
"""

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Count, Max
from django.utils import timezone

from surveys.models import SurveyResponse

from .models import (
    ActionCentreState,
    Product,
    Purchase,
    PurchaseLineItem,
)


@dataclass(frozen=True)
class ActionCentre:
    hot: list[Product] = field(default_factory=list)
    disambiguations: list[PurchaseLineItem] = field(default_factory=list)
    unrated: list[Product] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.hot) + len(self.disambiguations) + len(self.unrated)

    @property
    def is_empty(self) -> bool:
        return self.total == 0


def _live_line_items(player: object):
    """Line items on purchases still linked to this player.

    The whole Action Centre hangs off this. Once a purchase is anonymised it is
    nobody's, so nothing from it can be surfaced to anyone.
    """
    return PurchaseLineItem.objects.filter(purchase__player=player)


def hot_products(player: object) -> list[Product]:
    """Hot products this player actually bought, most recently flagged first.

    Restricted to their own purchases on purpose. A general "here is what is
    contentious this week" feed would be a different feature; this one is about
    things the player has a stake in.
    """
    product_ids = (
        _live_line_items(player)
        .filter(product__isnull=False)
        .values_list("product_id", flat=True)
        .distinct()
    )
    return list(
        Product.objects.filter(pk__in=product_ids, hot_since__isnull=False)
        .exclude(status=Product.STATUS_RETIRED)
        .order_by("-hot_since")
    )


def unresolved_disambiguations(player: object) -> list[PurchaseLineItem]:
    """Lines we could not confidently identify, least certain first."""
    return list(
        _live_line_items(player)
        .filter(disambiguation_state=PurchaseLineItem.STATE_PENDING)
        .select_related("product", "purchase__store")
        .order_by("match_confidence", "pk")
    )


def unrated_products(player: object) -> list[Product]:
    """Rateable products this player has not rated yet, newest purchase first."""
    rated_ids = set(
        SurveyResponse.objects.filter(
            player=player,
            content_type__app_label="spendium",
            content_type__model="product",
        ).values_list("object_id", flat=True)
    )
    rows = (
        _live_line_items(player)
        .filter(product__isnull=False)
        .exclude(product_id__in=rated_ids)
        .values("product_id")
        .annotate(bought=Max("purchase__purchased_at"))
        .order_by("-bought")
    )
    ordered_ids = [row["product_id"] for row in rows]
    if not ordered_ids:
        return []

    products = {
        p.pk: p
        for p in Product.objects.filter(pk__in=ordered_ids).exclude(
            status=Product.STATUS_RETIRED
        )
    }
    return [products[pk] for pk in ordered_ids if pk in products]


def build(player: object) -> ActionCentre:
    return ActionCentre(
        hot=hot_products(player),
        disambiguations=unresolved_disambiguations(player),
        unrated=unrated_products(player),
    )


# ── The badge ─────────────────────────────────────────────────────────────────


def new_item_count(player: object) -> int:
    """How many items have appeared since the player last looked.

    Counted by when things arrived rather than by what is outstanding, so the
    badge means "something new" rather than "you still have not done this". A
    badge that never clears is one people learn to ignore, taking the genuinely
    new with it.
    """
    state = ActionCentreState.get_for(player)
    since = state.last_visited_at
    if since is None:
        return build(player).total

    newly_hot = (
        Product.objects.filter(
            pk__in=_live_line_items(player).values_list("product_id", flat=True),
            hot_since__gt=since,
        )
        .exclude(status=Product.STATUS_RETIRED)
        .count()
    )
    # A newly uploaded receipt makes its products rateable and may bring
    # disambiguations with it, so new line items count as new items.
    new_lines = _live_line_items(player).filter(purchase__created_at__gt=since).count()
    return newly_hot + new_lines


def mark_visited(player: object) -> None:
    from .context_processors import invalidate

    state = ActionCentreState.get_for(player)
    state.last_visited_at = timezone.now()
    state.save(update_fields=["last_visited_at"])
    # The one moment the badge must be right is the moment it becomes zero.
    invalidate(player.pk)


# ── Hotness ───────────────────────────────────────────────────────────────────


def _trending_product_ids() -> set[int]:
    """Products being bought unusually often across the whole player base."""
    days: int = settings.SPENDIUM["HOT_TRENDING_DAYS"]
    threshold: int = settings.SPENDIUM["HOT_TRENDING_PURCHASES"]
    since = timezone.now() - timedelta(days=days)
    rows = (
        PurchaseLineItem.objects.filter(
            product__isnull=False, purchase__purchased_at__gte=since
        )
        .values("product_id")
        .annotate(n=Count("pk"))
        .filter(n__gte=threshold)
    )
    return {row["product_id"] for row in rows}


def _rating_moved_product_ids() -> set[int]:
    """Products whose rating has shifted sharply either way.

    Uses the daily snapshots, which is the only way to know what a rating was
    a month ago — the rolling window means it cannot be recomputed after the
    fact.
    """
    from .models import ProductRatingSnapshot

    move = Decimal(str(settings.SPENDIUM["HOT_RATING_MOVE"]))
    window: int = settings.SPENDIUM["HOT_RATING_WINDOW_DAYS"]
    cutoff = (timezone.now() - timedelta(days=window)).date()

    moved = set()
    snapshots = ProductRatingSnapshot.objects.filter(taken_on__gte=cutoff).order_by(
        "product_id", "taken_on"
    )
    by_product: dict[int, list[Decimal]] = {}
    for snapshot in snapshots:
        by_product.setdefault(snapshot.product_id, []).append(snapshot.score)
    for product_id, scores in by_product.items():
        if len(scores) < 2:
            continue
        if abs(scores[-1] - scores[0]) >= move:
            moved.add(product_id)
    return moved


def recompute_hotness() -> int:
    """Refresh which products are hot. Returns how many are.

    Manual flags are left entirely alone. An admin sets one for a recall or a
    safety event — precisely the situations no purchase-volume metric will have
    noticed yet — so a nightly recompute must not be able to clear it.
    """
    now = timezone.now()
    duration: int = settings.SPENDIUM["HOT_DURATION_DAYS"]
    expiry = now - timedelta(days=duration)

    trending = _trending_product_ids()
    moved = _rating_moved_product_ids()

    # Expire computed flags that have had their run.
    Product.objects.filter(hot_is_manual=False, hot_since__lt=expiry).exclude(
        pk__in=trending | moved
    ).update(hot_since=None, hot_reason="")

    for ids, reason in (
        (trending, Product.HOT_TRENDING),
        (moved, Product.HOT_RATING_MOVED),
    ):
        # Only newly hot products get a fresh timestamp, so "most recently
        # flagged first" keeps meaning something across runs.
        Product.objects.filter(pk__in=ids, hot_since__isnull=True).update(
            hot_since=now, hot_reason=reason
        )

    return Product.objects.filter(hot_since__isnull=False).count()


def flag_hot(product: Product, manual: bool = True) -> None:
    """Mark a product hot by hand, for a recall or safety event."""
    product.hot_since = timezone.now()
    product.hot_reason = Product.HOT_MANUAL if manual else product.hot_reason
    product.hot_is_manual = manual
    product.save(update_fields=["hot_since", "hot_reason", "hot_is_manual"])


def clear_hot(product: Product) -> None:
    product.hot_since = None
    product.hot_reason = ""
    product.hot_is_manual = False
    product.save(update_fields=["hot_since", "hot_reason", "hot_is_manual"])


# ── Email ─────────────────────────────────────────────────────────────────────


def email_due(player: object) -> str | None:
    """Which email, if any, this player should get. None means stay quiet.

    Follows the design's algorithm. The important part is what does *not*
    qualify: unrated products and unresolved disambiguations never trigger an
    email. They are surfaced passively, by the badge, for people already using
    the product. Emailing about routine housekeeping is how a mailing list
    teaches people to ignore it — and then the one email that mattered goes
    unread too.
    """
    state = ActionCentreState.get_for(player)
    if not state.emails_enabled:
        return None

    if state.onboarding_emails_sent < settings.SPENDIUM["ONBOARDING_EMAILS"]:
        # Fixed schedule regardless of content: these exist so players learn the
        # Action Centre is there at all.
        return "onboarding"

    gap: int = settings.SPENDIUM["EMAIL_MIN_GAP_DAYS"]
    if state.last_email_at and state.last_email_at > timezone.now() - timedelta(
        days=gap
    ):
        return None

    if hot_products(player):
        return "hot"
    return None


def record_email_sent(player: object, kind: str) -> None:
    state = ActionCentreState.get_for(player)
    state.last_email_at = timezone.now()
    if kind == "onboarding":
        state.onboarding_emails_sent += 1
    state.save(update_fields=["last_email_at", "onboarding_emails_sent"])


def players_with_live_purchases():
    """Only people with something to be told about."""
    from django.contrib.auth import get_user_model

    ids = Purchase.objects.values_list("player_id", flat=True).distinct()
    return get_user_model().objects.filter(pk__in=ids)


def rateable_summary(player: object) -> dict[str, int]:
    """Counts for the badge tooltip and the email body."""
    centre = build(player)
    return {
        "hot": len(centre.hot),
        "disambiguations": len(centre.disambiguations),
        "unrated": len(centre.unrated),
        "total": centre.total,
    }
