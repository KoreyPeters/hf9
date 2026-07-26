"""Purchase points — the reward the whole pipeline exists to deliver."""

from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import Player
from points.models import PointTransaction
from spendium import points, service
from spendium.conftest import FakeClient
from spendium.models import Product, Purchase, PurchaseLineItem, Store
from spendium.test_extraction import png_bytes, receipt_payload, upload_and_process


@pytest.fixture
def shopper(db: None) -> Player:
    player = Player.objects.create_user(username="shopper", email="s@example.com")
    # award_points pays nothing to unverified accounts.
    Player.objects.filter(pk=player.pk).update(email_verified=True)
    player.refresh_from_db()
    return player


@pytest.fixture
def store(db: None) -> Store:
    return Store.objects.create(name="Shoppers Drug Mart")


@pytest.fixture(autouse=True)
def isolated_media(settings, tmp_path) -> None:
    settings.MEDIA_ROOT = tmp_path


def make_purchase(player: Player, store: Store, *totals: str) -> Purchase:
    purchase = Purchase.objects.create(
        player=player,
        store=store,
        purchased_at=timezone.now(),
        total=Decimal("10.00"),
        processing_status=Purchase.STATUS_PROCESSED,
    )
    for i, total in enumerate(totals):
        PurchaseLineItem.objects.create(
            purchase=purchase, raw_text=f"ITEM {i}", line_total=Decimal(total)
        )
    return purchase


# ── The formula ───────────────────────────────────────────────────────────────


def rate_subject(player: Player, subject, *values: str) -> None:
    """Give a subject criteria worth `values`, satisfied by enough players.

    compute_declaration_points ignores criteria below the k-threshold, so a
    rating only counts once enough people have answered — which is also what
    stops anyone inflating their own payout.
    """
    from surveys.models import Category, Criterion, SurveyConfig
    from surveys.service import submit_survey

    category, _ = Category.objects.get_or_create(
        name="Ethics", defaults={"description": "", "game": "spendium"}
    )
    criteria = [
        Criterion.objects.create(category=category, question=f"q{i}", weight=Decimal(v))
        for i, v in enumerate(values)
    ]
    for i in range(SurveyConfig.get().min_survey_threshold):
        voter = Player.objects.create_user(
            username=f"voter{subject.pk}-{i}-{id(subject)}",
            email=f"v{subject.pk}{i}{id(subject)}@example.com",
        )
        submit_survey(voter, subject, {c.pk: True for c in criteria})


@pytest.mark.django_db
def test_an_unrated_purchase_earns_the_floor(shopper: Player, store: Store) -> None:
    """Paying a genuine player nothing because the catalogue has not caught up
    is a worse failure than paying them something imprecise."""
    purchase = make_purchase(shopper, store, "10.00")
    # Floor of 2 points per dollar, receipt verification 1.0.
    assert points.calculate(purchase) == Decimal("20.00")


@pytest.mark.django_db
def test_self_reporting_halves_the_floor(shopper: Player, store: Store) -> None:
    """The day-one incentive to upload, before anything has been rated."""
    purchase = make_purchase(shopper, store, "10.00")
    purchase.verification_method = Purchase.METHOD_SELF_REPORT
    purchase.save()
    assert points.calculate(purchase) == Decimal("10.00")


@pytest.mark.django_db
def test_points_per_dollar_is_the_sum_of_criterion_values(
    shopper: Player, store: Store
) -> None:
    """Σ(criterion value × probability), with no ceiling.

    A rating expressed as a fraction would cap the reward at what was spent and
    remove the incentive the arms race depends on.
    """
    rate_subject(shopper, store, "10", "100")
    purchase = make_purchase(shopper, store, "10.00")
    # All criteria satisfied: ppd = 110, so $10 earns far more than $10.
    assert points.calculate(purchase) == Decimal("1100.00")


@pytest.mark.django_db
def test_ratings_overtake_the_floor(shopper: Player, store: Store) -> None:
    """The floor is a floor, not a bonus — it does not inflate a real rating."""
    rate_subject(shopper, store, "50")
    purchase = make_purchase(shopper, store, "10.00")
    assert points.calculate(purchase) == Decimal("500.00")


@pytest.mark.django_db
def test_products_earn_on_top_of_the_store(shopper: Player, store: Store) -> None:
    """Telling us what you bought is worth much more than where you shopped."""
    product = Product.objects.create(canonical_name="Heinz Ketchup")
    rate_subject(shopper, store, "10")
    rate_subject(shopper, product, "40")

    purchase = make_purchase(shopper, store)
    PurchaseLineItem.objects.create(
        purchase=purchase,
        raw_text="HEINZ",
        product=product,
        line_total=Decimal("10.00"),
    )
    # store 10×10 = 100, product 10×40 = 400.
    assert points.store_points(purchase) == Decimal("100.00")
    assert points.product_points(purchase) == Decimal("400.00")
    assert points.calculate(purchase) == Decimal("500.00")


