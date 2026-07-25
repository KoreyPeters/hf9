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
