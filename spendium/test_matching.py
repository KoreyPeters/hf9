"""Tier 0 and Tier 1 of the matching cascade.

Every test here runs offline. That is the practical payoff of matching being a
separate stage rather than something the extraction model does inline: the
thresholds can be exercised against a fixed input set with no API calls, so
tuning them is cheap and repeatable.
"""

from decimal import Decimal

import pytest

from spendium import fixtures_receipts, matching, search
from spendium.models import (
    MatchConfig,
    MatchTier,
    Product,
    ProductAlias,
    Store,
)


@pytest.fixture
def store(db: None) -> Store:
    return Store.objects.create(name="Shoppers Drug Mart")


@pytest.fixture
def product(db: None) -> Product:
    return Product.objects.create(canonical_name="Colgate Toothpaste Bright Whitening")


@pytest.fixture
def catalogue(db: None) -> dict[str, Product]:
    return {
        name: Product.objects.create(canonical_name=name)
        for name in fixtures_receipts.CATALOGUE
    }


# ── FTS5 narrowing index ──────────────────────────────────────────────────────


@pytest.mark.django_db
def test_index_populated_by_signal_on_product_create() -> None:
    p = Product.objects.create(canonical_name="Heinz Tomato Ketchup")
    assert search.narrow("heinz ketchup", 10) == [p.pk]


@pytest.mark.django_db
def test_index_updated_when_canonical_name_changes() -> None:
    p = Product.objects.create(canonical_name="Heinz Tomato Ketchup")
    p.canonical_name = "Heinz Yellow Mustard"
    p.save()
    assert search.narrow("mustard", 10) == [p.pk]
    assert search.narrow("ketchup", 10) == []


@pytest.mark.django_db
def test_index_removed_when_product_deleted() -> None:
    p = Product.objects.create(canonical_name="Heinz Tomato Ketchup")
    p.delete()
    assert search.narrow("ketchup", 10) == []


@pytest.mark.django_db
def test_aliases_become_searchable(product: Product, store: Store) -> None:
    """A learned receipt string should help future fuzzy matches, not just exact ones."""
    assert search.narrow("tp colg 250", 10) == []
    ProductAlias.objects.create(product=product, store=store, raw_text="TP-COLG-250")
    assert search.narrow("tp colg 250", 10) == [product.pk]


@pytest.mark.django_db
def test_demoted_aliases_leave_the_index(product: Product, store: Store) -> None:
    alias = ProductAlias.objects.create(
        product=product, store=store, raw_text="TP-COLG-250"
    )
    alias.contradict()
    assert search.narrow("tp colg 250", 10) == []


@pytest.mark.django_db
def test_narrowing_ranks_by_relevance() -> None:
    """BM25 must put the discriminative token ahead of the common one."""
    colgate = Product.objects.create(canonical_name="Colgate Toothpaste Whitening")
    Product.objects.create(canonical_name="Crest Toothpaste Whitening")
    Product.objects.create(canonical_name="Aim Toothpaste Whitening")
    assert search.narrow("colgate toothpaste whitening", 10)[0] == colgate.pk


@pytest.mark.django_db
def test_narrowing_respects_the_limit() -> None:
    for i in range(10):
        Product.objects.create(canonical_name=f"Toothpaste Brand {i}")
    assert len(search.narrow("toothpaste", 3)) == 3


@pytest.mark.django_db
def test_empty_query_returns_nothing() -> None:
    Product.objects.create(canonical_name="Heinz Tomato Ketchup")
    assert search.narrow("", 10) == []
    assert search.narrow("   ", 10) == []


@pytest.mark.django_db
def test_rebuild_restores_the_index_after_bulk_import() -> None:
    """bulk_create bypasses signals, which is exactly how seeding will work."""
    Product.objects.bulk_create(
        [Product(canonical_name=f"Bulk Product {i}") for i in range(5)]
    )
    assert search.narrow("bulk", 10) == []
    assert search.rebuild() == 5
    assert len(search.narrow("bulk", 10)) == 5