@pytest.mark.django_db
def test_an_unmatched_line_still_earns_store_points(
    shopper: Player, store: Store
) -> None:
    """A receipt full of products we cannot recognise is never worthless."""
    rate_subject(shopper, store, "10")
    purchase = make_purchase(shopper, store, "10.00")
    assert points.product_points(purchase) == Decimal("0")
    assert points.calculate(purchase) == Decimal("100.00")


@pytest.mark.django_db
def test_a_rating_below_the_threshold_counts_for_nothing(
    shopper: Player, store: Store
) -> None:
    """Nobody can rate their own purchase upward and cash out."""
    from surveys.models import Category, Criterion
    from surveys.service import submit_survey

    category = Category.objects.create(name="E", description="", game="spendium")
    criterion = Criterion.objects.create(
        category=category, question="q", weight=Decimal("500")
    )
    submit_survey(shopper, store, {criterion.pk: True})

    purchase = make_purchase(shopper, store, "10.00")
    assert points.calculate(purchase) == Decimal("20.00")  # floor only


@pytest.mark.django_db
def test_a_purchase_with_no_store_still_earns_the_floor(shopper: Player) -> None:
    purchase = Purchase.objects.create(
        player=shopper, purchased_at=timezone.now(), total=Decimal("10.00")
    )
    PurchaseLineItem.objects.create(
        purchase=purchase, raw_text="X", line_total=Decimal("10.00")
    )
    assert points.calculate(purchase) == Decimal("20.00")


# ── Negative lines ────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_refunds_earn_nothing_and_are_not_subtracted(
    shopper: Player, store: Store
) -> None:
    """A refund should not eat into points earned on the rest of the shop."""
    purchase = make_purchase(shopper, store, "10.00", "-4.00")
    assert points.eligible_spend(purchase) == Decimal("10.00")
    assert points.calculate(purchase) == Decimal("20.00")


@pytest.mark.django_db
def test_negative_lines_are_identified_for_display(
    shopper: Player, store: Store
) -> None:
    purchase = make_purchase(shopper, store, "10.00", "-4.00")
    assert len(points.negative_line_item_ids(purchase)) == 1


@pytest.mark.django_db
def test_an_all_refund_receipt_earns_nothing_and_is_settled(
    shopper: Player, store: Store
) -> None:
    """Stamped anyway, so it is not retried forever."""
    purchase = make_purchase(shopper, store, "-4.00")
    assert points.award_for_purchase(purchase) == Decimal("0")
    purchase.refresh_from_db()
    assert purchase.points_awarded == Decimal("0")


# ── Awarded once ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_points_are_awarded_and_recorded(shopper: Player, store: Store) -> None:
    purchase = make_purchase(shopper, store, "10.00")
    awarded = points.award_for_purchase(purchase)
    assert awarded == Decimal("20.00")
    purchase.refresh_from_db()
    assert purchase.points_awarded == Decimal("20.00")
    assert PointTransaction.objects.filter(reason="purchase").count() == 1


@pytest.mark.django_db
def test_a_second_award_pays_nothing(shopper: Player, store: Store) -> None:
    """Reprocessing and redelivered tasks must not pay twice."""
    purchase = make_purchase(shopper, store, "10.00")
    points.award_for_purchase(purchase)
    assert points.award_for_purchase(purchase) == Decimal("0")
    assert PointTransaction.objects.filter(reason="purchase").count() == 1


@pytest.mark.django_db
def test_membership_multiplies_the_award(shopper: Player, store: Store) -> None:
    from datetime import timedelta

    from accounts.models import Membership

    Membership.objects.create(
        player=shopper, expires_at=timezone.now() + timedelta(days=365)
    )
    purchase = make_purchase(shopper, store, "10.00")
    # Floor of 20, then the member multiplier of 1.5.
    assert points.award_for_purchase(purchase) == Decimal("30.00")


# ── The ledger entry ──────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_the_ledger_entry_says_what_it_was_for(shopper: Player, store: Store) -> None:
    """The purchase is anonymised at thirty days; without this the player's
    history becomes an unexplained row of numbers."""
    purchase = make_purchase(shopper, store, "10.00")
    points.award_for_purchase(purchase)
    entry = PointTransaction.objects.get(reason="purchase")
    assert "Shoppers Drug Mart" in entry.description


