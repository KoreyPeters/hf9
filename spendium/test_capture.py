"""Receipt capture and history: upload, duplicates, deletion, export.

Deletion and export are tested against what the privacy policy actually
promises, not just against what the code happens to do — they are published
commitments, so a change that quietly narrows them should fail here.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from accounts.models import Membership, Player
from core.tasks import _registry
from spendium import service
from spendium.models import Product, ProductAlias, Purchase, PurchaseLineItem, Store
from spendium.test_extraction import (
    patterned_image,
    png_bytes,
    receipt_payload,
    upload_and_process,
)


def image_bytes_from(image) -> bytes:
    from io import BytesIO

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def shopper(db: None) -> Player:
    return Player.objects.create_user(username="shopper", email="shopper@example.com")


@pytest.fixture
def member(shopper: Player) -> Player:
    Membership.objects.create(
        player=shopper, expires_at=timezone.now() + timedelta(days=365)
    )
    return shopper


@pytest.fixture(autouse=True)
def isolated_media(settings, tmp_path) -> None:
    settings.MEDIA_ROOT = tmp_path


# ── Upload validation ─────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_upload_creates_a_pending_purchase(shopper: Player, fake_model) -> None:
    """The purchase exists before the receipt has been read."""
    fake_model(receipt_payload())
    purchase = service.accept_upload(shopper, png_bytes(), content_type="image/png")
    assert purchase.player == shopper
    assert purchase.image_phash


@pytest.mark.django_db
def test_oversized_upload_is_refused(shopper: Player, settings) -> None:
    settings.SPENDIUM = {**settings.SPENDIUM, "MAX_UPLOAD_BYTES": 10}
    with pytest.raises(service.UnsupportedImageError):
        service.accept_upload(shopper, png_bytes(), content_type="image/png")


@pytest.mark.django_db
def test_non_image_content_type_is_refused(shopper: Player) -> None:
    with pytest.raises(service.UnsupportedImageError):
        service.accept_upload(shopper, b"%PDF-1.4", content_type="application/pdf")


@pytest.mark.django_db
def test_undecodable_image_is_refused(shopper: Player) -> None:
    """A correct content type does not make the bytes an image."""
    with pytest.raises(service.UnsupportedImageError):
        service.accept_upload(shopper, b"not an image", content_type="image/png")


# ── A queue that will not take the task ───────────────────────────────────────
#
# Drawn from a real incident: the runtime service account could enqueue but
# could not act as the account the task's OIDC token was minted for, so every
# upload raised after the receipt had already been stored.


def _age_past_sweep_grace(purchase: Purchase) -> None:
    """Make a purchase old enough for the sweep to consider it.

    `pending_purchase_ids` ignores anything younger than SWEEP_GRACE_MINUTES, so
    a test that uploads and immediately sweeps is testing a case the sweep is
    built to skip.
    """
    grace = settings.SPENDIUM["SWEEP_GRACE_MINUTES"]
    Purchase.objects.filter(pk=purchase.pk).update(
        created_at=timezone.now() - timedelta(minutes=grace + 1)
    )


@pytest.fixture
def broken_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise PermissionError("the queue would not take it")

    monkeypatch.setattr(service, "enqueue", refuse)


@pytest.mark.django_db
def test_an_upload_survives_a_queue_that_will_not_take_the_task(
    shopper: Player, broken_queue: None
) -> None:
    """The receipt is stored and committed before the task is enqueued, so
    failing the request reports a loss that did not happen."""
    purchase = service.accept_upload(shopper, png_bytes(), content_type="image/png")

    assert purchase.pk is not None
    assert purchase.processing_status == Purchase.STATUS_PENDING


@pytest.mark.django_db
def test_the_sweep_reads_a_receipt_whose_task_was_never_queued(
    shopper: Player, fake_model, broken_queue: None
) -> None:
    """What makes absorbing the failure safe rather than merely quieter."""
    fake_model(receipt_payload())
    purchase = service.accept_upload(shopper, png_bytes(), content_type="image/png")

    # Past the sweep's grace period. A receipt this fresh is deliberately left to
    # its own task; the sweep only takes over once that task has plainly not
    # arrived, which is the case being tested here.
    _age_past_sweep_grace(purchase)
    _registry["sweep-pending-receipts"]()

    purchase.refresh_from_db()
    assert purchase.processing_status == Purchase.STATUS_PROCESSED


@pytest.mark.django_db
def test_a_refused_task_is_still_reported(
    shopper: Player, broken_queue: None, caplog
) -> None:
    """The 500 this replaces was also the alert. A queue refusing every task
    must not be something we learn about from players."""
    with caplog.at_level("ERROR", logger="spendium.service"):
        service.accept_upload(shopper, png_bytes(), content_type="image/png")

    assert [r for r in caplog.records if r.levelname == "ERROR"]
    assert "process-receipt" in caplog.text


@pytest.mark.django_db
def test_the_upload_page_does_not_report_a_failure(
    client, shopper: Player, broken_queue: None
) -> None:
    """What the player actually sees. The old 500 said the receipt was lost, and
    their obvious next move — upload it again — was then refused as a duplicate
    of the row they had just been told did not exist."""
    Membership.objects.create(
        player=shopper, expires_at=timezone.now() + timedelta(days=365)
    )
    client.force_login(shopper)

    response = client.post(
        reverse("spendium:receipt_upload"),
        {"receipt": SimpleUploadedFile("r.png", png_bytes(), content_type="image/png")},
    )

    assert response.status_code == 302
    assert Purchase.objects.filter(player=shopper).count() == 1


# ── Duplicate detection ───────────────────────────────────────────────────────


@pytest.mark.django_db
def test_the_same_receipt_twice_is_refused(shopper: Player, fake_model) -> None:
    """Re-uploading should not cost a second extraction."""
    fake_model(receipt_payload())
    image = image_bytes_from(patterned_image())
    service.accept_upload(shopper, image, content_type="image/png")
    with pytest.raises(service.DuplicateReceiptError):
        service.accept_upload(shopper, image, content_type="image/png")


@pytest.mark.django_db
def test_a_receipt_that_failed_can_be_uploaded_again(
    shopper: Player, fake_model
) -> None:
    """Nothing was extracted from a failed purchase, so there is nothing to
    double-count — and blocking it strands the player for the full 90-day
    lookback over a receipt we never managed to read."""
    fake_model(receipt_payload())
    image = image_bytes_from(patterned_image())
    purchase = service.accept_upload(shopper, image, content_type="image/png")
    Purchase.objects.filter(pk=purchase.pk).update(
        processing_status=Purchase.STATUS_FAILED
    )

    again = service.accept_upload(shopper, image, content_type="image/png")
    assert again.pk != purchase.pk


@pytest.mark.django_db
def test_a_rescaled_photo_of_one_receipt_is_still_a_duplicate(
    shopper: Player, fake_model
) -> None:
    """Two photos of one receipt never produce identical bytes.

    Exact-hash matching would never fire on the behaviour this exists to catch.
    """
    fake_model(receipt_payload())
    original = patterned_image()
    service.accept_upload(shopper, image_bytes_from(original), content_type="image/png")
    with pytest.raises(service.DuplicateReceiptError):
        service.accept_upload(
            shopper,
            image_bytes_from(original.resize((160, 160))),
            content_type="image/png",
        )


@pytest.mark.django_db
def test_a_different_receipt_is_accepted(shopper: Player, fake_model) -> None:
    fake_model(receipt_payload())
    service.accept_upload(
        shopper, image_bytes_from(patterned_image(seed=0)), content_type="image/png"
    )
    service.accept_upload(
        shopper, image_bytes_from(patterned_image(seed=5)), content_type="image/png"
    )
    assert Purchase.objects.count() == 2


@pytest.mark.django_db
def test_duplicate_check_is_scoped_to_the_uploading_player(
    shopper: Player, fake_model
) -> None:
    """Two people can legitimately buy the same things at the same shop."""
    fake_model(receipt_payload())
    other = Player.objects.create_user(username="other", email="o@example.com")
    image = image_bytes_from(patterned_image())
    service.accept_upload(shopper, image, content_type="image/png")
    service.accept_upload(other, image, content_type="image/png")
    assert Purchase.objects.count() == 2


# ── Asynchronous processing ───────────────────────────────────────────────────


@pytest.mark.django_db
def test_processing_populates_the_purchase(shopper: Player, fake_model) -> None:
    purchase = upload_and_process(shopper, fake_model(receipt_payload()))
    assert purchase.processing_status == Purchase.STATUS_PROCESSED
    assert purchase.total == Decimal("9.58")
    assert purchase.store is not None


@pytest.mark.django_db
def test_processing_records_arithmetic_problems(shopper: Player, fake_model) -> None:
    """Surfaced to the player, who is the only one who can judge them."""
    purchase = upload_and_process(shopper, fake_model(receipt_payload(subtotal=99.99)))
    assert purchase.processing_status == Purchase.STATUS_PROCESSED
    assert purchase.processing_problems


@pytest.mark.django_db
def test_unreadable_receipt_is_marked_failed(shopper: Player, fake_model) -> None:
    """The player is owed an answer even when the answer is 'we could not'."""
    purchase = upload_and_process(shopper, fake_model("not json at all"))
    assert purchase.processing_status == Purchase.STATUS_FAILED
    assert purchase.processing_problems


@pytest.mark.django_db
def test_a_failed_receipt_still_has_its_image_deleted(
    shopper: Player, fake_model
) -> None:
    """The 24-hour promise does not depend on the reading succeeding."""
    purchase = upload_and_process(shopper, fake_model("not json at all"))
    assert not purchase.receipt_image
    assert purchase.image_deleted_at is not None


@pytest.mark.django_db
def test_processing_is_not_repeated(shopper: Player, fake_model) -> None:
    """A redelivered task must not double the line items."""
    purchase = upload_and_process(shopper, fake_model(receipt_payload()))
    assert service.process_receipt(purchase.pk) is None
    assert purchase.line_items.count() == 2


# ── Purchase list ─────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_list_shows_only_your_own_receipts(client, shopper: Player) -> None:
    other = Player.objects.create_user(username="other", email="o@example.com")
    Purchase.objects.create(
        player=other, purchased_at=timezone.now(), total=Decimal("1"), image_phash="a"
    )
    mine = Purchase.objects.create(
        player=shopper,
        store=Store.objects.create(name="My Shop"),
        purchased_at=timezone.now(),
        total=Decimal("2"),
    )
    client.force_login(shopper)
    response = client.get(reverse("spendium:purchase_list"))
    assert response.status_code == 200
    assert b"My Shop" in response.content
    assert Purchase.objects.filter(pk=mine.pk).exists()


@pytest.mark.django_db
def test_list_requires_login(client) -> None:
    assert client.get(reverse("spendium:purchase_list")).status_code == 302


# ── The members-only gate ─────────────────────────────────────────────────────


@pytest.mark.django_db
def test_a_new_player_may_upload_without_membership(client, shopper: Player) -> None:
    """The free trial. The wall used to be here, before anyone had a reason to
    care about membership."""
    client.force_login(shopper)
    assert client.get(reverse("spendium:receipt_upload")).status_code == 200


@pytest.mark.django_db
def test_upload_is_refused_once_the_trial_is_used_up(
    client, shopper: Player, settings
) -> None:
    settings.SPENDIUM = settings.SPENDIUM | {"FREE_TRIAL_UPLOADS": 2}
    for _ in range(2):
        Purchase.objects.create(
            player=shopper, purchased_at=timezone.now(), total=Decimal("1")
        )
    client.force_login(shopper)
    assert client.get(reverse("spendium:receipt_upload")).status_code == 403


@pytest.mark.django_db
def test_a_receipt_we_could_not_read_does_not_cost_a_trial_upload(
    shopper: Player, settings
) -> None:
    """Our bug, not their allowance."""
    settings.SPENDIUM = settings.SPENDIUM | {"FREE_TRIAL_UPLOADS": 2}
    Purchase.objects.create(
        player=shopper,
        purchased_at=timezone.now(),
        total=Decimal("1"),
        processing_status=Purchase.STATUS_FAILED,
    )
    assert service.trial_uploads_left(shopper) == 2


@pytest.mark.django_db
def test_upload_page_is_open_to_members(client, member: Player) -> None:
    client.force_login(member)
    assert client.get(reverse("spendium:receipt_upload")).status_code == 200


@pytest.mark.django_db
def test_an_expired_membership_does_not_count(
    client, shopper: Player, settings
) -> None:
    """With the trial spent, an expired membership is no membership at all."""
    settings.SPENDIUM = settings.SPENDIUM | {"FREE_TRIAL_UPLOADS": 0}
    Membership.objects.create(
        player=shopper, expires_at=timezone.now() - timedelta(days=1)
    )
    client.force_login(shopper)
    assert client.get(reverse("spendium:receipt_upload")).status_code == 403


@pytest.mark.django_db
def test_a_member_keeps_uploading_past_the_trial(
    client, member: Player, settings
) -> None:
    settings.SPENDIUM = settings.SPENDIUM | {"FREE_TRIAL_UPLOADS": 0}
    client.force_login(member)
    assert client.get(reverse("spendium:receipt_upload")).status_code == 200


@pytest.mark.django_db
def test_uploading_through_the_view(client, member: Player, fake_model) -> None:
    from django.core.files.uploadedfile import SimpleUploadedFile

    fake_model(receipt_payload())
    client.force_login(member)
    response = client.post(
        reverse("spendium:receipt_upload"),
        {"receipt": SimpleUploadedFile("r.png", png_bytes(), content_type="image/png")},
    )
    assert response.status_code == 302
    assert Purchase.objects.filter(player=member).exists()


# ── Deletion, per the published policy ────────────────────────────────────────


@pytest.mark.django_db
def test_deleting_a_purchase_removes_it(shopper: Player, fake_model) -> None:
    purchase = upload_and_process(shopper, fake_model(receipt_payload()))
    service.delete_purchase(purchase)
    assert Purchase.objects.count() == 0
    assert PurchaseLineItem.objects.count() == 0


@pytest.mark.django_db
def test_deleting_keeps_aliases_the_player_confirmed(
    shopper: Player, fake_model
) -> None:
    """Aliases say what a receipt string means, not who bought what."""
    purchase = upload_and_process(shopper, fake_model(receipt_payload()))
    product = Product.objects.create(canonical_name="Anything")
    ProductAlias.objects.create(
        product=product, store=purchase.store, raw_text="TP-COLG-250"
    )
    service.delete_purchase(purchase)
    assert ProductAlias.objects.count() == 1


@pytest.mark.django_db
def test_deleting_history_removes_every_purchase(shopper: Player, fake_model) -> None:
    fake_model(receipt_payload())
    for seed in (0, 5):
        service.accept_upload(
            shopper,
            image_bytes_from(patterned_image(seed=seed)),
            content_type="image/png",
        )
    assert service.delete_purchase_history(shopper) == 2
    assert Purchase.objects.count() == 0


@pytest.mark.django_db
def test_delete_view_only_touches_your_own(client, shopper: Player) -> None:
    other = Player.objects.create_user(username="other", email="o@example.com")
    theirs = Purchase.objects.create(
        player=other, purchased_at=timezone.now(), total=Decimal("1")
    )
    client.force_login(shopper)
    response = client.post(reverse("spendium:purchase_delete", args=[theirs.pk]))
    assert response.status_code == 404
    assert Purchase.objects.filter(pk=theirs.pk).exists()


# ── Export, per the access right ──────────────────────────────────────────────


@pytest.mark.django_db
def test_export_includes_the_raw_receipt_text(shopper: Player, fake_model) -> None:
    """The raw string is what the system acts on; hiding it would be incomplete."""
    upload_and_process(shopper, fake_model(receipt_payload()))
    export = service.export_purchase_history(shopper)
    assert len(export) == 1
    texts = [line["receipt_text"] for line in export[0]["line_items"]]
    assert "TP-COLG-250" in texts


@pytest.mark.django_db
def test_export_view_returns_a_download(client, shopper: Player, fake_model) -> None:
    upload_and_process(shopper, fake_model(receipt_payload()))
    client.force_login(shopper)
    response = client.get(reverse("spendium:purchase_history_export"))
    assert response.status_code == 200
    assert "attachment" in response["Content-Disposition"]


@pytest.mark.django_db
def test_export_covers_only_your_own_history(client, shopper: Player) -> None:
    other = Player.objects.create_user(username="other", email="o@example.com")
    Purchase.objects.create(
        player=other, purchased_at=timezone.now(), total=Decimal("99")
    )
    assert service.export_purchase_history(shopper) == []
