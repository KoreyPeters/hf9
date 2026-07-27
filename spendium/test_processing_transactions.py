"""Where the transactions start and stop while a receipt is read.

`process_receipt` used to run under a single `@atomic`, so SQLite's one write
lock was held across two Gemini round trips. Everything else that wanted to
write — down to a session row on the page the player was refreshing — waited it
out and failed with "database is locked".

These pin the split that fixed it. The first is the one that matters: it fails
the moment anyone puts a model call back inside a transaction, however
reasonable the reason looks at the time.
"""

import pytest
from django.db import connection

from accounts.models import Player
from spendium import adjudication, extraction, service
from spendium.conftest import FakeClient
from spendium.models import Purchase
from spendium.test_extraction import png_bytes, receipt_payload


@pytest.fixture
def shopper(db: None) -> Player:
    return Player.objects.create_user(username="shopper", email="shopper@example.com")


@pytest.fixture(autouse=True)
def isolated_media(settings, tmp_path) -> None:
    settings.MEDIA_ROOT = tmp_path


@pytest.mark.django_db(transaction=True)
def test_the_model_is_never_called_while_a_transaction_is_open(
    shopper: Player, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression guard.

    Runs with a real transaction rather than the usual wrapped one, because
    pytest-django normally holds every test inside an atomic block — under which
    `in_atomic_block` is always true and this would assert nothing.
    """
    seen: list[bool] = []
    client = FakeClient(receipt_payload())
    underlying = client.models.generate_content

    def watched(**kwargs):
        seen.append(connection.in_atomic_block)
        return underlying(**kwargs)

    client.models.generate_content = watched
    monkeypatch.setattr(extraction, "_client", lambda: client)
    monkeypatch.setattr(adjudication, "_client", lambda: client)

    service.accept_upload(shopper, png_bytes(), content_type="image/png")

    assert seen, "no model call was made, so this test asserted nothing"
    assert not any(seen), (
        "a Gemini call happened inside an open transaction — that holds SQLite's "
        "only write lock for the length of a network round trip, which is exactly "
        "what produced 'database is locked' in production"
    )


@pytest.mark.django_db
def test_a_run_that_dies_before_settling_does_not_double_the_receipt(
    shopper: Player, fake_model, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 2 commits line items on its own now, so a later failure leaves them
    behind with the purchase still pending — and the sweep will try again.
    Appending rather than recreating would give the player two of every line."""
    fake_model(receipt_payload())

    attempts = {"n": 0}
    settle = service._settle

    def flaky(purchase, decisions):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("settlement failed")
        return settle(purchase, decisions)

    monkeypatch.setattr(service, "_settle", flaky)

    # The failure is swallowed by `_enqueue_or_sweep`, which is deliberate: an
    # upload that cannot be processed right now is still an upload we accepted.
    purchase = service.accept_upload(shopper, png_bytes(), content_type="image/png")

    purchase.refresh_from_db()
    assert purchase.processing_status == Purchase.STATUS_PENDING
    assert purchase.line_items.count() == 2

    service.process_receipt(purchase.pk)

    purchase.refresh_from_db()
    assert purchase.processing_status == Purchase.STATUS_PROCESSED
    assert purchase.line_items.count() == 2, "the retry duplicated the receipt"


@pytest.mark.django_db
def test_the_payout_happens_after_adjudication(
    shopper: Player, fake_model, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ordering constraint, not a preference. Points are summed over the
    products on the line items, and adjudication is what assigns products to the
    lines matching could not place — so settling first would underpay the player
    for precisely the items the model resolved.

    This is why the obvious fix for the lock, moving the model call to the end,
    was the wrong one.
    """
    fake_model(receipt_payload())

    order: list[str] = []
    apply_adjudication = service._apply_adjudication
    award = service.points.award_for_purchase

    def watched_apply(purchase, decisions):
        order.append("adjudicate")
        return apply_adjudication(purchase, decisions)

    def watched_award(purchase):
        order.append("award")
        return award(purchase)

    monkeypatch.setattr(service, "_apply_adjudication", watched_apply)
    monkeypatch.setattr(service.points, "award_for_purchase", watched_award)

    service.accept_upload(shopper, png_bytes(), content_type="image/png")

    assert order == ["adjudicate", "award"]
