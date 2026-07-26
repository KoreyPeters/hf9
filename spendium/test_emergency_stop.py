"""The emergency stop.

The test throughout is somebody's first day: they have been told the bill is
running away and they are the only person available. So the things worth
asserting are that it stops *everything*, that nothing is lost while it is on,
and that resuming needs no further intervention.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from accounts.models import Membership, Player
from core.tasks import _registry
from spendium import service, spending
from spendium.models import EmergencyStop, Product, Purchase
from spendium.test_extraction import png_bytes, receipt_payload


@pytest.fixture
def shopper(db: None) -> Player:
    return Player.objects.create_user(username="shopper", email="s@example.com")


@pytest.fixture
def admin_user(db: None) -> Player:
    return Player.objects.create_superuser(
        username="boss", email="boss@example.com", password="x"
    )


@pytest.fixture(autouse=True)
def isolated_media(settings, tmp_path) -> None:
    settings.MEDIA_ROOT = tmp_path


def stop(hours_ago: int = 0) -> EmergencyStop:
    state = EmergencyStop.get()
    state.is_stopped = True
    state.save()
    if hours_ago:
        EmergencyStop.objects.filter(pk=state.pk).update(
            stopped_at=timezone.now() - timedelta(hours=hours_ago)
        )
    return EmergencyStop.get()


# ── It stops everything ───────────────────────────────────────────────────────


@pytest.mark.django_db
def test_off_by_default() -> None:
    assert spending.is_stopped() is False


@pytest.mark.django_db
def test_the_guard_refuses_while_stopped() -> None:
    """A raise rather than a silent no-op: code that asked for a client and got
    None would fail further along, complaining about the wrong thing."""
    stop()
    with pytest.raises(spending.SpendingStoppedError):
        spending.guard()


@pytest.mark.django_db
def test_both_client_constructors_are_guarded() -> None:
    """The guard sits where the client is built — the only place money is
    actually committed, and a choke point both callers pass through.

    Read from the source file rather than the imported module, because the
    autouse fixture that keeps tests off the network replaces `_client` itself —
    inspecting the attribute would examine the fixture. The end-to-end test
    below is the real proof that nothing is spent; this one guards against a
    third caller being added later without the guard.
    """
    import ast
    import pathlib

    for name in ("extraction", "adjudication"):
        source = pathlib.Path(f"spendium/{name}.py").read_text(encoding="utf-8")
        fn = next(
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef) and node.name == "_client"
        )
        assert "spending.guard()" in ast.unparse(fn), f"{name}._client is unguarded"


@pytest.mark.django_db
def test_no_model_is_called_on_upload_while_stopped(
    shopper: Player, fake_model
) -> None:
    """The end-to-end claim: pulling it actually stops the meter."""
    client = fake_model(receipt_payload())
    stop()
    service.accept_upload(shopper, png_bytes(), content_type="image/png")
    assert client.models.calls == []


# ── Nothing is lost ───────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_uploads_are_still_accepted_while_stopped(shopper: Player) -> None:
    stop()
    purchase = service.accept_upload(shopper, png_bytes(), content_type="image/png")
    assert Purchase.objects.filter(pk=purchase.pk).exists()


@pytest.mark.django_db
def test_a_stopped_receipt_stays_pending_not_failed(
    shopper: Player, fake_model
) -> None:
    """The distinction that makes a short stop invisible to players.

    A failed receipt costs the player an upload; a pending one just waits and is
    read when the stop clears. Only holds while the image survives — see the
    long-outage section below.
    """
    fake_model(receipt_payload())
    stop()
    purchase = service.accept_upload(shopper, png_bytes(), content_type="image/png")
    purchase.refresh_from_db()
    assert purchase.processing_status == Purchase.STATUS_PENDING
    assert purchase.processing_problems == []


@pytest.mark.django_db
def test_no_points_are_wrongly_awarded_while_stopped(
    shopper: Player, fake_model
) -> None:
    fake_model(receipt_payload())
    stop()
    purchase = service.accept_upload(shopper, png_bytes(), content_type="image/png")
    purchase.refresh_from_db()
    assert purchase.points_awarded is None


# ── Resuming needs no further intervention ────────────────────────────────────


@pytest.mark.django_db
def test_waiting_receipts_are_read_once_it_is_switched_off(
    shopper: Player, fake_model
) -> None:
    fake_model(receipt_payload())
    stop()
    purchase = service.accept_upload(shopper, png_bytes(), content_type="image/png")

    state = EmergencyStop.get()
    state.is_stopped = False
    state.save()
    _registry["sweep-pending-receipts"]()

    purchase.refresh_from_db()
    assert purchase.processing_status == Purchase.STATUS_PROCESSED
    assert purchase.total == Decimal("9.58")


@pytest.mark.django_db
def test_the_sweeper_does_nothing_while_still_stopped(
    shopper: Player, fake_model
) -> None:
    """So it can stay on its normal schedule throughout an incident."""
    client = fake_model(receipt_payload())
    stop()
    service.accept_upload(shopper, png_bytes(), content_type="image/png")

    _registry["sweep-pending-receipts"]()
    assert client.models.calls == []


@pytest.mark.django_db
def test_the_sweeper_also_catches_a_dropped_task(shopper: Player, fake_model) -> None:
    """Independently useful: before this, a lost task meant a receipt sat
    pending forever with nothing watching."""
    fake_model(receipt_payload())
    purchase = Purchase.objects.create(
        player=shopper,
        purchased_at=timezone.now(),
        total=Decimal("0"),
        processing_status=Purchase.STATUS_PENDING,
    )
    assert purchase.pk in service.pending_purchase_ids()


# ── A stop that outlives the image retention window ───────────────────────────
#
# Queueing hides a short stop from players entirely. It cannot hide a long one:
# images are deleted 24 hours after upload regardless, because that commitment is
# published and an outage is the worst possible reason to hold player photos
# longer. So past that point the honest move is to stop taking receipts.


@pytest.mark.django_db
def test_images_are_still_deleted_on_time_during_a_stop(shopper: Player) -> None:
    """The promise is 24 hours from upload, with no exception for our outages."""
    stop()
    purchase = service.accept_upload(shopper, png_bytes(), content_type="image/png")
    Purchase.objects.filter(pk=purchase.pk).update(
        created_at=timezone.now() - timedelta(hours=25)
    )

    _registry["sweep-receipt-images"]()

    purchase.refresh_from_db()
    assert purchase.image_deleted_at is not None
    assert not purchase.receipt_image


@pytest.mark.django_db
def test_a_short_stop_still_accepts_uploads(shopper: Player) -> None:
    """The common case is a blip. Those receipts are read when it clears, so
    refusing them would cost players something for nothing."""
    stop(hours_ago=2)
    assert spending.uploads_paused() is False
    service.accept_upload(shopper, png_bytes(), content_type="image/png")


@pytest.mark.django_db
def test_a_long_stop_refuses_uploads(shopper: Player) -> None:
    """Refused at the door, before anything is stored. Accepting a receipt we
    can already tell we will delete unread costs the player their photo too."""
    stop(hours_ago=30)
    assert spending.uploads_paused() is True
    with pytest.raises(service.UploadsPausedError):
        service.accept_upload(shopper, png_bytes(), content_type="image/png")
    assert Purchase.objects.count() == 0


@pytest.mark.django_db
def test_the_refusal_tells_the_player_to_keep_the_receipt(
    client, shopper: Player
) -> None:
    """The whole value of refusing is that they still have it afterwards."""
    Membership.objects.create(
        player=shopper, expires_at=timezone.now() + timedelta(days=365)
    )
    stop(hours_ago=30)
    client.force_login(shopper)
    response = client.post(
        reverse("spendium:receipt_upload"),
        {"receipt": SimpleUploadedFile("r.png", png_bytes(), "image/png")},
    )
    assert b"upload it again later" in response.content


@pytest.mark.django_db
def test_uploads_resume_the_moment_it_is_switched_off(shopper: Player) -> None:
    """No separate all-clear to remember during an incident."""
    stop(hours_ago=30)
    state = EmergencyStop.get()
    state.is_stopped = False
    state.save()

    assert spending.uploads_paused() is False
    service.accept_upload(shopper, png_bytes(), content_type="image/png")


@pytest.mark.django_db
def test_a_receipt_stranded_by_a_long_stop_can_be_uploaded_again(
    shopper: Player, fake_model
) -> None:
    """The residual case: uploaded in the first 24 hours of a stop that then ran
    for days. Its image is gone, so it fails — but the player is not stuck with
    it, which is what makes that failure recoverable."""
    fake_model(receipt_payload())
    stop()
    image = png_bytes()
    purchase = service.accept_upload(shopper, image, content_type="image/png")
    Purchase.objects.filter(pk=purchase.pk).update(
        created_at=timezone.now() - timedelta(hours=25)
    )
    _registry["sweep-receipt-images"]()

    state = EmergencyStop.get()
    state.is_stopped = False
    state.save()
    _registry["sweep-pending-receipts"]()

    purchase.refresh_from_db()
    assert purchase.processing_status == Purchase.STATUS_FAILED
    assert "upload it again" in purchase.processing_problems[0]

    again = service.accept_upload(shopper, image, content_type="image/png")
    assert again.pk != purchase.pk


@pytest.mark.django_db
def test_the_clock_starts_however_it_was_switched(shopper: Player) -> None:
    """Stopped from a shell or a migration, not the admin. The timestamp decides
    whether players can still upload, so it cannot depend on the route taken."""
    state = EmergencyStop.get()
    state.is_stopped = True
    state.save()
    assert state.stopped_at is not None


# ── Findable and accountable ──────────────────────────────────────────────────


@pytest.mark.django_db
def test_it_appears_on_the_admin_index(client, admin_user: Player) -> None:
    """Somebody looking for it should not have to know which model holds it."""
    client.force_login(admin_user)
    response = client.get(reverse("admin:app_list", args=["spendium"]))
    assert b"Emergency stop" in response.content


@pytest.mark.django_db
def test_the_changelist_is_never_empty(client, admin_user: Player) -> None:
    """It creates its own row, so the page is never a blank list to somebody
    arriving in a hurry."""
    EmergencyStop.objects.all().delete()
    client.force_login(admin_user)
    response = client.get(reverse("admin:spendium_emergencystop_changelist"))
    assert response.status_code == 200
    assert EmergencyStop.objects.exists()


@pytest.mark.django_db
def test_it_records_who_stopped_it(client, admin_user: Player) -> None:
    """The person pulling this has enough to think about; whoever asks
    afterwards will still want to know."""
    client.force_login(admin_user)
    client.post(
        reverse("admin:spendium_emergencystop_change", args=[EmergencyStop.get().pk]),
        {"is_stopped": "on", "note": "bill spiking"},
    )
    state = EmergencyStop.get()
    assert state.is_stopped is True
    assert state.stopped_by == admin_user
    assert state.stopped_at is not None
    assert state.note == "bill spiking"


@pytest.mark.django_db
def test_switching_it_off_clears_the_attribution(client, admin_user: Player) -> None:
    client.force_login(admin_user)
    url = reverse("admin:spendium_emergencystop_change", args=[EmergencyStop.get().pk])
    client.post(url, {"is_stopped": "on"})
    client.post(url, {})
    state = EmergencyStop.get()
    assert state.is_stopped is False
    assert state.stopped_at is None


@pytest.mark.django_db
def test_it_cannot_be_deleted(client, admin_user: Player) -> None:
    """There is exactly one, and it must be there when somebody needs it."""
    from spendium.admin import EmergencyStopAdmin

    assert EmergencyStopAdmin(EmergencyStop, None).has_delete_permission(None) is False


@pytest.mark.django_db
def test_the_narrow_control_still_works_independently() -> None:
    """Tier 2 alone can still be disabled without stopping everything."""
    from spendium.models import MatchConfig

    config = MatchConfig.get()
    config.adjudication_candidates = 0
    config.save()
    assert spending.is_stopped() is False
    assert Product.objects.count() == 0  # nothing else disturbed
