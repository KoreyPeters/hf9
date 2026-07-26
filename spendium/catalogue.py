"""Catalogue hygiene: merging records and keeping duplicates from accumulating.

Two players describing the same product slightly differently would otherwise
create two records, splitting its ratings and doubling how long each takes to
clear the display threshold. The source plan routed every merge to an admin —
the one queue it promised would stay small — so this closes the common case
automatically and leaves humans only the genuinely contested ones.
"""

from django.db.models import QuerySet
from rapidfuzz import fuzz

from . import search
from .models import MatchConfig, Product, ProductAlias
from .normalisation import normalise_raw_text

SCORER = fuzz.token_sort_ratio


def merge_group_ids(product: Product) -> list[int]:
    """Every product id whose ratings belong to this record.

    Ratings attach to whichever record existed when they were given, so a merge
    would silently orphan them unless aggregation looks across the whole chain.
    `merged_into` is followed in both directions: down to the survivor, then out
    across everything that has ever merged into it.
    """
    canonical = product.resolve_canonical()
    ids = {canonical.pk}
    frontier = [canonical.pk]
    while frontier:
        merged = Product.objects.filter(merged_into_id__in=frontier).values_list(
            "pk", flat=True
        )
        new = [pk for pk in merged if pk not in ids]
        ids.update(new)
        frontier = new
    return sorted(ids)


def merge_products(loser: Product, winner: Product) -> bool:
    """Retire `loser` into `winner`, moving its aliases and UPCs across.

    Ratings are not rewritten. They resolve through `merged_into` at read time
    via `merge_group_ids`, which keeps the merge reversible — rewriting rating
    rows would destroy the evidence of what was originally recorded.
    """
    winner = winner.resolve_canonical()
    if loser.pk == winner.pk:
        return False
    # Merging one of the winner's own ancestors into it would make
    # resolve_canonical() loop.
    if winner.resolve_canonical().pk == loser.pk:
        return False

    # No collision check is needed on the aliases. (store, raw_text_normalised)
    # is unique across the whole table rather than per product, so a string
    # already resolves to exactly one product per retailer.
    loser.aliases.update(product=winner)
    loser.upcs.update(product=winner)
    loser.merged_into = winner
    loser.status = Product.STATUS_RETIRED
    loser.save()
    return True


def _unverified_candidates(name: str, exclude_pk: int | None) -> QuerySet[Product]:
    config = MatchConfig.get()
    shortlist = search.narrow(normalise_raw_text(name), config.candidate_limit)
    products = Product.objects.filter(
        pk__in=shortlist, status=Product.STATUS_UNVERIFIED
    )
    if exclude_pk is not None:
        products = products.exclude(pk=exclude_pk)
    return products


def find_duplicate_unverified(
    name: str, exclude_pk: int | None = None
) -> Product | None:
    """An existing unverified record near-identical to this name.

    Restricted to unverified records on purpose. Auto-merging into a *verified*
    record would let an unreviewed player description silently absorb a curated
    one, which is a much worse error than leaving two rows for a human to look
    at.
    """
    config = MatchConfig.get()
    normalised = normalise_raw_text(name)
    if not normalised:
        return None

    best: Product | None = None
    best_score = 0.0
    for candidate in _unverified_candidates(name, exclude_pk):
        score = SCORER(normalised, normalise_raw_text(candidate.canonical_name))
        if score > best_score:
            best, best_score = candidate, score

    if best is not None and best_score >= config.auto_merge_score:
        return best
    return None


def create_or_cluster(
    name: str,
    confidence_source: str = Product.SOURCE_PLAYER,
) -> Product:
    """Return an existing near-identical unverified record, or make a new one.

    This is the cheap half of catalogue hygiene: stopping duplicates being
    created at all is far easier than merging them afterwards, and it is what
    keeps the admin queue to genuinely contested cases.
    """
    existing = find_duplicate_unverified(name)
    if existing is not None:
        return existing
    return Product.objects.create(
        canonical_name=name,
        status=Product.STATUS_UNVERIFIED,
        confidence_source=confidence_source,
    )


def aliases_needing_review() -> QuerySet[ProductAlias]:
    """The admin queue. Small by design — see the module docstring."""
    return ProductAlias.objects.filter(needs_review=True).select_related(
        "product", "store"
    )


def review_queue_size() -> int:
    return aliases_needing_review().count()
