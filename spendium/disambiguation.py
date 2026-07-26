"""Tier 3: asking the player, sparingly.

The shopper held the product, so their answer is the best signal available
anywhere in the system. It is also the most expensive: attention does not scale,
and a player who is shown fifteen icons learns to ignore all of them — including
the one that mattered.

So prompts are budgeted per receipt and ordered by how much resolving each one
is worth to *everyone*, not by how uncertain this particular line looks. A
receipt string blocking five hundred pending line items across the player base
is worth roughly five hundred times a one-off, and the source plan's
"lowest confidence first" ordering would bury it under a private oddity nobody
else will ever buy.
"""

from dataclasses import dataclass
from typing import Any

from django.db.models import Count

from . import catalogue, matching
from .models import (
    MatchConfig,
    MatchTier,
    Product,
    ProductAlias,
    PurchaseLineItem,
    Store,
)


class WindowClosedError(RuntimeError):
    """The purchase has been anonymised; its line items are no longer the player's."""


@dataclass(frozen=True)
class Prompt:
    """A line item to ask about, with candidates recomputed at display time.

    Recomputing rather than reusing the scores from processing is deliberate:
    the catalogue may have grown since the receipt was read, and the better
    answer might now exist. It also costs nothing — the matching cascade is
    offline.
    """

    line: PurchaseLineItem
    candidates: list[matching.Candidate]
    blocked_elsewhere: int

    @property
    def has_match(self) -> bool:
        return self.line.product_id is not None


def _global_pending_counts(normalised: list[str]) -> dict[str, int]:
    """How many pending line items each raw string is blocking, everywhere.

    One aggregate for the whole receipt rather than a correlated subquery per
    line — this runs on every page view of a purchase.
    """
    if not normalised:
        return {}
    rows = (
        PurchaseLineItem.objects.filter(
            raw_text_normalised__in=normalised,
            disambiguation_state=PurchaseLineItem.STATE_PENDING,
        )
        .values("raw_text_normalised")
        .annotate(total=Count("pk"))
    )
    return {row["raw_text_normalised"]: row["total"] for row in rows}


def prompt_queue(purchase: Any) -> list[Prompt]:
    """The questions worth asking about this receipt, best first and budgeted.

    Ordering:
      1. How many pending line items share this string, across all players.
      2. Least confident first, so a coin-flip beats a near-certainty.
      3. Primary key, purely so the order is stable between page loads.
    """
    config = MatchConfig.get()
    if not config.prompt_budget:
        return []

    pending = list(
        purchase.line_items.filter(
            disambiguation_state=PurchaseLineItem.STATE_PENDING
        ).select_related("product")
    )
    if not pending:
        return []

    counts = _global_pending_counts([line.raw_text_normalised for line in pending])

    def rank(line: PurchaseLineItem) -> tuple:
        blocked = counts.get(line.raw_text_normalised, 1)
        confidence = line.match_confidence if line.match_confidence is not None else 0
        return (-blocked, confidence, line.pk)

    pending.sort(key=rank)

    prompts = []
    for line in pending[: config.prompt_budget]:
        result = matching.match_line_item(
            line.raw_text, line.interpreted_name, store=purchase.store, config=config
        )
        prompts.append(
            Prompt(
                line=line,
                # Already filtered to the noise floor by the cascade: showing
                # implausible options produces confusion, not signal.
                candidates=result.candidates,
                blocked_elsewhere=counts.get(line.raw_text_normalised, 1),
            )
        )
    return prompts


def _require_open_window(line: PurchaseLineItem) -> None:
    if not line.purchase.window_is_open:
        raise WindowClosedError(
            "This purchase has passed its window and can no longer be edited."
        )


def _apply_alias(
    line: PurchaseLineItem, product: Product, store: Store | None, player: object
) -> None:
    """Record what the player said about this string, at this retailer.

    Uniqueness is global per (store, string), so there is only ever one row to
    update — a player disagreeing cannot simply add a competing alias.

    Disagreement therefore nets against the existing evidence rather than
    overturning it outright. One dissenter does not undo two confirmations; the
    alias falls back to provisional and starts prompting again. Only once it is
    fully demoted does the string become free to mean something else, at which
    point the dissenting player's choice takes it over.

    Votes are per player, so someone who resolves the same string on two of
    their own receipts is counted once, not twice.
    """
    if not line.raw_text_normalised:
        return

    alias = ProductAlias.objects.filter(
        store=store, raw_text_normalised=line.raw_text_normalised
    ).first()

    if alias is None:
        alias = ProductAlias.objects.create(
            product=product,
            store=store,
            raw_text=line.raw_text,
            source=ProductAlias.SOURCE_PLAYER,
        )
        alias.confirm(player)
        return

    if alias.product_id == product.pk:
        alias.confirm(player)
        return

    alias.contradict(player)
    if alias.status == ProductAlias.STATUS_DEMOTED:
        # The evidence for the old meaning is exhausted, so the string is free
        # to be reassigned. Earlier votes are cleared with it — they were about
        # a different product and would otherwise carry over as support for one
        # nobody voted for.
        alias.votes.all().delete()
        alias.product = product
        alias.source = ProductAlias.SOURCE_PLAYER
        alias.save()
        alias.confirm(player)


def _resolve(
    line: PurchaseLineItem, product: Product, player: object
) -> PurchaseLineItem:
    line.product = product
    line.match_tier = MatchTier.PLAYER
    # The player is the authority here, not a scorer, so there is no
    # measurement to record. A synthetic 1.0 would be indistinguishable
    # downstream from a fuzzy score that happened to be perfect.
    line.match_confidence = None
    line.disambiguation_state = PurchaseLineItem.STATE_RESOLVED
    line.save(
        update_fields=[
            "product",
            "match_tier",
            "match_confidence",
            "disambiguation_state",
        ]
    )
    _apply_alias(line, product, line.purchase.store, player)
    return line


def confirm(line: PurchaseLineItem) -> PurchaseLineItem:
    """The player agrees with the match already on the line."""
    _require_open_window(line)
    if line.product is None:
        raise ValueError("Nothing to confirm — this line has no match.")
    return _resolve(line, line.product, line.purchase.player)


def choose(line: PurchaseLineItem, product: Product) -> PurchaseLineItem:
    """The player picks a different catalogue record."""
    _require_open_window(line)
    return _resolve(line, product.resolve_canonical(), line.purchase.player)


def submit_free_text(line: PurchaseLineItem, text: str) -> PurchaseLineItem:
    """The player describes the product themselves.

    Free text never writes to the catalogue directly. It goes through the same
    matching cascade as any other input, so a description that already exists
    resolves to the existing record instead of creating a near-duplicate — the
    fragmentation that would otherwise fall to the admin merge queue.

    Only when nothing matches does a new record appear, marked as
    player-supplied and unverified.
    """
    _require_open_window(line)
    text = (text or "").strip()
    if not text:
        raise ValueError("Description was empty.")

    config = MatchConfig.get()
    result = matching.match_line_item(
        line.raw_text, text, store=line.purchase.store, config=config
    )
    if result.product is not None and not result.needs_prompt:
        return _resolve(line, result.product, line.purchase.player)

    # Cluster rather than create outright: another player may already have
    # described this product in almost the same words, and two records would
    # split its ratings.
    product = catalogue.create_or_cluster(text)
    return _resolve(line, product, line.purchase.player)
