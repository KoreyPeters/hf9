"""The matching cascade: receipt string to canonical product.

Four tiers, each handling only what the tier above could not:

    Tier 0  exact alias lookup       deterministic, O(1)
    Tier 1  FTS5 narrowing + scoring  bounded by shortlist size
    Tier 2  targeted model adjudication  (Phase 5)
    Tier 3  player prompt             budgeted and prioritised  (Phase 6)

Only Tiers 0 and 1 live here. Both run entirely offline — no API calls — which
is the point of matching being a separate stage rather than something the
extraction model does inline. It means thresholds can be tuned against a fixed
fixture set, and it means matching can be *re-run* later: once the receipt image
is deleted at 24 hours, the stored raw text is the only durable record of the
purchase, so replaying it as the catalogue grows is what lets the system
resolve old backlog without ever seeing the image again.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from rapidfuzz import fuzz, process

from . import search
from .models import (
    MatchConfig,
    MatchTier,
    Product,
    ProductAlias,
)
from .normalisation import normalise_raw_text

# Chosen over token_set_ratio, which scores a query as a perfect match against
# any candidate containing it as a subset — "Colgate" would score 100 against
# "Colgate Toothpaste Bright Whitening". Sorting tokens tolerates word-order
# differences while still penalising a length mismatch.
SCORER = fuzz.token_sort_ratio


@dataclass(frozen=True)
class Candidate:
    product: Product
    score: float


@dataclass(frozen=True)
class MatchResult:
    product: Product | None
    tier: str
    confidence: Decimal | None
    candidates: list[Candidate] = field(default_factory=list)
    needs_prompt: bool = False

    @property
    def matched(self) -> bool:
        return self.product is not None


def _alias_lookup(normalised: str, store: object | None) -> ProductAlias | None:
    """Tier 0. Retailer-scoped first, then global.

    Retailer scope wins because a receipt string means whatever the chain that
    printed it means by it; a global alias is a weaker generalisation and only
    applies when the retailer has not spoken.

    Demoted aliases are excluded — a demoted alias is one the players have
    contradicted, and continuing to match on it would re-apply a known error
    with full confidence.
    """
    base = ProductAlias.objects.exclude(
        status=ProductAlias.STATUS_DEMOTED
    ).select_related("product")
    if store is not None:
        scoped = base.filter(store=store, raw_text_normalised=normalised).first()
        if scoped is not None:
            return scoped
    return base.filter(store__isnull=True, raw_text_normalised=normalised).first()


def _score_candidates(normalised_query: str, product_ids: list[int]) -> list[Candidate]:
    """Tier 1 Stage B. Score the shortlist and order it."""
    if not product_ids:
        return []

    products = list(
        Product.objects.filter(pk__in=product_ids).exclude(
            status=Product.STATUS_RETIRED
        )
    )
    if not products:
        return []

    choices = [normalise_raw_text(p.canonical_name) for p in products]
    # `extract` scores the whole shortlist in C and returns (choice, score,
    # index), already ordered best first. `cdist` would vectorise this but
    # drags in numpy, and there is nothing to gain: the shortlist is capped at
    # candidate_limit, so a full receipt is ~6k comparisons — about 12ms.
    scored = process.extract(normalised_query, choices, scorer=SCORER, limit=None)

    return [
        Candidate(product=products[index], score=float(raw_score))
        for _choice, raw_score, index in scored
    ]


def match_line_item(
    raw_text: str,
    interpreted_name: str = "",
    store: object | None = None,
    config: MatchConfig | None = None,
) -> MatchResult:
    """Resolve one receipt line to a product.

    `raw_text` drives Tier 0 and `interpreted_name` drives Tier 1. They are
    different keys doing different jobs: the raw string is stable and exact,
    the interpretation is fuzzy and human-readable. Falling back to the raw
    string as the Tier 1 query keeps the function useful before extraction
    exists.
    """
    config = config or MatchConfig.get()
    normalised_raw = normalise_raw_text(raw_text)

    alias = _alias_lookup(normalised_raw, store)
    if alias is not None:
        return MatchResult(
            product=alias.product.resolve_canonical(),
            tier=MatchTier.ALIAS,
            confidence=Decimal("1.000"),
            # A provisional alias has been confirmed once. It is trusted enough
            # to match on, but not enough to stop asking about.
            needs_prompt=alias.status == ProductAlias.STATUS_PROVISIONAL,
        )

    query = normalise_raw_text(interpreted_name) or normalised_raw
    shortlist = search.narrow(query, config.candidate_limit)
    candidates = _score_candidates(query, shortlist)

    visible = [c for c in candidates if c.score >= config.noise_floor_score]
    best = candidates[0] if candidates else None

    if best is None or best.score < config.weak_match_score:
        return MatchResult(
            product=None,
            tier=MatchTier.UNMATCHED,
            confidence=None,
            candidates=visible[:3],
            needs_prompt=True,
        )

    return MatchResult(
        product=best.product.resolve_canonical(),
        tier=MatchTier.FUZZY,
        confidence=Decimal(str(round(best.score / 100, 3))),
        candidates=visible[:3],
        needs_prompt=best.score < config.strong_match_score,
    )


def match_line_items(
    items: list[tuple[str, str]],
    store: object | None = None,
) -> list[MatchResult]:
    """Match a whole receipt. `items` is a list of (raw_text, interpreted_name).

    Loads the config once for the batch rather than per line.
    """
    config = MatchConfig.get()
    return [
        match_line_item(raw, interpreted, store=store, config=config)
        for raw, interpreted in items
    ]