@pytest.mark.django_db
def test_points_survive_anonymisation(shopper: Player, store: Store) -> None:
    """The reward is permanent; the basket it came from is not."""
    purchase = make_purchase(shopper, store, "10.00")
    points.award_for_purchase(purchase)
    service.anonymise_purchase(purchase.pk)

    entry = PointTransaction.objects.get(reason="purchase")
    assert entry.amount == Decimal("20.00")
    # The link to the basket is severed even though the entry remains.
    assert entry.content_type_id is None
    assert entry.object_id is None


@pytest.mark.django_db
def test_anonymisation_clears_where_and_when_you_shopped(
    shopper: Player, store: Store
) -> None:
    """The description exists only while the purchase does.

    During the window it merely repeats what the purchase row already holds, so
    it costs nothing. Kept afterwards it would be the only surviving record of
    where and when somebody shopped, and years of those amount to a movement
    trace more revealing than the basket anonymisation exists to destroy.
    """
    purchase = make_purchase(shopper, store, "10.00")
    points.award_for_purchase(purchase)
    assert "Shoppers Drug Mart" in PointTransaction.objects.get().description

    service.anonymise_purchase(purchase.pk)

    entry = PointTransaction.objects.get()
    assert entry.description == ""
    # What the player keeps: how much, and when. Frequency, never location.
    assert entry.amount == Decimal("20.00")
    assert entry.created_at is not None


@pytest.mark.django_db
def test_unverified_accounts_earn_nothing(store: Store) -> None:
    """Existing ledger rule: points do not accrue before email verification."""
    player = Player.objects.create_user(username="new", email="n@example.com")
    purchase = make_purchase(player, store, "10.00")
    assert points.award_for_purchase(purchase) == Decimal("0")


# ── Not gated on rating, not repeated by matching ─────────────────────────────


@pytest.mark.django_db
def test_points_are_awarded_when_the_receipt_is_read(
    shopper: Player, fake_model
) -> None:
    """Not when it is rated. Gating would withhold points already earned."""
    purchase = upload_and_process(shopper, fake_model(receipt_payload()))
    assert purchase.points_awarded is not None
    assert purchase.points_awarded > 0


@pytest.mark.django_db
def test_retro_matching_does_not_re_award(shopper: Player, fake_model) -> None:
    """Matching changes which product a line points at, never what it paid."""
    from spendium import retro

    purchase = upload_and_process(shopper, fake_model(receipt_payload()))
    before = purchase.points_awarded
    Product.objects.create(canonical_name="Heinz Ketchup")
    retro.run()

    purchase.refresh_from_db()
    assert purchase.points_awarded == before
    assert PointTransaction.objects.filter(reason="purchase").count() == 1


@pytest.mark.django_db
def test_disambiguation_does_not_re_award(
    shopper: Player, store: Store, fake_model
) -> None:
    from spendium import disambiguation

    purchase = upload_and_process(shopper, fake_model(receipt_payload()))
    before = purchase.points_awarded

    product = Product.objects.create(canonical_name="Anything")
    line = purchase.line_items.first()
    disambiguation.choose(line, product)

    purchase.refresh_from_db()
    assert purchase.points_awarded == before
    assert PointTransaction.objects.filter(reason="purchase").count() == 1


@pytest.mark.django_db
def test_a_failed_receipt_earns_nothing(shopper: Player, fake_model) -> None:
    purchase = upload_and_process(shopper, fake_model("not json at all"))
    assert purchase.processing_status == Purchase.STATUS_FAILED
    assert purchase.points_awarded is None
    assert PointTransaction.objects.filter(reason="purchase").count() == 0


@pytest.mark.django_db
def test_points_show_on_the_purchase_page(client, shopper: Player, fake_model) -> None:
    from django.urls import reverse

    purchase = upload_and_process(shopper, fake_model(receipt_payload()))
    client.force_login(shopper)
    response = client.get(reverse("spendium:purchase_detail", args=[purchase.pk]))
    assert b"points" in response.content


@pytest.mark.django_db
def test_a_receipt_uploaded_by_a_member_pays_once(shopper: Player, fake_model) -> None:
    """End to end: upload, read, pay — and only once."""
    client = fake_model(receipt_payload())
    purchase = service.accept_upload(shopper, png_bytes(), content_type="image/png")
    service.process_receipt(purchase.pk, client=FakeClient(receipt_payload()))
    purchase.refresh_from_db()
    assert PointTransaction.objects.filter(reason="purchase").count() == 1
    assert client is not None