@pytest.mark.django_db
def test_index_lives_in_the_database_file() -> None:
    """Litestream replicates the main database and its WAL, nothing else.

    FTS5 keeps its data in ordinary shadow tables, so an index that shows up in
    sqlite_master is carried by replication and survives a restore. An in-memory
    or connection-local index would not, and the failure would only appear after
    a restore — when the catalogue silently stopped matching.
    """
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name LIKE %s ORDER BY name",
            [f"{search.TABLE}%"],
        )
        tables = [row[0] for row in cursor.fetchall()]

    assert search.TABLE in tables
    for suffix in ("_content", "_data", "_idx", "_config"):
        assert f"{search.TABLE}{suffix}" in tables


# ── Tier 0: exact alias lookup ────────────────────────────────────────────────


@pytest.mark.django_db
def test_tier0_exact_alias_hit(product: Product, store: Store) -> None:
    ProductAlias.objects.create(product=product, store=store, raw_text="TP-COLG-250")
    result = matching.match_line_item("TP-COLG-250", store=store)
    assert result.product == product
    assert result.tier == MatchTier.ALIAS
    assert result.confidence == Decimal("1.000")


@pytest.mark.django_db
def test_tier0_tolerates_punctuation_differences(
    product: Product, store: Store
) -> None:
    ProductAlias.objects.create(product=product, store=store, raw_text="TP-COLG-250")
    assert matching.match_line_item("TP COLG 250", store=store).product == product


@pytest.mark.django_db
def test_tier0_prefers_retailer_scoped_over_global(
    product: Product, store: Store
) -> None:
    """A string means whatever the chain that printed it means by it."""
    other = Product.objects.create(canonical_name="Crest 3D White Toothpaste")
    ProductAlias.objects.create(product=other, store=None, raw_text="TP-COLG-250")
    ProductAlias.objects.create(product=product, store=store, raw_text="TP-COLG-250")
    assert matching.match_line_item("TP-COLG-250", store=store).product == product


@pytest.mark.django_db
def test_tier0_falls_back_to_global(product: Product, store: Store) -> None:
    ProductAlias.objects.create(product=product, store=None, raw_text="TP-COLG-250")
    assert matching.match_line_item("TP-COLG-250", store=store).product == product


@pytest.mark.django_db
def test_tier0_ignores_demoted_aliases(product: Product, store: Store) -> None:
    """Matching on a contradicted alias would re-apply a known error confidently."""
    alias = ProductAlias.objects.create(
        product=product, store=store, raw_text="TP-COLG-250"
    )
    alias.contradict()
    assert matching.match_line_item("TP-COLG-250", store=store).tier != MatchTier.ALIAS


@pytest.mark.django_db
def test_tier0_provisional_alias_still_prompts(product: Product, store: Store) -> None:
    """Confirmed once is enough to match on, not enough to stop asking."""
    alias = ProductAlias.objects.create(
        product=product, store=store, raw_text="TP-COLG-250"
    )
    alias.confirm()
    assert matching.match_line_item("TP-COLG-250", store=store).needs_prompt is True


@pytest.mark.django_db
def test_tier0_authoritative_alias_does_not_prompt(
    product: Product, store: Store
) -> None:
    alias = ProductAlias.objects.create(
        product=product, store=store, raw_text="TP-COLG-250"
    )
    alias.confirm()
    alias.confirm()
    assert matching.match_line_item("TP-COLG-250", store=store).needs_prompt is False


@pytest.mark.django_db
def test_tier0_follows_a_merge(product: Product, store: Store) -> None:
    """An alias on a retired record must resolve to the surviving one."""
    survivor = Product.objects.create(canonical_name="Colgate Toothpaste Whitening")
    ProductAlias.objects.create(product=product, store=store, raw_text="TP-COLG-250")
    product.merged_into = survivor
    product.status = Product.STATUS_RETIRED
    product.save()
    assert matching.match_line_item("TP-COLG-250", store=store).product == survivor


# ── Tier 1: narrowing and scoring ─────────────────────────────────────────────


@pytest.mark.django_db
def test_tier1_matches_interpreted_name(catalogue: dict[str, Product]) -> None:
    result = matching.match_line_item(
        "TP-COLG-250", "Colgate Toothpaste Bright Whitening 250ml"
    )
    assert result.tier == MatchTier.FUZZY
    assert result.product == catalogue["Colgate Toothpaste Bright Whitening"]


