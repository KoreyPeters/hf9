"""Convergence metrics and the abuse holds.

The metrics exist to make one claim falsifiable: that the system improves
without curation. So the tests worth having are the ones that would catch a
metric which cannot move, or which moves the wrong way.

The holds exist to bound obvious abuse without punishing the honest majority, so
the tests worth having are the ones that check what is *not* withheld.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import Player
from points.models import PointTransaction
from spendium import abuse, metrics, points
from spendium.models import (
    MatchTier,
    MetricsSnapshot,
    Product,
    ProductAlias,
    Purchase,
    PurchaseLineItem,
    Store,
)


@pytest.fixture
def shopper(db: None) -> Player:
    player = Player.objects.create_user(username="shopper", email="s@example.com")
    Player.objects.filter(pk=player.pk).update(email_verified=True)
    player.refresh_from_db()
    return player


@pytest.fixture
def store(db: None) -> Store:
    return Store.objects.create(name="Shoppers Drug Mart")


def make_purchase(
    player: Player, store: Store | None = None, total="10.00"
) -> Purchase:
    return Purchase.objects.create(
        player=player,
        store=store,
        purchased_at=timezone.now(),
        total=Decimal(total),
        processing_status=Purchase.STATUS_PROCESSED,
    )


def make_line(
    purchase: Purchase, tier=MatchTier.UNMATCHED, state=None
) -> PurchaseLineItem:
    return PurchaseLineItem.objects.create(
        purchase=purchase,
        raw_text="ITEM",
        match_tier=tier,
        line_total=Decimal("5.00"),
        disambiguation_state=state or PurchaseLineItem.STATE_NOT_NEEDED,
    )


# ── Convergence metrics ───────────────────────────────────────────────────────


@pytest.mark.django_db
def test_metrics_are_none_with_no_data() -> None:
    """None rather than zero — nothing measured is not the same as measured zero."""
    assert metrics.alias_hit_rate() is None
    assert metrics.prompt_completion_rate() is None
    assert metrics.alias_demotion_rate() is None


@pytest.mark.django_db
def test_alias_hit_rate_is_the_share_resolved_at_tier_zero(
    shopper: Player, store: Store
) -> None:
    purchase = make_purchase(shopper, store)
    make_line(purchase, MatchTier.ALIAS)
    make_line(purchase, MatchTier.ALIAS)
    make_line(purchase, MatchTier.FUZZY)
    make_line(purchase, MatchTier.UNMATCHED)
    assert metrics.alias_hit_rate() == 0.5


@pytest.mark.django_db
def test_alias_hit_rate_can_be_read_per_store(shopper: Player, store: Store) -> None:
    """Each chain's strings are learned separately, so an average hides a chain
    that is not converging at all."""
    other = Store.objects.create(name="Loblaws")
    make_line(make_purchase(shopper, store), MatchTier.ALIAS)
    make_line(make_purchase(shopper, other), MatchTier.UNMATCHED)

    assert metrics.alias_hit_rate(store) == 1.0
    assert metrics.alias_hit_rate(other) == 0.0
    assert metrics.alias_hit_rate() == 0.5


@pytest.mark.django_db
def test_prompt_completion_counts_only_prompts_actually_shown(
    shopper: Player, store: Store
) -> None:
    """A line that never needed asking about is not an unanswered prompt."""
    purchase = make_purchase(shopper, store)
    make_line(purchase, state=PurchaseLineItem.STATE_NOT_NEEDED)
    make_line(purchase, state=PurchaseLineItem.STATE_PENDING)
    make_line(purchase, state=PurchaseLineItem.STATE_RESOLVED)
    assert metrics.prompt_completion_rate() == 0.5


@pytest.mark.django_db
def test_demotion_rate_is_the_poisoning_detector(
    shopper: Player, store: Store, voter, other_voter
) -> None:
    product = Product.objects.create(canonical_name="X")
    good = ProductAlias.objects.create(product=product, store=store, raw_text="A")
    bad = ProductAlias.objects.create(product=product, store=store, raw_text="B")
    good.confirm(voter)
    bad.contradict(voter)
    bad.contradict(other_voter)
    assert metrics.alias_demotion_rate() == 0.5


@pytest.mark.django_db
def test_summary_reports_every_rate(shopper: Player, store: Store) -> None:
    make_line(make_purchase(shopper, store), MatchTier.ALIAS)
    summary = metrics.summary()
    assert set(summary) == {
        "alias_hit_rate",
        "tier_distribution",
        "prompt_rate",
        "prompt_completion_rate",
        "new_record_rate",
        "auto_merge_rate",
        "alias_demotion_rate",
        "adjudication_accuracy",
        "adjudications_judged",
    }


# ── Snapshots ─────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_a_snapshot_records_the_platform_and_each_store(
    shopper: Player, store: Store
) -> None:
    make_line(make_purchase(shopper, store), MatchTier.ALIAS)
    assert metrics.take_snapshot() == 2  # platform + one store

    platform = MetricsSnapshot.objects.get(store__isnull=True)
    assert platform.alias_hits == 1
    assert platform.alias_hit_rate == 1.0


@pytest.mark.django_db
def test_stores_with_no_line_items_are_skipped(shopper: Player, store: Store) -> None:
    Store.objects.create(name="Never Shopped At")
    make_line(make_purchase(shopper, store), MatchTier.ALIAS)
    assert metrics.take_snapshot() == 2


@pytest.mark.django_db
def test_snapshotting_twice_in_a_day_overwrites(shopper: Player, store: Store) -> None:
    make_line(make_purchase(shopper, store), MatchTier.ALIAS)
    metrics.take_snapshot()
    metrics.take_snapshot()
    assert MetricsSnapshot.objects.filter(store__isnull=True).count() == 1


@pytest.mark.django_db
def test_a_snapshot_of_nothing_still_records_a_row() -> None:
    """An empty day is data. A gap in the series is not."""
    assert metrics.take_snapshot() == 1
    assert MetricsSnapshot.objects.get().line_items == 0


# ── Velocity hold ─────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_normal_submission_is_not_held(shopper: Player, store: Store) -> None:
    purchase = make_purchase(shopper, store)
    assert abuse.evaluate(purchase) == ""


@pytest.mark.django_db
def test_too_many_receipts_in_an_hour_are_held(
    shopper: Player, store: Store, settings
) -> None:
    settings.SPENDIUM = {**settings.SPENDIUM, "VELOCITY_LIMIT_PER_HOUR": 2}
    for _ in range(2):
        make_purchase(shopper, store)
    purchase = make_purchase(shopper, store)
    assert abuse.evaluate(purchase) == Purchase.HOLD_VELOCITY


@pytest.mark.django_db
def test_velocity_only_counts_recent_submissions(
    shopper: Player, store: Store, settings
) -> None:
    settings.SPENDIUM = {**settings.SPENDIUM, "VELOCITY_LIMIT_PER_HOUR": 2}
    old = [make_purchase(shopper, store) for _ in range(5)]
    Purchase.objects.filter(pk__in=[p.pk for p in old]).update(
        created_at=timezone.now() - timedelta(days=2)
    )
    assert abuse.evaluate(make_purchase(shopper, store)) == ""


@pytest.mark.django_db
def test_velocity_is_per_player(shopper: Player, store: Store, settings) -> None:
    """One busy player must not hold everybody else's receipts."""
    settings.SPENDIUM = {**settings.SPENDIUM, "VELOCITY_LIMIT_PER_HOUR": 2}
    busy = Player.objects.create_user(username="busy", email="b@example.com")
    for _ in range(5):
        make_purchase(busy, store)
    assert abuse.evaluate(make_purchase(shopper, store)) == ""


