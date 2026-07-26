"""Product ratings.

The two things worth pinning down are the ones the generic survey engine cannot
know about products: that aggregation spans a merge group, and that displaying a
rating and publishing an aggregate are separate decisions answering separate
questions.
"""

from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from accounts.models import Player
from spendium import catalogue, ratings
from spendium.models import (
    AnonymisedLineItem,
    AnonymisedPurchase,
    Product,
    ProductCategory,
    ProductRatingSnapshot,
    Purchase,
    PurchaseLineItem,
    Store,
)
from surveys.models import Category, Criterion, SurveyConfig
from surveys.service import submit_survey


@pytest.fixture
def category(db: None) -> Category:
    return Category.objects.create(
        name="Product ethics", description="", game="spendium"
    )


@pytest.fixture
def criteria(category: Category) -> list[Criterion]:
    return [
        Criterion.objects.create(
            category=category, question="Fair labour?", weight=100
        ),
        Criterion.objects.create(category=category, question="Low harm?", weight=100),
    ]


@pytest.fixture
def product(db: None) -> Product:
    return Product.objects.create(canonical_name="Heinz Tomato Ketchup")


@pytest.fixture
def store(db: None) -> Store:
    return Store.objects.create(name="Shoppers Drug Mart")


def make_player(name: str) -> Player:
    return Player.objects.create_user(username=name, email=f"{name}@example.com")


def buy(player: Player, product: Product, store: Store) -> PurchaseLineItem:
    purchase = Purchase.objects.create(
        player=player, store=store, purchased_at=timezone.now(), total=Decimal("5.00")
    )
    return PurchaseLineItem.objects.create(
        purchase=purchase,
        raw_text="HEINZ KETCHUP",
        product=product,
        line_total=Decimal("5.00"),
    )


def rate(player: Player, product: Product, criteria, answers, verified=True) -> None:
    submit_survey(
        player,
        product,
        {c.pk: a for c, a in zip(criteria, answers, strict=True)},
        is_verified=verified,
    )


# ── Aggregation ───────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_a_rating_is_the_weighted_share_of_yes_answers(
    product: Product, criteria
) -> None:
    rate(make_player("a"), product, criteria, [True, False])
    assert ratings.compute(product).score == Decimal("0.5")


@pytest.mark.django_db
def test_no_responses_means_no_score(product: Product) -> None:
    result = ratings.compute(product)
    assert result.score is None
    assert result.displayable is False


@pytest.mark.django_db
def test_aggregation_spans_the_merge_group(product: Product, criteria) -> None:
    """A rating attaches to whichever record existed when it was given.

    Aggregating one product id would silently drop everything left on a record
    that has since been merged away.
    """
    retired = Product.objects.create(canonical_name="Heinz Ketchup Duplicate")
    rate(make_player("a"), retired, criteria, [True, True])
    catalogue.merge_products(retired, product)

    result = ratings.compute(product)
    assert result.response_count == 1
    assert result.score == Decimal("1")


@pytest.mark.django_db
def test_a_retired_record_reports_the_survivors_rating(
    product: Product, criteria
) -> None:
    retired = Product.objects.create(canonical_name="Duplicate")
    rate(make_player("a"), product, criteria, [True, True])
    catalogue.merge_products(retired, product)
    assert ratings.compute(retired).score == ratings.compute(product).score


# ── Verified against unverified ───────────────────────────────────────────────


@pytest.mark.django_db
def test_unverified_responses_count_for_less(product: Product, criteria) -> None:
    """Someone who did not buy it may still have an opinion, worth less."""
    rate(make_player("bought"), product, criteria, [True, True], verified=True)
    rate(make_player("didnt"), product, criteria, [False, False], verified=False)
    # Verified yes at full weight against unverified no at 0.4.
    assert ratings.compute(product).score > Decimal("0.5")


@pytest.mark.django_db
def test_unverified_responses_never_clear_the_display_threshold(
    product: Product, criteria
) -> None:
    """A product must not get a public rating from people who never bought it."""
    config = SurveyConfig.get()
    for i in range(config.min_survey_threshold + 3):
        rate(make_player(f"p{i}"), product, criteria, [True, True], verified=False)

    result = ratings.compute(product)
    assert result.response_count > config.min_survey_threshold
    assert result.verified_count == 0
    assert result.displayable is False


