from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError
from django.utils import timezone

from accounts.models import Player
from points.models import PointTransaction
from spendium import service
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
from spendium.normalisation import normalise_raw_text


@pytest.fixture
def store(db: None) -> Store:
    return Store.objects.create(name="Shoppers Drug Mart")


@pytest.fixture
def product(db: None) -> Product:
    return Product.objects.create(canonical_name="Colgate Toothpaste Bright Whitening")


@pytest.fixture
def shopper(db: None) -> Player:
    return Player.objects.create_user(username="shopper", email="shopper@example.com")


@pytest.fixture
def purchase(shopper: Player, store: Store, product: Product) -> Purchase:
    p = Purchase.objects.create(
        player=shopper,
        store=store,
        purchased_at=timezone.now(),
        total=Decimal("12.49"),
    )
    PurchaseLineItem.objects.create(
        purchase=p,
        raw_text="TP-COLG-250",
        interpreted_name="Colgate Toothpaste Bright Whitening",
        product=product,
        match_tier=MatchTier.ALIAS,
        match_confidence=Decimal("1.000"),
        line_total=Decimal("4.99"),
    )
    PurchaseLineItem.objects.create(
        purchase=p,
        raw_text="BNLS CHKN BRST KG",
        line_total=Decimal("7.50"),
    )
    return p


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


# ── Purchase retention window ─────────────────────────────────────────────────


@pytest.mark.django_db
def test_anonymise_after_defaults_to_retention_window(purchase: Purchase) -> None:
    expected = purchase.created_at + timedelta(days=30)
    assert abs((purchase.anonymise_after - expected).total_seconds()) < 60


@pytest.mark.django_db
def test_window_open_within_retention(purchase: Purchase) -> None:
    assert purchase.window_is_open is True


@pytest.mark.django_db
def test_window_closed_past_retention(purchase: Purchase) -> None:
    purchase.anonymise_after = timezone.now() - timedelta(seconds=1)
    purchase.save()
    assert purchase.window_is_open is False


# ── Anonymisation ─────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_anonymisation_deletes_the_player_linked_row(purchase: Purchase) -> None:
    """Deletion, not nulling — a row that does not exist cannot be joined to."""
    service.anonymise_purchase(purchase.pk)
    assert not Purchase.objects.filter(pk=purchase.pk).exists()
    assert PurchaseLineItem.objects.count() == 0


@pytest.mark.django_db
def test_anonymisation_preserves_line_item_content(purchase: Purchase) -> None:
    service.anonymise_purchase(purchase.pk)
    anon = AnonymisedPurchase.objects.get()
    assert anon.total == Decimal("12.49")
    assert sorted(i.raw_text for i in anon.line_items.all()) == [
        "BNLS CHKN BRST KG",
        "TP-COLG-250",
    ]


@pytest.mark.django_db
def test_anonymisation_preserves_raw_text_for_retro_matching(
    purchase: Purchase,
) -> None:
    """Once the image is gone, raw_text is the only durable record of the buy."""
    service.anonymise_purchase(purchase.pk)
    unmatched = AnonymisedLineItem.objects.get(match_tier=MatchTier.UNMATCHED)
    assert unmatched.raw_text == "BNLS CHKN BRST KG"
    assert unmatched.raw_text_normalised == "bnls chkn brst kg"


@pytest.mark.django_db
def test_anonymisation_preserves_match_state(
    purchase: Purchase, product: Product
) -> None:
    service.anonymise_purchase(purchase.pk)
    matched = AnonymisedLineItem.objects.get(match_tier=MatchTier.ALIAS)
    assert matched.product_id == product.pk
    assert matched.match_confidence == Decimal("1.000")


@pytest.mark.django_db
def test_anonymised_row_has_no_route_back_to_a_player(purchase: Purchase) -> None:
    anon_fields = {f.name for f in AnonymisedPurchase._meta.get_fields()}
    assert "player" not in anon_fields
    assert "purchase" not in anon_fields


