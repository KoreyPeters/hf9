import pytest
from django.db import IntegrityError

from spendium.models import Product, ProductAlias, Store
from spendium.normalisation import normalise_raw_text


@pytest.fixture
def store(db: None) -> Store:
    return Store.objects.create(name="Shoppers Drug Mart")


@pytest.fixture
def product(db: None) -> Product:
    return Product.objects.create(canonical_name="Colgate Toothpaste Bright Whitening")


# ── Normalisation ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("TP-COLG-250", "tp colg 250"),
        ("TP  COLG   250", "tp colg 250"),
        ("tp/colg/250", "tp colg 250"),
        ("  TP-COLG-250  ", "tp colg 250"),
        ("CAFÉ BIO 300G", "cafe bio 300g"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalise_raw_text(raw: str | None, expected: str) -> None:
    assert normalise_raw_text(raw) == expected


def test_normalisation_collapses_punctuation_variants() -> None:
    """Different printings of one string must land on the same key."""
    assert normalise_raw_text("TP-COLG-250") == normalise_raw_text("TP COLG 250")
    assert normalise_raw_text("TP.COLG.250") == normalise_raw_text("tp_colg_250")


def test_normalisation_preserves_digits() -> None:
    """Digits distinguish receipt strings even though size is not product identity."""
    assert normalise_raw_text("TP-COLG-250") != normalise_raw_text("TP-COLG-100")


# ── Alias status transitions ──────────────────────────────────────────────────


@pytest.mark.django_db
def test_alias_normalises_on_save(product: Product, store: Store) -> None:
    alias = ProductAlias.objects.create(
        product=product, store=store, raw_text="TP-COLG-250"
    )
    assert alias.raw_text_normalised == "tp colg 250"


@pytest.mark.django_db
def test_new_alias_starts_provisional_and_unconfirmed(
    product: Product, store: Store
) -> None:
    alias = ProductAlias.objects.create(
        product=product, store=store, raw_text="TP-COLG-250"
    )
    assert alias.status == ProductAlias.STATUS_PROVISIONAL
    assert alias.net_confirmations == 0


@pytest.mark.django_db
def test_single_confirmation_stays_provisional(product: Product, store: Store) -> None:
    """One confirmation is not enough — a lone mis-tap must not become authoritative."""
    alias = ProductAlias.objects.create(
        product=product, store=store, raw_text="TP-COLG-250"
    )
    alias.confirm()
    assert alias.status == ProductAlias.STATUS_PROVISIONAL


@pytest.mark.django_db
def test_second_confirmation_promotes_to_authoritative(
    product: Product, store: Store
) -> None:
    alias = ProductAlias.objects.create(
        product=product, store=store, raw_text="TP-COLG-250"
    )
    alias.confirm()
    alias.confirm()
    assert alias.status == ProductAlias.STATUS_AUTHORITATIVE
    assert alias.net_confirmations == 2


@pytest.mark.django_db
def test_contradiction_demotes_authoritative_to_provisional(
    product: Product, store: Store
) -> None:
    """A disputed alias reopens for prompting rather than disappearing."""
    alias = ProductAlias.objects.create(
        product=product, store=store, raw_text="TP-COLG-250"
    )
    alias.confirm()
    alias.confirm()
    alias.contradict()
    assert alias.status == ProductAlias.STATUS_PROVISIONAL


@pytest.mark.django_db
def test_sustained_contradiction_demotes(product: Product, store: Store) -> None:
    """Net evidence at or below zero means the alias stops being used."""
    alias = ProductAlias.objects.create(
        product=product, store=store, raw_text="TP-COLG-250"
    )
    alias.confirm()
    alias.contradict()
    assert alias.status == ProductAlias.STATUS_DEMOTED


@pytest.mark.django_db
def test_demoted_alias_can_recover(product: Product, store: Store) -> None:
    alias = ProductAlias.objects.create(
        product=product, store=store, raw_text="TP-COLG-250"
    )
    alias.contradict()
    assert alias.status == ProductAlias.STATUS_DEMOTED
    alias.confirm()
    alias.confirm()
    assert alias.status == ProductAlias.STATUS_PROVISIONAL


@pytest.mark.django_db
def test_transitions_persist(product: Product, store: Store) -> None:
    alias = ProductAlias.objects.create(
        product=product, store=store, raw_text="TP-COLG-250"
    )
    alias.confirm()
    alias.confirm()
    assert (
        ProductAlias.objects.get(pk=alias.pk).status
        == ProductAlias.STATUS_AUTHORITATIVE
    )


# ── Alias uniqueness ──────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_duplicate_alias_within_a_store_is_rejected(
    product: Product, store: Store
) -> None:
    ProductAlias.objects.create(product=product, store=store, raw_text="TP-COLG-250")
    with pytest.raises(IntegrityError):
        ProductAlias.objects.create(
            product=product, store=store, raw_text="TP COLG 250"
        )


