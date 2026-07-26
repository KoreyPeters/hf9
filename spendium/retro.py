"""Re-running the matching cascade over line items already recorded.

This is what makes the system compound rather than merely accumulate. Every
alias a player confirms, every product added to the catalogue, improves matching
for receipts that were read months ago — with no image, no model call, and
nobody being asked anything.

It works only because matching is a separate stage. The receipt image is deleted
within 24 hours, so the stored raw text is the sole durable record of a purchase;
had matching happened inside the extraction call, its result would be welded to
an image that no longer exists and could never be revisited.

Anonymous line items are included deliberately. A purchase past its window is no
longer anybody's, but the string on it is still evidence about what that string
means, and the rating it feeds still counts.

Two things it must never do:

* **Overwrite what a player decided.** They held the product; no amount of later
  string similarity outranks that.
* **Alter settled points.** Points are awarded once and never retrospectively
  adjusted, so this only ever changes which product a line points at.
"""

import logging
from dataclasses import dataclass

from django.db.models import F
from django.utils import timezone

from . import matching
from .models import (
    AnonymisedLineItem,
    MatchConfig,
    MatchTier,
    PurchaseLineItem,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetroResult:
    examined: int = 0
    filled: int = 0
    strengthened: int = 0

    @property
    def changed(self) -> int:
        return self.filled + self.strengthened

    def __add__(self, other: "RetroResult") -> "RetroResult":
        return RetroResult(
            examined=self.examined + other.examined,
            filled=self.filled + other.filled,
            strengthened=self.strengthened + other.strengthened,
        )


def _should_skip(line: object) -> bool:
    """Whether this line is off limits.

    Player decisions are final. A line resolved by the person who bought it is
    the strongest evidence in the system, and re-deriving it from string
    similarity could only ever make it worse.
    """
    if line.match_tier == MatchTier.PLAYER:
        return True
    state = getattr(line, "disambiguation_state", None)
    return state == PurchaseLineItem.STATE_RESOLVED


def _retro_match_line(line: object, store: object, config: MatchConfig) -> str | None:
    """Re-match one line. Returns "filled", "strengthened", or None.

    Stamps `retro_checked_at` whatever the outcome, so the next run moves on to
    lines it has not seen. Without that, a batch smaller than the backlog would
    re-examine the same rows forever and the tail would never be reached.
    """
    if _should_skip(line):
        return None

    line.retro_checked_at = timezone.now()
    result = matching.match_line_item(
        line.raw_text, line.interpreted_name, store=store, config=config
    )
    if result.product is None:
        line.save(update_fields=["retro_checked_at"])
        return None

    had_product = line.product_id is not None

    # An existing match is only displaced by a Tier 0 hit. An exact alias is
    # backed by people having confirmed it; a fuzzy score is not, so re-scoring
    # alone is never grounds to change an answer already recorded.
    if had_product and result.tier != MatchTier.ALIAS:
        line.save(update_fields=["retro_checked_at"])
        return None
    if (
        had_product
        and result.product.pk == line.product_id
        and line.match_tier == MatchTier.ALIAS
    ):
        line.save(update_fields=["retro_checked_at"])
        return None  # already there

    line.product = result.product
    line.match_tier = result.tier
    line.match_confidence = result.confidence

    fields = ["product", "match_tier", "match_confidence", "retro_checked_at"]
    if hasattr(line, "disambiguation_state") and not result.needs_prompt:
        # A confident answer takes the line out of the prompt queue, which is
        # the point: backlog clears without anybody being asked.
        line.disambiguation_state = PurchaseLineItem.STATE_NOT_NEEDED
        fields.append("disambiguation_state")

    line.save(update_fields=fields)
    return "strengthened" if had_product else "filled"


def _run_over(queryset, store_getter, config: MatchConfig) -> RetroResult:
    examined = filled = strengthened = 0
    for line in queryset:
        examined += 1
        outcome = _retro_match_line(line, store_getter(line), config)
        if outcome == "filled":
            filled += 1
        elif outcome == "strengthened":
            strengthened += 1
    return RetroResult(examined=examined, filled=filled, strengthened=strengthened)


def _purchase_candidates(limit: int):
    """Player-linked lines still worth re-examining."""
    return (
        PurchaseLineItem.objects.exclude(match_tier=MatchTier.PLAYER)
        .exclude(disambiguation_state=PurchaseLineItem.STATE_RESOLVED)
        .select_related("purchase__store", "product")
        .order_by(F("retro_checked_at").asc(nulls_first=True), "pk")[:limit]
    )


def _anonymised_candidates(limit: int):
    """Lines whose purchase has passed its window.

    Still worth matching: the rating they feed is permanent, and the string is
    still evidence.
    """
    return (
        AnonymisedLineItem.objects.exclude(match_tier=MatchTier.PLAYER)
        .select_related("anonymised_purchase__store", "product")
        .order_by(F("retro_checked_at").asc(nulls_first=True), "pk")[:limit]
    )


def run(limit: int | None = None) -> RetroResult:
    """Re-match a batch of recorded line items against the current catalogue."""
    config = MatchConfig.get()
    limit = limit or config.retro_batch_size

    result = _run_over(
        _purchase_candidates(limit), lambda line: line.purchase.store, config
    )
    result = result + _run_over(
        _anonymised_candidates(limit),
        lambda line: line.anonymised_purchase.store,
        config,
    )

    if result.changed:
        logger.info(
            "Retro-matching examined %s lines: %s filled, %s strengthened.",
            result.examined,
            result.filled,
            result.strengthened,
        )
    return result