@pytest.mark.django_db
def test_enough_verified_responses_make_it_displayable(
    product: Product, criteria
) -> None:
    config = SurveyConfig.get()
    for i in range(config.min_survey_threshold):
        rate(make_player(f"p{i}"), product, criteria, [True, False], verified=True)
    assert ratings.compute(product).displayable is True


# ── Publishing: k-anonymity ───────────────────────────────────────────────────


@pytest.mark.django_db
def test_displayable_is_not_publishable(
    product: Product, criteria, store: Store
) -> None:
    """Different questions: is the number meaningful, versus can it expose a basket."""
    config = SurveyConfig.get()
    for i in range(config.min_survey_threshold):
        rate(make_player(f"p{i}"), product, criteria, [True, False], verified=True)

    result = ratings.compute(product)
    assert result.displayable is True
    assert result.publishable is False


@pytest.mark.django_db
def test_enough_purchases_make_it_publishable(
    product: Product, criteria, store: Store, settings
) -> None:
    settings.SPENDIUM = {**settings.SPENDIUM, "PUBLISH_K": 3}
    for i in range(SurveyConfig.get().min_survey_threshold):
        player = make_player(f"p{i}")
        buy(player, product, store)
        rate(player, product, criteria, [True, False], verified=True)
    assert ratings.compute(product).publishable is True


@pytest.mark.django_db
def test_sensitive_categories_need_more_purchases(
    product: Product, criteria, store: Store, settings
) -> None:
    """Health and personal care expose more about someone than groceries do."""
    settings.SPENDIUM = {
        **settings.SPENDIUM,
        "PUBLISH_K": 3,
        "PUBLISH_K_SENSITIVE": 99,
    }
    product.category = ProductCategory.objects.create(
        name="Personal care", is_sensitive=True
    )
    product.save()

    for i in range(SurveyConfig.get().min_survey_threshold):
        player = make_player(f"p{i}")
        buy(player, product, store)
        rate(player, product, criteria, [True, False], verified=True)
    assert ratings.compute(product).publishable is False


@pytest.mark.django_db
def test_purchases_past_the_window_still_count_towards_k(
    product: Product, store: Store
) -> None:
    """The anonymous layer is where most purchase history ends up living."""
    anon = AnonymisedPurchase.objects.create(
        store=store, purchased_at=timezone.now(), total=Decimal("5")
    )
    AnonymisedLineItem.objects.create(
        anonymised_purchase=anon,
        raw_text="HEINZ KETCHUP",
        product=product,
        line_total=Decimal("5"),
    )
    assert ratings.purchase_count(product) == 1


# ── Who may rate ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_a_buyer_is_recognised(product: Product, store: Store) -> None:
    player = make_player("buyer")
    buy(player, product, store)
    assert ratings.player_has_bought(player, product) is True


@pytest.mark.django_db
def test_a_non_buyer_is_not(product: Product) -> None:
    assert ratings.player_has_bought(make_player("nobody"), product) is False


@pytest.mark.django_db
def test_buying_a_merged_away_record_still_counts(
    product: Product, store: Store
) -> None:
    retired = Product.objects.create(canonical_name="Duplicate")
    player = make_player("buyer")
    buy(player, retired, store)
    catalogue.merge_products(retired, product)
    assert ratings.player_has_bought(player, product) is True


# ── Criteria versioning ───────────────────────────────────────────────────────


@pytest.mark.django_db
def test_responses_record_the_criteria_version(
    product: Product, criteria, category: Category
) -> None:
    """A rating means "answers to these questions"; changing them changes it."""
    category.bump_criteria_version()
    submit_survey(
        make_player("a"),
        product,
        {criteria[0].pk: True},
        criteria_version=category.criteria_version,
    )
    from surveys.models import SurveyResponse

    assert SurveyResponse.objects.get().criteria_version == 2


@pytest.mark.django_db
def test_polium_callers_are_unaffected(product: Product, criteria) -> None:
    """The new arguments default to what Polium has always behaved as though it used."""
    submit_survey(make_player("a"), product, {criteria[0].pk: True})
    from surveys.models import SurveyResponse

    response = SurveyResponse.objects.get()
    assert response.is_verified is False
    assert response.criteria_version == 1