@pytest.mark.django_db
def test_one_string_at_one_store_maps_to_one_product(
    product: Product, store: Store
) -> None:
    """Tier 0 must be unambiguous: two products cannot claim the same string.

    The uniqueness is global rather than per product, which is what lets an
    exact alias lookup return a single answer without further scoring.
    """
    other_product = Product.objects.create(canonical_name="Crest 3D White Toothpaste")
    ProductAlias.objects.create(product=product, store=store, raw_text="TP-COLG-250")
    with pytest.raises(IntegrityError):
        ProductAlias.objects.create(
            product=other_product, store=store, raw_text="TP-COLG-250"
        )


@pytest.mark.django_db
def test_same_string_may_differ_between_stores(product: Product, store: Store) -> None:
    """A string means whatever the retailer that printed it means by it."""
    other = Store.objects.create(name="Loblaws")
    ProductAlias.objects.create(product=product, store=store, raw_text="TP-COLG-250")
    ProductAlias.objects.create(product=product, store=other, raw_text="TP-COLG-250")
    assert ProductAlias.objects.filter(raw_text_normalised="tp colg 250").count() == 2


@pytest.mark.django_db
def test_duplicate_global_alias_is_rejected(product: Product) -> None:
    """NULL store is distinct in SQL, so this needs its own constraint to hold."""
    ProductAlias.objects.create(product=product, store=None, raw_text="TP-COLG-250")
    with pytest.raises(IntegrityError):
        ProductAlias.objects.create(product=product, store=None, raw_text="tp colg 250")


# ── Merge resolution ──────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_unmerged_product_resolves_to_itself(product: Product) -> None:
    assert product.resolve_canonical().pk == product.pk


@pytest.mark.django_db
def test_merged_product_resolves_to_target(product: Product) -> None:
    target = Product.objects.create(canonical_name="Colgate Toothpaste Whitening")
    product.merged_into = target
    product.status = Product.STATUS_RETIRED
    product.save()
    assert product.resolve_canonical().pk == target.pk


@pytest.mark.django_db
def test_merge_resolution_is_transitive(product: Product) -> None:
    """A merges into B, B later merges into C — A must resolve all the way to C."""
    middle = Product.objects.create(canonical_name="Middle")
    final = Product.objects.create(canonical_name="Final")
    product.merged_into = middle
    product.save()
    middle.merged_into = final
    middle.save()
    assert product.resolve_canonical().pk == final.pk


@pytest.mark.django_db
def test_merge_resolution_survives_a_cycle(product: Product) -> None:
    """A corrupt cycle must terminate rather than hang the request."""
    other = Product.objects.create(canonical_name="Other")
    product.merged_into = other
    product.save()
    other.merged_into = product
    other.save()
    assert product.resolve_canonical() is not None


# ── SQIDs ─────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_product_and_store_get_distinct_sqids(product: Product, store: Store) -> None:
    product.refresh_from_db()
    store.refresh_from_db()
    assert product.sqid
    assert store.sqid
    assert product.sqid != store.sqid
