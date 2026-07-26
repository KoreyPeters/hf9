"""The Action Centre, the badge, and the email rules.

The behaviours worth pinning down are the restraints rather than the features:
the badge must clear, the anonymisation boundary must hold, and routine
housekeeping must never trigger an email.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from accounts.models import Player
from spendium import action_centre
from spendium.models import (
    ActionCentreState,
    Product,
    Purchase,
    PurchaseLineItem,
    Store,
)


@pytest.fixture
def shopper(db: None) -> Player:
    return Player.objects.create_user(username="shopper", email="s@example.com")


@pytest.fixture
def store(db: None) -> Store:
    return Store.objects.create(name="Shoppers Drug Mart")


@pytest.fixture
def product(db: None) -> Product:
    return Product.objects.create(canonical_name="Heinz Ketchup")


def buy(
    player: Player,
    store: Store,
    product: Product | None = None,
    state: str = PurchaseLineItem.STATE_NOT_NEEDED,
) -> PurchaseLineItem:
    purchase = Purchase.objects.create(
        player=player, store=store, purchased_at=timezone.now(), total=Decimal("5")
    )
    return PurchaseLineItem.objects.create(
        purchase=purchase,
        raw_text="ITEM",
        product=product,
        line_total=Decimal("5"),
        disambiguation_state=state,
    )


# ── The three sections ────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_an_empty_centre_is_empty(shopper: Player) -> None:
    assert action_centre.build(shopper).is_empty


@pytest.mark.django_db
def test_unrated_purchases_appear(
    shopper: Player, store: Store, product: Product
) -> None:
    buy(shopper, store, product)
    assert action_centre.build(shopper).unrated == [product]


@pytest.mark.django_db
def test_a_rated_product_drops_off(
    shopper: Player, store: Store, product: Product
) -> None:
    from surveys.models import Category, Criterion
    from surveys.service import submit_survey

    buy(shopper, store, product)
    category = Category.objects.create(name="E", description="", game="spendium")
    criterion = Criterion.objects.create(category=category, question="q", weight=1)
    submit_survey(shopper, product, {criterion.pk: True})

    assert action_centre.build(shopper).unrated == []


@pytest.mark.django_db
def test_pending_lines_appear_as_disambiguations(shopper: Player, store: Store) -> None:
    line = buy(shopper, store, state=PurchaseLineItem.STATE_PENDING)
    assert action_centre.build(shopper).disambiguations == [line]


@pytest.mark.django_db
def test_hot_products_you_bought_appear(
    shopper: Player, store: Store, product: Product
) -> None:
    buy(shopper, store, product)
    action_centre.flag_hot(product)
    assert action_centre.build(shopper).hot == [product]


@pytest.mark.django_db
def test_a_hot_product_you_did_not_buy_does_not_appear(
    shopper: Player, product: Product
) -> None:
    """This is about things the player has a stake in, not a general feed."""
    action_centre.flag_hot(product)
    assert action_centre.build(shopper).hot == []


@pytest.mark.django_db
def test_another_players_purchases_never_appear(
    shopper: Player, store: Store, product: Product
) -> None:
    other = Player.objects.create_user(username="other", email="o@example.com")
    buy(other, store, product)
    assert action_centre.build(shopper).is_empty


@pytest.mark.django_db
def test_the_anonymisation_boundary_empties_the_centre(
    shopper: Player, store: Store, product: Product
) -> None:
    """Once a purchase belongs to nobody, nothing from it can be surfaced."""
    from spendium import service

    line = buy(shopper, store, product, state=PurchaseLineItem.STATE_PENDING)
    action_centre.flag_hot(product)
    assert not action_centre.build(shopper).is_empty

    service.anonymise_purchase(line.purchase_id)
    assert action_centre.build(shopper).is_empty


@pytest.mark.django_db
def test_retired_products_are_not_offered(
    shopper: Player, store: Store, product: Product
) -> None:
    buy(shopper, store, product)
    product.status = Product.STATUS_RETIRED
    product.save()
    assert action_centre.build(shopper).unrated == []


# ── The badge ─────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_a_new_player_sees_everything_as_new(
    shopper: Player, store: Store, product: Product
) -> None:
    buy(shopper, store, product)
    assert action_centre.new_item_count(shopper) > 0


@pytest.mark.django_db
def test_visiting_clears_the_badge(
    shopper: Player, store: Store, product: Product
) -> None:
    """A badge that never clears is one people learn to ignore."""
    buy(shopper, store, product)
    action_centre.mark_visited(shopper)
    assert action_centre.new_item_count(shopper) == 0


@pytest.mark.django_db
def test_the_badge_stays_clear_while_nothing_new_arrives(
    shopper: Player, store: Store, product: Product
) -> None:
    """It must not nag about items already seen and deliberately ignored."""
    buy(shopper, store, product)
    action_centre.mark_visited(shopper)
    assert action_centre.new_item_count(shopper) == 0
    assert action_centre.build(shopper).unrated == [product]  # still outstanding


@pytest.mark.django_db
def test_a_newly_hot_product_brings_the_badge_back(
    shopper: Player, store: Store, product: Product
) -> None:
    buy(shopper, store, product)
    action_centre.mark_visited(shopper)
    action_centre.flag_hot(product)
    assert action_centre.new_item_count(shopper) == 1


@pytest.mark.django_db
def test_a_new_receipt_brings_the_badge_back(
    shopper: Player, store: Store, product: Product
) -> None:
    buy(shopper, store, product)
    action_centre.mark_visited(shopper)
    buy(shopper, store, product)
    assert action_centre.new_item_count(shopper) == 1


# ── Hotness ───────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_trending_products_become_hot(
    shopper: Player, store: Store, product: Product, settings
) -> None:
    settings.SPENDIUM = {**settings.SPENDIUM, "HOT_TRENDING_PURCHASES": 3}
    for _ in range(3):
        buy(shopper, store, product)

    action_centre.recompute_hotness()
    product.refresh_from_db()
    assert product.is_hot
    assert product.hot_reason == Product.HOT_TRENDING


@pytest.mark.django_db
def test_a_quiet_product_does_not_become_hot(
    shopper: Player, store: Store, product: Product, settings
) -> None:
    settings.SPENDIUM = {**settings.SPENDIUM, "HOT_TRENDING_PURCHASES": 10}
    buy(shopper, store, product)
    action_centre.recompute_hotness()
    product.refresh_from_db()
    assert not product.is_hot


@pytest.mark.django_db
def test_a_sharp_rating_move_becomes_hot(product: Product, settings) -> None:
    """Uses the daily snapshots — a rolling rating cannot be recomputed later."""
    from spendium.models import ProductRatingSnapshot

    settings.SPENDIUM = {**settings.SPENDIUM, "HOT_RATING_MOVE": "0.10"}
    today = timezone.now().date()
    ProductRatingSnapshot.objects.create(
        product=product, taken_on=today - timedelta(days=10), score=Decimal("0.900")
    )
    ProductRatingSnapshot.objects.create(
        product=product, taken_on=today, score=Decimal("0.400")
    )

    action_centre.recompute_hotness()
    product.refresh_from_db()
    assert product.hot_reason == Product.HOT_RATING_MOVED


@pytest.mark.django_db
def test_a_manual_flag_survives_the_recompute(product: Product) -> None:
    """Recalls and safety events are exactly what no metric has noticed yet."""
    action_centre.flag_hot(product, manual=True)
    action_centre.recompute_hotness()
    product.refresh_from_db()
    assert product.is_hot
    assert product.hot_is_manual


@pytest.mark.django_db
def test_a_stale_computed_flag_expires(product: Product, settings) -> None:
    settings.SPENDIUM = {**settings.SPENDIUM, "HOT_DURATION_DAYS": 1}
    Product.objects.filter(pk=product.pk).update(
        hot_since=timezone.now() - timedelta(days=5),
        hot_reason=Product.HOT_TRENDING,
        hot_is_manual=False,
    )
    action_centre.recompute_hotness()
    product.refresh_from_db()
    assert not product.is_hot


# ── Email restraint ───────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_onboarding_emails_come_first(shopper: Player) -> None:
    """They exist so players learn the Action Centre is there at all."""
    assert action_centre.email_due(shopper) == "onboarding"


@pytest.mark.django_db
def test_onboarding_stops_after_its_run(shopper: Player, settings) -> None:
    settings.SPENDIUM = {**settings.SPENDIUM, "ONBOARDING_EMAILS": 1}
    action_centre.record_email_sent(shopper, "onboarding")
    ActionCentreState.objects.filter(player=shopper).update(last_email_at=None)
    assert action_centre.email_due(shopper) is None


@pytest.mark.django_db
def test_routine_items_never_trigger_an_email(
    shopper: Player, store: Store, product: Product, settings
) -> None:
    """The restraint that makes the rare important email get read.

    Unrated products and pending disambiguations are surfaced passively, by the
    badge, for people already using the product.
    """
    settings.SPENDIUM = {**settings.SPENDIUM, "ONBOARDING_EMAILS": 0}
    buy(shopper, store, product, state=PurchaseLineItem.STATE_PENDING)
    assert action_centre.build(shopper).total > 0
    assert action_centre.email_due(shopper) is None


@pytest.mark.django_db
def test_a_hot_product_does_trigger_one(
    shopper: Player, store: Store, product: Product, settings
) -> None:
    settings.SPENDIUM = {**settings.SPENDIUM, "ONBOARDING_EMAILS": 0}
    buy(shopper, store, product)
    action_centre.flag_hot(product)
    assert action_centre.email_due(shopper) == "hot"


@pytest.mark.django_db
def test_at_most_one_email_a_week(
    shopper: Player, store: Store, product: Product, settings
) -> None:
    settings.SPENDIUM = {**settings.SPENDIUM, "ONBOARDING_EMAILS": 0}
    buy(shopper, store, product)
    action_centre.flag_hot(product)
    action_centre.record_email_sent(shopper, "hot")
    assert action_centre.email_due(shopper) is None


@pytest.mark.django_db
def test_opting_out_is_honoured_immediately(shopper: Player) -> None:
    state = ActionCentreState.get_for(shopper)
    state.emails_enabled = False
    state.save()
    assert action_centre.email_due(shopper) is None


# ── Views ─────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_the_page_requires_login(client) -> None:
    assert client.get(reverse("spendium:action_centre")).status_code == 302


@pytest.mark.django_db
def test_the_page_renders_each_section(
    client, shopper: Player, store: Store, product: Product
) -> None:
    buy(shopper, store, product, state=PurchaseLineItem.STATE_PENDING)
    action_centre.flag_hot(product)
    client.force_login(shopper)
    response = client.get(reverse("spendium:action_centre"))
    assert response.status_code == 200
    assert b"Needs a look" in response.content
    assert b"Can you check these?" in response.content


@pytest.mark.django_db
def test_visiting_the_page_clears_the_badge(
    client, shopper: Player, store: Store, product: Product
) -> None:
    buy(shopper, store, product)
    client.force_login(shopper)
    client.get(reverse("spendium:action_centre"))
    assert action_centre.new_item_count(shopper) == 0


@pytest.mark.django_db
def test_the_email_preference_can_be_toggled(client, shopper: Player) -> None:
    client.force_login(shopper)
    client.post(reverse("spendium:set_email_preference"), {})
    assert ActionCentreState.get_for(shopper).emails_enabled is False
    client.post(reverse("spendium:set_email_preference"), {"emails_enabled": "on"})
    assert ActionCentreState.get_for(shopper).emails_enabled is True