# ── High-value hold ───────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_a_large_receipt_is_held(shopper: Player, store: Store, settings) -> None:
    settings.SPENDIUM = {**settings.SPENDIUM, "HIGH_VALUE_HOLD": "100"}
    purchase = make_purchase(shopper, store, total="250.00")
    assert abuse.evaluate(purchase) == Purchase.HOLD_HIGH_VALUE


@pytest.mark.django_db
def test_an_ordinary_receipt_is_not(shopper: Player, store: Store, settings) -> None:
    settings.SPENDIUM = {**settings.SPENDIUM, "HIGH_VALUE_HOLD": "100"}
    assert abuse.evaluate(make_purchase(shopper, store, total="40.00")) == ""


# ── What a hold does and does not do ──────────────────────────────────────────


@pytest.mark.django_db
def test_a_hold_withholds_points(shopper: Player, store: Store, settings) -> None:
    settings.SPENDIUM = {**settings.SPENDIUM, "HIGH_VALUE_HOLD": "1"}
    purchase = make_purchase(shopper, store)
    make_line(purchase)
    abuse.evaluate(purchase)

    assert points.award_for_purchase(purchase) == Decimal("0")
    assert PointTransaction.objects.filter(reason="purchase").count() == 0


@pytest.mark.django_db
def test_a_hold_leaves_the_purchase_payable_later(
    shopper: Player, store: Store, settings
) -> None:
    """points_awarded stays unset, so releasing pays rather than finding it
    already settled at zero."""
    settings.SPENDIUM = {**settings.SPENDIUM, "HIGH_VALUE_HOLD": "1"}
    purchase = make_purchase(shopper, store)
    make_line(purchase)
    abuse.evaluate(purchase)
    points.award_for_purchase(purchase)

    purchase.refresh_from_db()
    assert purchase.points_awarded is None


