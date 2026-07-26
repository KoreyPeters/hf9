"""Alias integrity and catalogue hygiene.

The poisoning scenario is the important one here. Raw receipt strings are matched
deterministically at Tier 0, so a wrong alias is applied silently and
confidently to every future receipt carrying that string. It is the one failure
mode that gets *worse* as the system grows more confident, which is why the
guards are structural rather than advisory.
"""

import pytest

from spendium import catalogue
from spendium.models import MatchConfig, Product, ProductAlias, Store


@pytest.fixture
def store(db: None) -> Store:
    return Store.objects.create(name="Shoppers Drug Mart")


@pytest.fixture
def product(db: None) -> Product:
    return Product.objects.create(canonical_name="Colgate Toothpaste Bright Whitening")


@pytest.fixture
def alias(product: Product, store: Store) -> ProductAlias:
    return ProductAlias.objects.create(
        product=product, store=store, raw_text="TP-COLG-250"
    )


# ── Independence: the poisoning scenario ──────────────────────────────────────


@pytest.mark.django_db
def test_one_player_cannot_promote_an_alias_alone(alias: ProductAlias, voter) -> None:
    """The whole point of requiring two confirmations.

    Counting taps would let one mis-tapping player make a wrong alias
    authoritative, after which it would be applied silently to every future
    receipt carrying that string.
    """
    alias.confirm(voter)
    alias.confirm(voter)
    alias.confirm(voter)
    assert alias.confirmation_count == 1
    assert alias.status == ProductAlias.STATUS_PROVISIONAL


@pytest.mark.django_db
def test_two_different_players_do_promote_it(
    alias: ProductAlias, voter, other_voter
) -> None:
    alias.confirm(voter)
    alias.confirm(other_voter)
    assert alias.confirmation_count == 2
    assert alias.status == ProductAlias.STATUS_AUTHORITATIVE


@pytest.mark.django_db
def test_one_vote_is_recorded_per_player(alias: ProductAlias, voter) -> None:
    alias.confirm(voter)
    alias.contradict(voter)
    alias.confirm(voter)
    assert alias.votes.count() == 1


@pytest.mark.django_db
def test_counts_are_derived_not_incremented(
    alias: ProductAlias, voter, other_voter
) -> None:
    """A stale running total would survive a vote being withdrawn."""
    alias.confirm(voter)
    alias.confirm(other_voter)
    alias.votes.filter(player=other_voter).delete()
    alias._recount()
    assert alias.confirmation_count == 1
    assert alias.status == ProductAlias.STATUS_PROVISIONAL


# ── The review queue ──────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_repeated_contradiction_flags_for_review(
    alias: ProductAlias, voter, other_voter
) -> None:
    """Two people disagreeing means disputed, not mis-tapped."""
    alias.contradict(voter)
    assert alias.needs_review is False
    alias.contradict(other_voter)
    assert alias.needs_review is True
    assert catalogue.review_queue_size() == 1


@pytest.mark.django_db
def test_a_single_objection_does_not_reach_the_queue(
    alias: ProductAlias, voter
) -> None:
    """The queue is meant to stay small enough for a person to work through."""
    alias.contradict(voter)
    assert catalogue.review_queue_size() == 0


@pytest.mark.django_db
def test_agreement_clears_the_review_flag(
    alias: ProductAlias, voter, other_voter
) -> None:
    alias.contradict(voter)
    alias.contradict(other_voter)
    assert alias.needs_review is True
    alias.confirm(voter)
    alias.confirm(other_voter)
    assert alias.needs_review is False


# ── Auto-clustering unverified duplicates ─────────────────────────────────────


@pytest.mark.django_db
def test_a_near_identical_unverified_record_is_reused() -> None:
    """Two records for one product would split its ratings."""
    existing = Product.objects.create(
        canonical_name="Bulk Red Lentils", status=Product.STATUS_UNVERIFIED
    )
    result = catalogue.create_or_cluster("bulk red lentils")
    assert result == existing
    assert Product.objects.count() == 1


