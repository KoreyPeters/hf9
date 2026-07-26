"""Measurements of whether the system is actually converging.

The premise of the whole design is that it gets better without curation. That
claim is unfalsifiable without numbers, and the failure mode it hides is
stagnation that looks like progress — a catalogue growing steadily while the
prompt rate never falls.
"""

from dataclasses import dataclass

from django.db.models import Count, Q

from .models import MatchTier, ProductAlias, PurchaseLineItem


@dataclass(frozen=True)
class AdjudicationAccuracy:
    """How often Tier 2 was right, judged by the people who did the shopping.

    Only aliases players have actually ruled on are counted. An adjudication
    nobody has confirmed or contradicted is not evidence either way, and
    treating silence as agreement would flatter the model exactly where it is
    least reliable — the long tail nobody buys twice.
    """

    confirmed: int
    contradicted: int

    @property
    def judged(self) -> int:
        return self.confirmed + self.contradicted

    @property
    def accuracy(self) -> float | None:
        """None when no player has ruled yet, rather than a misleading zero."""
        if not self.judged:
            return None
        return self.confirmed / self.judged


def adjudication_accuracy() -> AdjudicationAccuracy:
    counts = ProductAlias.objects.filter(
        source=ProductAlias.SOURCE_ADJUDICATION
    ).aggregate(
        confirmed=Count("pk", filter=Q(confirmation_count__gt=0)),
        contradicted=Count("pk", filter=Q(contradiction_count__gt=0)),
    )
    return AdjudicationAccuracy(
        confirmed=counts["confirmed"] or 0,
        contradicted=counts["contradicted"] or 0,
    )


def tier_distribution() -> dict[str, int]:
    """How much work each tier of the cascade is carrying.

    The headline convergence signal is the share resolved at Tier 0: exact
    alias hits are free, deterministic and need no one's attention, so a rising
    share means the system is learning. A flat one means it is not, however
    much the catalogue has grown.
    """
    counts = dict(
        PurchaseLineItem.objects.values_list("match_tier")
        .annotate(total=Count("pk"))
        .values_list("match_tier", "total")
    )
    return {tier.value: counts.get(tier.value, 0) for tier in MatchTier}


def prompt_rate() -> float | None:
    """Share of line items asking the player to disambiguate.

    Watched alongside completion rate rather than alone. A low prompt rate is
    good; a high one that players ignore is worse than useless, because the
    items still look unresolved while the attention has already been spent.
    """
    total = PurchaseLineItem.objects.count()
    if not total:
        return None
    pending = PurchaseLineItem.objects.filter(
        disambiguation_state=PurchaseLineItem.STATE_PENDING
    ).count()
    return pending / total


# ── Convergence ───────────────────────────────────────────────────────────────


def alias_hit_rate(store: object | None = None) -> float | None:
    """Share of line items resolved by an exact alias — the headline metric.

    Tier 0 hits are free, deterministic and need nobody's attention, so a rising
    share means the system is learning. A flat one means it is not, however much
    the catalogue has grown. Restrictable to a store because each chain's
    strings are learned separately, and an overall average hides a chain that is
    not converging at all.
    """
    lines = PurchaseLineItem.objects.all()
    if store is not None:
        lines = lines.filter(purchase__store=store)
    total = lines.count()
    if not total:
        return None
    return lines.filter(match_tier=MatchTier.ALIAS).count() / total


def prompt_completion_rate() -> float | None:
    """Share of prompts a player actually answered.

    Watched alongside the prompt rate, never instead of it. A low prompt rate is
    good; a high one that players ignore is worse than useless, because the
    items still look unresolved while the attention has already been spent.
    """
    asked = PurchaseLineItem.objects.filter(
        disambiguation_state__in=[
            PurchaseLineItem.STATE_PENDING,
            PurchaseLineItem.STATE_RESOLVED,
        ]
    ).count()
    if not asked:
        return None
    return (
        PurchaseLineItem.objects.filter(
            disambiguation_state=PurchaseLineItem.STATE_RESOLVED
        ).count()
        / asked
    )


def new_record_rate() -> float | None:
    """Unverified products per line item recorded.

    Should fall. A rate that stays flat means every receipt is still inventing
    products, which is duplicate fragmentation accumulating rather than a
    catalogue forming.
    """
    from .models import Product

    lines = PurchaseLineItem.objects.count()
    if not lines:
        return None
    return Product.objects.filter(status=Product.STATUS_UNVERIFIED).count() / lines