# ── Trend ─────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_snapshots_record_todays_rating(product: Product, criteria) -> None:
    rate(make_player("a"), product, criteria, [True, False])
    assert ratings.snapshot_all() == 1
    assert ProductRatingSnapshot.objects.get().score == Decimal("0.500")


@pytest.mark.django_db
def test_snapshotting_twice_in_a_day_overwrites(product: Product, criteria) -> None:
    rate(make_player("a"), product, criteria, [True, False])
    ratings.snapshot_all()
    ratings.snapshot_all()
    assert ProductRatingSnapshot.objects.count() == 1


@pytest.mark.django_db
def test_products_without_a_rating_are_not_snapshotted(product: Product) -> None:
    assert ratings.snapshot_all() == 0


@pytest.mark.django_db
def test_trend_returns_snapshots_oldest_first(product: Product, criteria) -> None:
    rate(make_player("a"), product, criteria, [True, False])
    ratings.snapshot_all()
    points = ratings.trend(product)
    assert len(points) == 1
    assert points[0][1] == Decimal("0.500")


# ── Manufacturer roll-up ──────────────────────────────────────────────────────


@pytest.mark.django_db
def test_manufacturer_rating_weights_by_evidence(criteria) -> None:
    """Otherwise a bad product could be diluted by many uncontroversial ones."""
    from spendium.models import Manufacturer

    maker = Manufacturer.objects.create(name="Acme")
    popular = Product.objects.create(canonical_name="Acme Popular", manufacturer=maker)
    obscure = Product.objects.create(canonical_name="Acme Obscure", manufacturer=maker)

    for i in range(4):
        rate(make_player(f"bad{i}"), popular, criteria, [False, False])
    rate(make_player("good"), obscure, criteria, [True, True])

    result = ratings.manufacturer_rating(maker)
    assert result.score is not None
    assert result.score < Decimal("0.5")


@pytest.mark.django_db
def test_a_manufacturer_with_no_products_has_no_rating() -> None:
    from spendium.models import Manufacturer

    assert (
        ratings.manufacturer_rating(Manufacturer.objects.create(name="Empty")).score
        is None
    )


# ── Views ─────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_product_page_is_public(client, product: Product, criteria) -> None:
    """Ratings should be actionable by people who are not players."""
    product.refresh_from_db()
    response = client.get(reverse("spendium:product_detail", args=[product.sqid]))
    assert response.status_code == 200
    assert b"Heinz Tomato Ketchup" in response.content


@pytest.mark.django_db
def test_a_retired_record_redirects_to_the_survivor(product: Product) -> None:
    from django.test import Client

    retired = Product.objects.create(canonical_name="Duplicate")
    catalogue.merge_products(retired, product)
    retired.refresh_from_db()
    product.refresh_from_db()

    response = Client().get(reverse("spendium:product_detail", args=[retired.sqid]))
    assert response.status_code == 302
    assert product.sqid in response["Location"]


@pytest.mark.django_db
def test_a_buyer_sees_the_survey(
    client, product: Product, criteria, store: Store
) -> None:
    player = make_player("buyer")
    buy(player, product, store)
    product.refresh_from_db()
    client.force_login(player)
    response = client.get(reverse("spendium:product_detail", args=[product.sqid]))
    assert b"Rate this product" in response.content


@pytest.mark.django_db
def test_a_non_buyer_is_told_why_not(client, product: Product, criteria) -> None:
    product.refresh_from_db()
    client.force_login(make_player("nobody"))
    response = client.get(reverse("spendium:product_detail", args=[product.sqid]))
    assert b"once you have bought it" in response.content


@pytest.mark.django_db
def test_provisional_criteria_are_flagged_to_players(
    client, product: Product, criteria, store: Store, category: Category
) -> None:
    """Members deciding the criteria is the point; a stopgap should look like one."""
    player = make_player("buyer")
    buy(player, product, store)
    product.refresh_from_db()
    client.force_login(player)
    response = client.get(reverse("spendium:product_detail", args=[product.sqid]))
    assert b"set by the founder" in response.content