@pytest.mark.django_db
def test_a_genuinely_different_name_creates_a_record() -> None:
    Product.objects.create(
        canonical_name="Bulk Red Lentils", status=Product.STATUS_UNVERIFIED
    )
    result = catalogue.create_or_cluster("Heinz Tomato Ketchup")
    assert result.canonical_name == "Heinz Tomato Ketchup"
    assert Product.objects.count() == 2


@pytest.mark.django_db
def test_verified_records_are_never_auto_absorbed() -> None:
    """An unreviewed player description must not silently take over a curated one.

    A wrong auto-merge into a verified record is far worse than leaving two rows
    for a human to look at.
    """
    Product.objects.create(
        canonical_name="Bulk Red Lentils", status=Product.STATUS_VERIFIED
    )
    result = catalogue.create_or_cluster("bulk red lentils")
    assert result.status == Product.STATUS_UNVERIFIED
    assert Product.objects.count() == 2


@pytest.mark.django_db
def test_clustering_respects_the_threshold() -> None:
    Product.objects.create(
        canonical_name="Bulk Red Lentils", status=Product.STATUS_UNVERIFIED
    )
    config = MatchConfig.get()
    config.auto_merge_score = 100
    config.save()
    catalogue.create_or_cluster("Bulk Red Lentil")
    assert Product.objects.count() == 2


@pytest.mark.django_db
def test_new_records_are_marked_player_supplied() -> None:
    product = catalogue.create_or_cluster("Something Novel")
    assert product.status == Product.STATUS_UNVERIFIED
    assert product.confidence_source == Product.SOURCE_PLAYER


# ── Merging ───────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_merging_moves_aliases_and_retires_the_loser(store: Store) -> None:
    loser = Product.objects.create(canonical_name="Loser")
    winner = Product.objects.create(canonical_name="Winner")
    ProductAlias.objects.create(product=loser, store=store, raw_text="X-1")

    assert catalogue.merge_products(loser, winner) is True
    loser.refresh_from_db()
    assert loser.status == Product.STATUS_RETIRED
    assert loser.merged_into == winner
    assert winner.aliases.count() == 1


@pytest.mark.django_db
def test_merging_a_product_into_itself_is_refused() -> None:
    product = Product.objects.create(canonical_name="Only")
    assert catalogue.merge_products(product, product) is False


@pytest.mark.django_db
def test_a_cycle_is_refused() -> None:
    """resolve_canonical() would loop forever on one."""
    a = Product.objects.create(canonical_name="A")
    b = Product.objects.create(canonical_name="B")
    catalogue.merge_products(a, b)
    assert catalogue.merge_products(b, a) is False


# ── Ratings follow merges ─────────────────────────────────────────────────────


@pytest.mark.django_db
def test_merge_group_covers_both_records() -> None:
    """Ratings given before a merge must still count afterwards."""
    loser = Product.objects.create(canonical_name="Loser")
    winner = Product.objects.create(canonical_name="Winner")
    catalogue.merge_products(loser, winner)
    assert catalogue.merge_group_ids(winner) == sorted([loser.pk, winner.pk])


@pytest.mark.django_db
def test_merge_group_is_transitive() -> None:
    """A merges into B, B later merges into C — all three must roll up to C."""
    a = Product.objects.create(canonical_name="A")
    b = Product.objects.create(canonical_name="B")
    c = Product.objects.create(canonical_name="C")
    catalogue.merge_products(a, b)
    catalogue.merge_products(b, c)
    assert catalogue.merge_group_ids(c) == sorted([a.pk, b.pk, c.pk])


@pytest.mark.django_db
def test_merge_group_is_the_same_from_any_member() -> None:
    """Asking from a retired record must give the same answer as from the survivor."""
    a = Product.objects.create(canonical_name="A")
    b = Product.objects.create(canonical_name="B")
    catalogue.merge_products(a, b)
    assert catalogue.merge_group_ids(a) == catalogue.merge_group_ids(b)


@pytest.mark.django_db
def test_an_unmerged_product_is_its_own_group() -> None:
    product = Product.objects.create(canonical_name="Alone")
    assert catalogue.merge_group_ids(product) == [product.pk]
