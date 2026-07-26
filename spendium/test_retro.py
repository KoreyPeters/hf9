"""Retro-matching: the catalogue improving for receipts already read.

The point of these tests is the compounding claim. A catalogue that grows should
resolve historical backlog on its own — and must do so without ever overturning
a player, or touching points that were settled long ago.
"""

from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import Player
from points.models import PointTransaction
from spendium import retro
from spendium.models import (
    AnonymisedLineItem,
    AnonymisedPurchase,
    MatchTier,
    Product,
    ProductAlias,
    Purchase,
    PurchaseLineItem,
    Store,
)


@pytest.fixture
def shopper(db: None) -> Player:
    return Player.objects.create_user(username="shopper", email="shopper@example.com")


@pytest.fixture
def store(db: None) -> Store:
    return Store.objects.create(name="Shoppers Drug Mart")


@pytest.fixture
def purchase(shopper: Player, store: Store) -> Purchase:
    return Purchase.objects.create(
        player=shopper, store=store, purchased_at=timezone.now(), total=Decimal("5.00")
    )


def make_line(purchase: Purchase, raw_text: str, **kwargs) -> PurchaseLineItem:
    defaults = {
        "interpreted_name": raw_text.title(),
        "line_total": Decimal("1.00"),
        "match_tier": MatchTier.UNMATCHED,
        "disambiguation_state": PurchaseLineItem.STATE_PENDING,
    }
    defaults.update(kwargs)
    return PurchaseLineItem.objects.create(
        purchase=purchase, raw_text=raw_text, **defaults
    )


def make_anonymised_line(store: Store, raw_text: str, **kwargs) -> AnonymisedLineItem:
    anon = AnonymisedPurchase.objects.create(
        store=store, purchased_at=timezone.now(), total=Decimal("5.00")
    )
    defaults = {
        "interpreted_name": raw_text.title(),
        "line_total": Decimal("1.00"),
        "match_tier": MatchTier.UNMATCHED,
    }
    defaults.update(kwargs)
    return AnonymisedLineItem.objects.create(
        anonymised_purchase=anon, raw_text=raw_text, **defaults
    )


# ── Filling what could not be matched before ──────────────────────────────────


@pytest.mark.django_db
def test_a_product_added_later_resolves_old_backlog(purchase: Purchase) -> None:
    """The compounding claim, in its simplest form."""
    line = make_line(purchase, "HEINZ KETCHUP 750ML")
    assert retro.run().filled == 0

    product = Product.objects.create(canonical_name="Heinz Ketchup")
    result = retro.run()

    assert result.filled == 1
    line.refresh_from_db()
    assert line.product == product


@pytest.mark.django_db
def test_an_alias_confirmed_by_one_player_helps_everyone(
    purchase: Purchase, store: Store, voter
) -> None:
    """One player's confirmation clears the same string from other receipts."""
    line = make_line(purchase, "TP-COLG-250")
    product = Product.objects.create(canonical_name="Colgate Toothpaste Whitening")
    ProductAlias.objects.create(product=product, store=store, raw_text="TP-COLG-250")

    assert retro.run().filled == 1
    line.refresh_from_db()
    assert line.product == product
    assert line.match_tier == MatchTier.ALIAS


@pytest.mark.django_db
def test_a_confident_fill_clears_the_prompt(purchase: Purchase, store: Store) -> None:
    """Backlog should shrink without anybody being asked anything."""
    line = make_line(purchase, "TP-COLG-250")
    product = Product.objects.create(canonical_name="Colgate Toothpaste Whitening")
    alias = ProductAlias.objects.create(
        product=product, store=store, raw_text="TP-COLG-250"
    )
    alias.confirmation_count = 2
    alias._recompute_status()
    alias.save()

    retro.run()
    line.refresh_from_db()
    assert line.disambiguation_state == PurchaseLineItem.STATE_NOT_NEEDED


@pytest.mark.django_db
def test_nothing_changes_when_the_catalogue_has_not_moved(
    purchase: Purchase,
) -> None:
    make_line(purchase, "SOMETHING UNKNOWN")
    assert retro.run().changed == 0


# ── Strengthening an existing match ───────────────────────────────────────────


@pytest.mark.django_db
def test_a_new_alias_upgrades_a_fuzzy_match(purchase: Purchase, store: Store) -> None:
    """An exact alias is backed by people; a fuzzy score is not."""
    product = Product.objects.create(canonical_name="Colgate Toothpaste Whitening")
    line = make_line(
        purchase,
        "TP-COLG-250",
        product=product,
        match_tier=MatchTier.FUZZY,
        match_confidence=Decimal("0.740"),
    )
    ProductAlias.objects.create(product=product, store=store, raw_text="TP-COLG-250")

    assert retro.run().strengthened == 1
    line.refresh_from_db()
    assert line.match_tier == MatchTier.ALIAS
    assert line.match_confidence == Decimal("1.000")


@pytest.mark.django_db
def test_rescoring_alone_never_changes_an_existing_match(
    purchase: Purchase,
) -> None:
    """Only a Tier 0 hit displaces an answer already recorded.

    Otherwise a catalogue addition could silently reassign lines by string
    similarity, which is exactly the kind of quiet change nobody would notice.
    """
    original = Product.objects.create(canonical_name="Heinz Ketchup")
    line = make_line(
        purchase,
        "HEINZ KETCHUP 750ML",
        product=original,
        match_tier=MatchTier.FUZZY,
        match_confidence=Decimal("0.800"),
    )
    Product.objects.create(canonical_name="Heinz Ketchup 750ml Squeeze")

    retro.run()
    line.refresh_from_db()
    assert line.product == original