@pytest.mark.django_db
def test_tokens_are_random_and_unique_per_basket(
    purchase: Purchase, shopper: Player, store: Store
) -> None:
    """Two baskets from one player must not be linkable through their tokens."""
    second = Purchase.objects.create(
        player=shopper,
        store=store,
        purchased_at=timezone.now(),
        total=Decimal("3.00"),
    )
    PurchaseLineItem.objects.create(
        purchase=second, raw_text="MILK 2L", line_total=Decimal("3.00")
    )

    service.anonymise_purchase(purchase.pk)
    service.anonymise_purchase(second.pk)

    tokens = list(AnonymisedPurchase.objects.values_list("purchase_token", flat=True))
    assert len(set(tokens)) == 2
    # Version 4 means randomly generated, not derived from anything.
    assert all(token.version == 4 for token in tokens)


@pytest.mark.django_db
def test_all_line_items_of_a_basket_share_one_token(purchase: Purchase) -> None:
    """Basket-level co-purchase analysis has to survive anonymisation."""
    service.anonymise_purchase(purchase.pk)
    tokens = {
        i.anonymised_purchase.purchase_token for i in AnonymisedLineItem.objects.all()
    }
    assert len(tokens) == 1


@pytest.mark.django_db
def test_anonymisation_severs_the_points_ledger_reference(
    purchase: Purchase, shopper: Player
) -> None:
    """The ledger is permanent, but must not point at basket detail.

    object_id has no FK constraint, so without this the integer would survive
    the delete and still name the purchase.
    """
    txn = PointTransaction.objects.create(
        player=shopper,
        amount=Decimal("25.00"),
        reason="purchase",
        content_type=ContentType.objects.get_for_model(Purchase),
        object_id=purchase.pk,
    )
    service.anonymise_purchase(purchase.pk)
    txn.refresh_from_db()
    assert txn.content_type_id is None
    assert txn.object_id is None
    assert txn.amount == Decimal("25.00")


@pytest.mark.django_db
def test_anonymisation_is_idempotent(purchase: Purchase) -> None:
    """A redelivered Cloud Task must not duplicate the anonymous record."""
    assert service.anonymise_purchase(purchase.pk) is True
    assert service.anonymise_purchase(purchase.pk) is False
    assert AnonymisedPurchase.objects.count() == 1


@pytest.mark.django_db
def test_anonymising_an_unknown_purchase_is_a_no_op() -> None:
    assert service.anonymise_purchase(999999) is False


@pytest.mark.django_db
def test_deleting_an_account_removes_player_linked_purchases(
    purchase: Purchase, shopper: Player
) -> None:
    shopper.delete()
    assert Purchase.objects.count() == 0
    assert PurchaseLineItem.objects.count() == 0


# ── Sweeper ───────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_sweeper_ignores_purchases_inside_their_window(purchase: Purchase) -> None:
    assert service.due_purchase_ids() == []


@pytest.mark.django_db
def test_sweeper_finds_overdue_purchases(purchase: Purchase) -> None:
    """A purchase outliving its window is a privacy failure, not a cosmetic one."""
    Purchase.objects.filter(pk=purchase.pk).update(
        anonymise_after=timezone.now() - timedelta(days=1)
    )
    assert service.due_purchase_ids() == [purchase.pk]


@pytest.mark.django_db
def test_image_expiry_sweep_respects_the_published_commitment(
    purchase: Purchase,
) -> None:
    assert service.expired_image_purchase_ids() == []
    Purchase.objects.filter(pk=purchase.pk).update(
        created_at=timezone.now() - timedelta(hours=25)
    )
    assert service.expired_image_purchase_ids() == [purchase.pk]


@pytest.mark.django_db
def test_image_expiry_sweep_skips_already_deleted_images(purchase: Purchase) -> None:
    Purchase.objects.filter(pk=purchase.pk).update(
        created_at=timezone.now() - timedelta(hours=25),
        image_deleted_at=timezone.now(),
    )
    assert service.expired_image_purchase_ids() == []