@pytest.mark.django_db
def test_releasing_a_hold_pays_out(shopper: Player, store: Store, settings) -> None:
    settings.SPENDIUM = {**settings.SPENDIUM, "HIGH_VALUE_HOLD": "1"}
    purchase = make_purchase(shopper, store)
    make_line(purchase)
    abuse.evaluate(purchase)

    awarded = abuse.release(purchase)
    assert awarded > 0
    purchase.refresh_from_db()
    assert purchase.hold_reason == ""
    assert purchase.points_awarded == awarded


@pytest.mark.django_db
def test_a_hold_does_not_withhold_the_data(
    shopper: Player, store: Store, settings
) -> None:
    """Only the reward waits. The receipt is still read and still counts toward
    ratings — holding the data would punish the honest majority."""
    settings.SPENDIUM = {**settings.SPENDIUM, "HIGH_VALUE_HOLD": "1"}
    purchase = make_purchase(shopper, store)
    line = make_line(purchase, MatchTier.ALIAS)
    abuse.evaluate(purchase)

    purchase.refresh_from_db()
    assert purchase.processing_status == Purchase.STATUS_PROCESSED
    assert purchase.line_items.filter(pk=line.pk).exists()
    assert metrics.alias_hit_rate() == 1.0


@pytest.mark.django_db
def test_holding_is_idempotent(shopper: Player, store: Store, settings) -> None:
    settings.SPENDIUM = {**settings.SPENDIUM, "HIGH_VALUE_HOLD": "1"}
    purchase = make_purchase(shopper, store)
    first = abuse.evaluate(purchase)
    assert abuse.evaluate(purchase) == first


@pytest.mark.django_db
def test_paid_purchases_are_never_clawed_back(
    shopper: Player, store: Store, settings
) -> None:
    """Points are settled once. A hold firing after payment would reverse a
    reward the player has already been told they earned."""
    purchase = make_purchase(shopper, store)
    make_line(purchase)
    points.award_for_purchase(purchase)
    before = PointTransaction.objects.get(reason="purchase").amount

    settings.SPENDIUM = {**settings.SPENDIUM, "HIGH_VALUE_HOLD": "1"}
    abuse.evaluate(purchase)

    assert PointTransaction.objects.get(reason="purchase").amount == before


@pytest.mark.django_db
def test_the_review_queue_is_oldest_first(
    shopper: Player, store: Store, settings
) -> None:
    """A held payout is a player waiting."""
    settings.SPENDIUM = {**settings.SPENDIUM, "HIGH_VALUE_HOLD": "1"}
    first = make_purchase(shopper, store)
    second = make_purchase(shopper, store)
    abuse.evaluate(first)
    abuse.evaluate(second)
    Purchase.objects.filter(pk=first.pk).update(
        held_at=timezone.now() - timedelta(days=1)
    )
    assert list(abuse.held_purchases())[0].pk == first.pk