@pytest.mark.django_db
def test_tier1_distinguishes_variants_of_one_brand(
    catalogue: dict[str, Product],
) -> None:
    """Variant is part of identity — Bright Whitening is not Cavity Protection."""
    result = matching.match_line_item(
        "COLG TP CAV 100", "Colgate Toothpaste Cavity Protection 100ml"
    )
    assert result.product == catalogue["Colgate Toothpaste Cavity Protection"]


@pytest.mark.django_db
def test_tier1_does_not_cross_match_competing_brands(
    catalogue: dict[str, Product],
) -> None:
    result = matching.match_line_item(
        "CREST 3DW TP 75ML", "Crest 3D White Toothpaste 75ml"
    )
    assert result.product == catalogue["Crest 3D White Toothpaste"]


@pytest.mark.django_db
def test_tier1_returns_unmatched_when_nothing_is_close(
    catalogue: dict[str, Product],
) -> None:
    """A weak best candidate must not be forced into a match."""
    result = matching.match_line_item(
        "BNLS CHKN BRST KG", "Chicken Breast Boneless per kg"
    )
    assert result.product is None
    assert result.tier == MatchTier.UNMATCHED
    assert result.needs_prompt is True


@pytest.mark.django_db
def test_tier1_suppresses_candidates_below_the_noise_floor(
    catalogue: dict[str, Product],
) -> None:
    """Showing implausible options produces confusion, not signal."""
    config = MatchConfig.get()
    result = matching.match_line_item("BANANAS 4011", "Bananas")
    assert all(c.score >= config.noise_floor_score for c in result.candidates)


@pytest.mark.django_db
def test_tier1_offers_at_most_three_candidates(catalogue: dict[str, Product]) -> None:
    result = matching.match_line_item("COLG TP", "Colgate Toothpaste")
    assert len(result.candidates) <= 3


@pytest.mark.django_db
def test_strong_match_does_not_prompt(catalogue: dict[str, Product]) -> None:
    result = matching.match_line_item("", "Heinz Tomato Ketchup")
    assert result.product == catalogue["Heinz Tomato Ketchup"]
    assert result.needs_prompt is False


@pytest.mark.django_db
def test_weak_match_prompts(catalogue: dict[str, Product]) -> None:
    """Above the weak bar the best candidate is used, but the player may correct it."""
    config = MatchConfig.get()
    config.strong_match_score = 99
    config.weak_match_score = 40
    config.save()
    result = matching.match_line_item("", "Heinz Ketchup Tomato Squeeze")
    assert result.matched
    assert result.needs_prompt is True


@pytest.mark.django_db
def test_retired_products_are_never_matched(catalogue: dict[str, Product]) -> None:
    for product in catalogue.values():
        product.status = Product.STATUS_RETIRED
        product.save()
    assert matching.match_line_item("", "Heinz Tomato Ketchup").product is None


@pytest.mark.django_db
def test_empty_catalogue_matches_nothing() -> None:
    result = matching.match_line_item(
        "TP-COLG-250", "Colgate Toothpaste Bright Whitening"
    )
    assert result.product is None
    assert result.candidates == []


# ── Batch matching ────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_batch_matches_a_whole_receipt(catalogue: dict[str, Product]) -> None:
    results = matching.match_line_items(
        [
            ("HEINZ KETCHUP 750ML", "Heinz Tomato Ketchup 750ml"),
            ("TIDE ORIG HE 1.09L", "Tide Laundry Detergent Original 1.09L"),
            ("BNLS CHKN BRST KG", "Chicken Breast Boneless per kg"),
        ]
    )
    assert results[0].product == catalogue["Heinz Tomato Ketchup"]
    assert results[1].product == catalogue["Tide Laundry Detergent Original"]
    assert results[2].product is None


# ── The labelled fixture set ──────────────────────────────────────────────────


@pytest.mark.django_db
def test_labelled_fixture_set_matches_at_current_thresholds(
    catalogue: dict[str, Product],
) -> None:
    """The point of the fixture set: judge threshold changes by their effect.

    A failure here after tuning MatchConfig means the new numbers traded away
    accuracy somewhere, and says exactly where.
    """
    wrong = []
    for raw, interpreted, expected in fixtures_receipts.LABELLED:
        result = matching.match_line_item(raw, interpreted)
        actual = result.product.canonical_name if result.product else None
        if actual != expected:
            wrong.append(f"{raw!r} → {actual!r}, expected {expected!r}")
    assert not wrong, "Mismatches:\n" + "\n".join(wrong)