@pytest.mark.django_db
def test_an_alias_hit_is_not_rewritten_every_run(
    purchase: Purchase, store: Store
) -> None:
    product = Product.objects.create(canonical_name="Colgate Toothpaste Whitening")
    ProductAlias.objects.create(product=product, store=store, raw_text="TP-COLG-250")
    make_line(
        purchase,
        "TP-COLG-250",
        product=product,
        match_tier=MatchTier.ALIAS,
        match_confidence=Decimal("1.000"),
    )
    assert retro.run().changed == 0


# ── What it must never touch ──────────────────────────────────────────────────


@pytest.mark.django_db
def test_a_player_decision_is_never_overturned(
    purchase: Purchase, store: Store
) -> None:
    """They held the product. No amount of string similarity outranks that."""
    chosen = Product.objects.create(canonical_name="What The Player Said")
    other = Product.objects.create(canonical_name="Colgate Toothpaste Whitening")
    ProductAlias.objects.create(product=other, store=store, raw_text="TP-COLG-250")

    line = make_line(
        purchase,
        "TP-COLG-250",
        product=chosen,
        match_tier=MatchTier.PLAYER,
        disambiguation_state=PurchaseLineItem.STATE_RESOLVED,
    )
    retro.run()
    line.refresh_from_db()
    assert line.product == chosen
    assert line.match_tier == MatchTier.PLAYER


@pytest.mark.django_db
def test_a_resolved_line_is_left_alone(purchase: Purchase, store: Store) -> None:
    chosen = Product.objects.create(canonical_name="Player's Answer")
    other = Product.objects.create(canonical_name="Colgate Toothpaste Whitening")
    ProductAlias.objects.create(product=other, store=store, raw_text="TP-COLG-250")

    line = make_line(
        purchase,
        "TP-COLG-250",
        product=chosen,
        match_tier=MatchTier.FUZZY,
        disambiguation_state=PurchaseLineItem.STATE_RESOLVED,
    )
    retro.run()
    line.refresh_from_db()
    assert line.product == chosen


@pytest.mark.django_db
def test_settled_points_are_never_altered(purchase: Purchase, shopper: Player) -> None:
    """Points are awarded once and never retrospectively adjusted."""
    make_line(purchase, "HEINZ KETCHUP 750ML")
    txn = PointTransaction.objects.create(
        player=shopper, amount=Decimal("25.00"), reason="purchase"
    )
    Product.objects.create(canonical_name="Heinz Ketchup")

    retro.run()
    txn.refresh_from_db()
    assert txn.amount == Decimal("25.00")
    assert PointTransaction.objects.count() == 1


# ── Past the window ───────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_anonymous_line_items_are_matched_too(store: Store) -> None:
    """A purchase past its window still feeds a permanent rating."""
    line = make_anonymised_line(store, "HEINZ KETCHUP 750ML")
    product = Product.objects.create(canonical_name="Heinz Ketchup")

    assert retro.run().filled == 1
    line.refresh_from_db()
    assert line.product == product


@pytest.mark.django_db
def test_a_player_decision_survives_anonymisation(store: Store) -> None:
    """The verdict outlives the link to the person who gave it."""
    chosen = Product.objects.create(canonical_name="What The Player Said")
    other = Product.objects.create(canonical_name="Colgate Toothpaste Whitening")
    ProductAlias.objects.create(product=other, store=store, raw_text="TP-COLG-250")

    line = make_anonymised_line(
        store, "TP-COLG-250", product=chosen, match_tier=MatchTier.PLAYER
    )
    retro.run()
    line.refresh_from_db()
    assert line.product == chosen


# ── Batching ──────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_the_batch_limit_is_respected(purchase: Purchase) -> None:
    """A modest batch that finishes beats a large one that times out."""
    Product.objects.create(canonical_name="Heinz Ketchup")
    for _ in range(6):
        make_line(purchase, "HEINZ KETCHUP 750ML")
    assert retro.run(limit=2).examined == 2


@pytest.mark.django_db
def test_repeated_runs_work_through_the_backlog(purchase: Purchase) -> None:
    Product.objects.create(canonical_name="Heinz Ketchup")
    for _ in range(4):
        make_line(purchase, "HEINZ KETCHUP 750ML")

    retro.run(limit=2)
    retro.run(limit=2)
    assert PurchaseLineItem.objects.filter(product__isnull=True).count() == 0


@pytest.mark.django_db
def test_each_run_moves_on_to_unseen_lines(purchase: Purchase) -> None:
    """Batches rotate rather than re-examining the same head of the queue.

    Ordering only by primary key would re-fetch the earliest rows every run, so
    a backlog larger than one batch would never have its tail reached.
    """
    for _ in range(4):
        make_line(purchase, "SOMETHING UNMATCHABLE")

    retro.run(limit=2)
    first_pass = set(
        PurchaseLineItem.objects.filter(retro_checked_at__isnull=False).values_list(
            "pk", flat=True
        )
    )
    assert len(first_pass) == 2

    retro.run(limit=2)
    second_pass = set(
        PurchaseLineItem.objects.filter(retro_checked_at__isnull=False).values_list(
            "pk", flat=True
        )
    )
    assert len(second_pass) == 4


@pytest.mark.django_db
def test_lines_are_stamped_even_when_nothing_matches(purchase: Purchase) -> None:
    line = make_line(purchase, "SOMETHING UNMATCHABLE")
    retro.run()
    line.refresh_from_db()
    assert line.retro_checked_at is not None