def auto_merge_rate() -> float | None:
    """Share of products that have been merged away.

    Should stay flat rather than grow. Growth means duplicates are being created
    faster than clustering prevents them, and the admin queue is next.
    """
    from .models import Product

    total = Product.objects.count()
    if not total:
        return None
    return Product.objects.filter(status=Product.STATUS_RETIRED).count() / total


def alias_demotion_rate() -> float | None:
    """Share of aliases players have contradicted — the poisoning detector.

    A wrong alias is applied silently and confidently to every future receipt
    carrying its string, so this is the one rate whose rise is unambiguously
    bad.
    """
    total = ProductAlias.objects.count()
    if not total:
        return None
    return (
        ProductAlias.objects.filter(status=ProductAlias.STATUS_DEMOTED).count() / total
    )


def summary() -> dict[str, object]:
    """Everything at once, for an admin glance or a management command."""
    accuracy = adjudication_accuracy()
    return {
        "alias_hit_rate": alias_hit_rate(),
        "tier_distribution": tier_distribution(),
        "prompt_rate": prompt_rate(),
        "prompt_completion_rate": prompt_completion_rate(),
        "new_record_rate": new_record_rate(),
        "auto_merge_rate": auto_merge_rate(),
        "alias_demotion_rate": alias_demotion_rate(),
        "adjudication_accuracy": accuracy.accuracy,
        "adjudications_judged": accuracy.judged,
    }


# ── Daily snapshots ───────────────────────────────────────────────────────────


def _counts_for(lines) -> dict[str, int]:
    from django.db.models import Count as _Count

    by_tier = dict(
        lines.values_list("match_tier")
        .annotate(n=_Count("pk"))
        .values_list("match_tier", "n")
    )
    by_state = dict(
        lines.values_list("disambiguation_state")
        .annotate(n=_Count("pk"))
        .values_list("disambiguation_state", "n")
    )
    return {
        "line_items": lines.count(),
        "alias_hits": by_tier.get(MatchTier.ALIAS, 0),
        "fuzzy_matches": by_tier.get(MatchTier.FUZZY, 0),
        "adjudicated": by_tier.get(MatchTier.ADJUDICATED, 0),
        "player_resolved": by_tier.get(MatchTier.PLAYER, 0),
        "unmatched": by_tier.get(MatchTier.UNMATCHED, 0),
        "prompts_pending": by_state.get(PurchaseLineItem.STATE_PENDING, 0),
        "prompts_resolved": by_state.get(PurchaseLineItem.STATE_RESOLVED, 0),
    }


def take_snapshot() -> int:
    """Record today's numbers, platform-wide and per store. Returns rows written.

    Recorded rather than derived on demand because the question is whether these
    rates are *moving*, and a rate computed today says nothing about that.
    """
    from django.utils import timezone

    from .models import MetricsSnapshot, Product, Store

    today = timezone.now().date()
    catalogue_counts = {
        "unverified_products": Product.objects.filter(
            status=Product.STATUS_UNVERIFIED
        ).count(),
        "retired_products": Product.objects.filter(
            status=Product.STATUS_RETIRED
        ).count(),
        "demoted_aliases": ProductAlias.objects.filter(
            status=ProductAlias.STATUS_DEMOTED
        ).count(),
        "aliases_needing_review": ProductAlias.objects.filter(
            needs_review=True
        ).count(),
    }

    written = 0
    MetricsSnapshot.objects.update_or_create(
        taken_on=today,
        store=None,
        defaults={**_counts_for(PurchaseLineItem.objects.all()), **catalogue_counts},
    )
    written += 1

    # Per store, because convergence is per retailer — each chain's strings are
    # learned separately.
    for store in Store.objects.all().iterator():
        lines = PurchaseLineItem.objects.filter(purchase__store=store)
        if not lines.exists():
            continue
        MetricsSnapshot.objects.update_or_create(
            taken_on=today, store=store, defaults=_counts_for(lines)
        )
        written += 1

    prune_snapshots()
    return written


def prune_snapshots() -> int:
    """Drop snapshots past the retention window. Same reasoning as ratings."""
    from datetime import timedelta

    from django.conf import settings as django_settings
    from django.utils import timezone as django_timezone

    from .models import MetricsSnapshot

    days: int = django_settings.SPENDIUM["SNAPSHOT_RETENTION_DAYS"]
    cutoff = (django_timezone.now() - timedelta(days=days)).date()
    removed, _ = MetricsSnapshot.objects.filter(taken_on__lt=cutoff).delete()
    return removed
