"""Store ratings.

The points half of this has been live and tested since `points.store_points`
was written — the moment survey responses exist against a `Store`, players earn
at that store's rate. What was missing was everything a player could see, and
everything that produces those responses in the first place.
"""

from decimal import Decimal

import pytest
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from django.utils import timezone

from accounts.models import Player
from spendium import ratings
from spendium.models import MatchConfig, Purchase, Store, StoreRatingSnapshot
from surveys.models import Category, Criterion, SurveyConfig
from surveys.service import submit_survey


@pytest.fixture
def store(db: None) -> Store:
    return Store.objects.create(name="Shoppers Drug Mart")


@pytest.fixture
def store_criteria(db: None, store: Store) -> list[Criterion]:
    category = Category.objects.create(
        name="Store ethics",
        description="",
        game="spendium",
        subject_type=ContentType.objects.get_for_model(Store),
    )
    return [
        Criterion.objects.create(
            category=category, question="Living wage?", weight=100
        ),
        Criterion.objects.create(
            category=category, question="Fair suppliers?", weight=100
        ),
    ]


def make_player(name: str) -> Player:
    return Player.objects.create_user(username=name, email=f"{name}@example.com")


def shop_at(player: Player, store: Store) -> Purchase:
    return Purchase.objects.create(
        player=player, store=store, purchased_at=timezone.now(), total=Decimal("50.00")
    )


def rate_store(player, store, criteria, answers, verified=True) -> None:
    submit_survey(
        player,
        store,
        {c.pk: a for c, a in zip(criteria, answers, strict=True)},
        is_verified=verified,
    )


# ── Aggregation ───────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_an_unrated_store_has_no_score(store: Store) -> None:
    result = ratings.compute_store(store)
    assert result.score is None
    assert result.displayable is False


@pytest.mark.django_db
def test_a_receipt_makes_a_rating_weigh_more(store: Store, store_criteria) -> None:
    """Someone who never shopped there may still have an opinion, worth less."""
    rate_store(
        make_player("shopper"), store, store_criteria, [True, True], verified=True
    )
    rate_store(
        make_player("passerby"), store, store_criteria, [False, False], verified=False
    )

    # Verified yes at full weight against unverified no at 0.4.
    assert ratings.compute_store(store).score > Decimal("0.5")


@pytest.mark.django_db
def test_a_non_shopper_may_rate_and_is_recorded_unverified(
    store: Store, store_criteria
) -> None:
    """Anyone signed in may rate any store. Whether they shopped there decides
    `is_verified`, not eligibility."""
    from surveys.models import SurveyResponse

    player = make_player("nobody")
    assert ratings.player_has_shopped_at(player, store) is False

    rate_store(
        player,
        store,
        store_criteria,
        [True, True],
        verified=ratings.player_has_shopped_at(player, store),
    )

    response = SurveyResponse.objects.get()
    assert response.is_verified is False
    assert ratings.compute_store(store).response_count == 1


@pytest.mark.django_db
def test_shopping_there_makes_the_response_verified(
    store: Store, store_criteria
) -> None:
    player = make_player("shopper")
    shop_at(player, store)
    assert ratings.player_has_shopped_at(player, store) is True


@pytest.mark.django_db
def test_the_display_threshold_applies(store: Store, store_criteria) -> None:
    config = MatchConfig.get()
    config.min_rating_responses = 3
    config.save()

    rate_store(make_player("a"), store, store_criteria, [True, True])
    assert ratings.compute_store(store).displayable is False

    rate_store(make_player("b"), store, store_criteria, [True, True])
    rate_store(make_player("c"), store, store_criteria, [True, True])
    assert ratings.compute_store(store).displayable is True


@pytest.mark.django_db
def test_a_store_rating_has_no_publish_gate(store: Store, store_criteria) -> None:
    """Products withhold sparse aggregates so an individual basket cannot be
    reverse-engineered. "People who shopped at Loblaws think X" carries no such
    risk, so displayable and publishable are the same thing here."""
    config = MatchConfig.get()
    config.min_rating_responses = 1
    config.save()

    rate_store(make_player("a"), store, store_criteria, [True, True])
    result = ratings.compute_store(store)
    assert result.displayable is True
    assert result.publishable is True


# ── Points ────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_an_unrated_store_still_pays_the_floor(store: Store, store_criteria) -> None:
    """The floor is what a player gets until the catalogue catches up. Rating
    stores must not make an unrated shop worth nothing."""
    from spendium import points

    player = make_player("shopper")
    purchase = shop_at(player, store)
    from spendium.models import PurchaseLineItem

    PurchaseLineItem.objects.create(
        purchase=purchase, raw_text="THING", line_total=Decimal("50.00")
    )

    assert ratings.store_points_per_dollar(store) == Decimal("0")
    assert points.calculate(purchase) > Decimal("0")


