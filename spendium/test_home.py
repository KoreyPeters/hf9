"""The Spendium home page, front page card, and nav link.

Spendium shipped with no way to find it: /spendium/ was not routed, the front
page said "coming soon", and the nav had no link.

The page is a state machine, and the states exist because the cold-start case is
the *main* case — a rating publishes at ten verified receipt-anchored responses,
so for a long while there is nothing to browse. Most of what is asserted here is
that the page is honest in that state rather than showing an empty shelf.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from accounts.models import Membership, Player
from spendium import service
from spendium.models import (
    Product,
    ProductCategory,
    ProductRatingSnapshot,
    Purchase,
    Store,
)


@pytest.fixture
def shopper(db: None) -> Player:
    return Player.objects.create_user(username="shopper", email="s@example.com")


def a_purchase(player: Player, **kwargs) -> Purchase:
    return Purchase.objects.create(
        player=player,
        purchased_at=timezone.now(),
        total=Decimal("10"),
        **kwargs,
    )


def publishable_products(count: int, *, sensitive: bool = False) -> None:
    """Enough purchases behind each to clear the k-anonymity gate."""
    category = ProductCategory.objects.create(
        name="Sensitive" if sensitive else "Groceries", is_sensitive=sensitive
    )
    for i in range(count):
        product = Product.objects.create(
            canonical_name=f"Product {i}", category=category
        )
        ProductRatingSnapshot.objects.create(
            product=product,
            taken_on=date.today(),
            score=Decimal("0.900"),
            response_count=30,
            verified_count=30,
            purchase_count=30,
        )


# ── The states ────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_it_is_public(client) -> None:
    """The front page needs somewhere to send people that explains the game
    before asking them to sign up."""
    response = client.get(reverse("spendium:home"))

    assert response.status_code == 200
    assert b"Sign up" in response.content


@pytest.mark.django_db
def test_an_anonymous_visitor_is_told_what_happens_to_their_receipt(client) -> None:
    """The first objection a thoughtful person has, answered above the fold
    rather than buried in a policy nobody opens."""
    content = client.get(reverse("spendium:home")).content

    assert b"deleted within 24 hours" in content
    assert reverse("spendium:privacy").encode() in content


@pytest.mark.django_db
def test_a_new_player_is_pointed_at_their_first_receipt(client, shopper) -> None:
    client.force_login(shopper)
    content = client.get(reverse("spendium:home")).content

    assert b"Scan a receipt" in content
    assert reverse("spendium:receipt_upload").encode() in content


@pytest.mark.django_db
def test_a_returning_player_sees_their_own_receipts(client, shopper) -> None:
    """What a returning player actually wants to know is whether the one they
    uploaded this morning worked."""
    store = Store.objects.create(name="Corner Shop")
    a_purchase(shopper, store=store)
    client.force_login(shopper)

    content = client.get(reverse("spendium:home")).content

    assert b"Corner Shop" in content
    assert b"Scan another receipt" in content


@pytest.mark.django_db
def test_spending_points_come_from_the_ledger_not_the_purchases(
    client, shopper
) -> None:
    """A purchase is anonymised after thirty days; its points are not. Summing
    purchases would show a total that shrinks over time."""
    from points.models import PointTransaction

    a_purchase(shopper)
    PointTransaction.objects.create(
        player=shopper, amount=Decimal("250"), reason="purchase"
    )
    PointTransaction.objects.create(
        player=shopper, amount=Decimal("99"), reason="survey"
    )
    client.force_login(shopper)

    assert b"250 points from spending" in client.get(reverse("spendium:home")).content


# ── Discovery only when it earns its place ────────────────────────────────────


@pytest.mark.django_db
def test_no_discovery_section_before_the_floor(client, shopper) -> None:
    """A listing of two says "nobody is here" more loudly than no listing."""
    publishable_products(3)
    a_purchase(shopper)
    client.force_login(shopper)

    assert b"Best rated right now" not in client.get(reverse("spendium:home")).content


@pytest.mark.django_db
def test_discovery_appears_once_there_is_enough(client, shopper) -> None:
    publishable_products(6)
    a_purchase(shopper)
    client.force_login(shopper)

    content = client.get(reverse("spendium:home")).content
    assert b"Best rated right now" in content
    assert b"Product 0" in content


@pytest.mark.django_db
def test_an_unpublishable_product_never_appears(client, shopper) -> None:
    """The listing is an aggregate publication. A product bought by too few
    people to be anonymous must not appear on it however good its score."""
    publishable_products(6)
    lonely = Product.objects.create(canonical_name="Bought By One Person")
    ProductRatingSnapshot.objects.create(
        product=lonely,
        taken_on=date.today(),
        score=Decimal("1.000"),  # the best score of the lot
        response_count=8,
        verified_count=8,
        purchase_count=1,
    )
    a_purchase(shopper)
    client.force_login(shopper)

    assert b"Bought By One Person" not in client.get(reverse("spendium:home")).content


@pytest.mark.django_db
def test_sensitive_categories_carry_the_higher_bar(client, shopper) -> None:
    """PUBLISH_K_SENSITIVE is 25 against PUBLISH_K's 10, and the query has to
    apply the right one per product rather than the loosest."""
    from spendium import ratings

    category = ProductCategory.objects.create(name="Medicines", is_sensitive=True)
    product = Product.objects.create(
        canonical_name="Something Private", category=category
    )
    ProductRatingSnapshot.objects.create(
        product=product,
        taken_on=date.today(),
        score=Decimal("0.950"),
        response_count=20,
        verified_count=20,
        # Clears PUBLISH_K (10) but not PUBLISH_K_SENSITIVE (25).
        purchase_count=15,
    )

    assert ratings.top_rated() == []


@pytest.mark.django_db
def test_only_the_most_recent_snapshot_counts(client, shopper) -> None:
    """Snapshots accumulate daily. Without a date filter every product would
    appear once per day it has ever been rated."""
    from spendium import ratings

    publishable_products(6)
    product = Product.objects.first()
    ProductRatingSnapshot.objects.create(
        product=product,
        taken_on=date.today() - timedelta(days=1),
        score=Decimal("0.100"),
        response_count=30,
        verified_count=30,
        purchase_count=30,
    )

    assert len(ratings.top_rated()) == 6


# ── The free trial ────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_a_new_player_is_told_the_scans_are_free(client, shopper) -> None:
    client.force_login(shopper)

    assert b"free" in client.get(reverse("spendium:home")).content.lower()


@pytest.mark.django_db
def test_the_remaining_count_is_shown_before_it_runs_out(
    client, shopper, settings
) -> None:
    """Stated up front is what makes it read as a gift rather than a trap
    discovered at the eleventh receipt."""
    settings.SPENDIUM = settings.SPENDIUM | {"FREE_TRIAL_UPLOADS": 5}
    a_purchase(shopper)
    client.force_login(shopper)

    assert b"4 free scans left" in client.get(reverse("spendium:home")).content


@pytest.mark.django_db
def test_members_are_not_shown_a_trial_count(client, shopper) -> None:
    Membership.objects.create(
        player=shopper, expires_at=timezone.now() + timedelta(days=365)
    )
    a_purchase(shopper)
    client.force_login(shopper)

    assert b"free scan" not in client.get(reverse("spendium:home")).content


@pytest.mark.django_db
def test_the_trial_never_goes_negative(shopper, settings) -> None:
    settings.SPENDIUM = settings.SPENDIUM | {"FREE_TRIAL_UPLOADS": 1}
    for _ in range(4):
        a_purchase(shopper)

    assert service.trial_uploads_left(shopper) == 0


# ── Findable from the rest of the site ────────────────────────────────────────


@pytest.mark.django_db
def test_the_nav_links_to_spendium(client) -> None:
    content = client.get(reverse("spendium:home")).content

    assert content.count(reverse("spendium:home").encode()) >= 1
    assert b">Spendium</a>" in content


@pytest.mark.django_db
def test_the_front_page_no_longer_says_coming_soon(client) -> None:
    content = client.get(reverse("landing")).content

    assert b"Coming soon" not in content
    assert b"Beta" in content
    assert reverse("spendium:home").encode() in content


# ── The bootstrapping threshold ───────────────────────────────────────────────


@pytest.mark.django_db
def test_the_display_threshold_follows_the_shared_one_by_default() -> None:
    """Nothing changes until it is deliberately set."""
    from spendium import ratings
    from surveys.models import SurveyConfig

    config = SurveyConfig.get()
    config.min_survey_threshold = 7
    config.save()

    assert ratings._min_responses() == 7


@pytest.mark.django_db
def test_spendium_can_lower_it_without_touching_polium() -> None:
    """The whole point of the split. `surveys/ratings.py` reads the shared value
    to decide which criteria count toward Polium's points, so bootstrapping
    Spendium with a global change would quietly alter what Polium pays out."""
    from spendium import ratings
    from spendium.models import MatchConfig
    from surveys.models import SurveyConfig

    config = SurveyConfig.get()
    config.min_survey_threshold = 5
    config.save()

    match_config = MatchConfig.get()
    match_config.min_rating_responses = 1
    match_config.save()

    assert ratings._min_responses() == 1
    assert SurveyConfig.get().min_survey_threshold == 5