@pytest.mark.django_db
def test_a_store_pays_once_enough_players_agree(store: Store, store_criteria) -> None:
    """`compute_declaration_points` excludes a criterion below k entirely rather
    than scoring it zero, so the payout is the gate worth defrauding and it needs
    k distinct players."""
    threshold = SurveyConfig.get().min_survey_threshold

    for i in range(threshold - 1):
        rate_store(make_player(f"p{i}"), store, store_criteria, [True, True])
    assert ratings.store_points_per_dollar(store) == Decimal("0")

    rate_store(make_player("last"), store, store_criteria, [True, True])
    assert ratings.store_points_per_dollar(store) > Decimal("0")


@pytest.mark.django_db
def test_a_rating_ageing_out_of_the_window_stops_counting(
    store: Store, store_criteria
) -> None:
    """The score is computed over a rolling window, so an old opinion stops
    contributing — which is the whole reason snapshots exist."""
    from datetime import timedelta

    from surveys.models import SurveyResponse

    rate_store(make_player("a"), store, store_criteria, [True, True])
    assert ratings.compute_store(store).response_count == 1

    SurveyResponse.objects.update(
        submitted_at=timezone.now() - timedelta(days=ratings.RATING_WINDOW_DAYS + 1)
    )
    assert ratings.compute_store(store).response_count == 0


# ── Snapshots ─────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_snapshots_record_the_score_and_the_payout(
    store: Store, store_criteria
) -> None:
    threshold = SurveyConfig.get().min_survey_threshold
    for i in range(threshold):
        rate_store(make_player(f"p{i}"), store, store_criteria, [True, True])

    assert ratings.snapshot_all_stores() == 1
    snapshot = StoreRatingSnapshot.objects.get()
    assert snapshot.score == Decimal("1.000")
    assert snapshot.points_per_dollar > 0


@pytest.mark.django_db
def test_an_unrated_store_leaves_no_trend_line(store: Store) -> None:
    """A flat line at zero would read as "rated badly" rather than "not rated"."""
    assert ratings.snapshot_all_stores() == 0
    assert not StoreRatingSnapshot.objects.exists()


# ── Surfaces ──────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_the_store_page_leads_with_points_per_dollar(
    client, store: Store, store_criteria
) -> None:
    store.refresh_from_db()
    response = client.get(reverse("spendium:store_detail", args=[store.sqid]))
    assert response.status_code == 200
    assert b"points per dollar" in response.content


@pytest.mark.django_db
def test_the_store_page_offers_the_right_questions(
    client, store: Store, store_criteria
) -> None:
    """The scoping change, seen from the page. Product questions must not appear
    here and store questions must not appear on a product page."""
    from spendium.models import Product

    product_category = Category.objects.create(
        name="Product ethics",
        description="",
        game="spendium",
        subject_type=ContentType.objects.get_for_model(Product),
    )
    Criterion.objects.create(
        category=product_category, question="Product-only question?", weight=50
    )

    store.refresh_from_db()
    client.force_login(make_player("rater"))
    response = client.get(reverse("spendium:store_detail", args=[store.sqid]))

    assert b"Living wage?" in response.content
    assert b"Product-only question?" not in response.content


@pytest.mark.django_db
def test_submitting_a_store_rating_records_it(
    client, store: Store, store_criteria
) -> None:
    import json

    store.refresh_from_db()
    player = make_player("rater")
    client.force_login(player)

    resp = client.post(
        reverse("spendium:submit_store_survey", args=[store.sqid]),
        data=json.dumps({f"criterion_{store_criteria[0].pk}": True}),
        content_type="application/json",
        headers={"Datastar-Request": "true"},
    )
    assert resp.status_code == 200

    assert ratings.compute_store(store).response_count == 1


@pytest.mark.django_db
def test_the_whole_loop_pays_more_after_the_shop_is_rated(
    store: Store, store_criteria
) -> None:
    """Survey → rating → interest → action → survey, closed.

    The point of the game: rating where you shop changes what shopping there is
    worth. Two identical baskets, the second bought after the store cleared the
    k threshold, and the second pays more for no other reason.
    """
    from spendium import points
    from spendium.models import PurchaseLineItem

    def basket() -> Purchase:
        purchase = shop_at(make_player(f"buyer{Purchase.objects.count()}"), store)
        PurchaseLineItem.objects.create(
            purchase=purchase, raw_text="THING", line_total=Decimal("50.00")
        )
        return purchase

    before = points.calculate(basket())

    threshold = SurveyConfig.get().min_survey_threshold
    for i in range(threshold):
        rate_store(make_player(f"rater{i}"), store, store_criteria, [True, True])

    after = points.calculate(basket())

    assert ratings.store_points_per_dollar(store) > Decimal("0")
    assert after > before, (
        "rating the shop did not change what shopping there pays — the loop the "
        "whole game is built on"
    )


@pytest.mark.django_db
def test_a_shop_you_have_not_rated_appears_in_the_action_centre(
    store: Store, store_criteria
) -> None:
    from spendium import action_centre

    player = make_player("shopper")
    shop_at(player, store)

    centre = action_centre.build(player)
    assert [s.pk for s in centre.unrated_stores] == [store.pk]

    rate_store(player, store, store_criteria, [True, True])
    assert action_centre.build(player).unrated_stores == []
