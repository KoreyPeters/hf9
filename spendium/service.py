"""Purchase lifecycle: recording, extraction, anonymisation, image expiry."""

from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.files.base import ContentFile
from django.db.transaction import atomic
from django.utils import timezone

from core.tasks import enqueue
from points.models import PointTransaction

from . import extraction, imaging, matching
from .models import (
    AnonymisedLineItem,
    AnonymisedPurchase,
    Purchase,
    PurchaseLineItem,
    Store,
)


def _resolve_store(store_name: str) -> Store | None:
    """Find or create the retailer named on the receipt.

    Matched case-insensitively on the printed name. This is deliberately naive —
    proper store deduplication is community lifecycle work, and a slightly
    fragmented store list costs far less than wrongly merging two chains, which
    would pool their aliases and corrupt retailer-scoped matching.
    """
    name = (store_name or "").strip()
    if not name:
        return None
    existing = Store.objects.filter(name__iexact=name).first()
    return existing or Store.objects.create(name=name)


@atomic
def record_receipt(
    player: Any,
    image_bytes: bytes,
    filename: str = "receipt.jpg",
    mime_type: str = "image/jpeg",
    client: Any | None = None,
) -> Purchase:
    """Extract a receipt, match its line items, and persist the purchase.

    The image is stored so the extraction step has something to read, hashed for
    duplicate detection, and then queued for deletion straight away. The hash is
    taken before anything else, because it is the only part of the image that
    survives and losing it would disable the duplicate check permanently.
    """
    phash = imaging.perceptual_hash(image_bytes)
    receipt = extraction.extract_receipt(image_bytes, mime_type, client=client)

    store = _resolve_store(receipt.store_name)
    purchase = Purchase(
        player=player,
        store=store,
        purchased_at=receipt.transaction_datetime or timezone.now(),
        subtotal=receipt.subtotal,
        tax=receipt.tax,
        total=receipt.total,
        image_phash=phash,
    )
    purchase.receipt_image.save(filename, ContentFile(image_bytes), save=False)
    purchase.save()

    results = matching.match_line_items(
        [(item.raw_text, item.interpreted_name) for item in receipt.line_items],
        store=store,
    )
    PurchaseLineItem.objects.bulk_create(
        [
            PurchaseLineItem(
                purchase=purchase,
                raw_text=item.raw_text,
                raw_text_normalised="",  # set below; bulk_create skips save()
                interpreted_name=item.interpreted_name,
                product=result.product,
                match_tier=result.tier,
                match_confidence=result.confidence,
                quantity=item.quantity,
                unit_price=item.unit_price,
                line_total=item.line_total,
                disambiguation_state=(
                    PurchaseLineItem.STATE_PENDING
                    if result.needs_prompt
                    else PurchaseLineItem.STATE_NOT_NEEDED
                ),
            )
            for item, result in zip(receipt.line_items, results, strict=True)
        ]
    )
    # bulk_create bypasses save(), so normalisation has to be applied after.
    for line in purchase.line_items.all():
        line.save(update_fields=["raw_text_normalised"])

    enqueue("delete-receipt-image", {"purchase_id": purchase.pk})
    enqueue(
        "anonymise-purchase",
        {"purchase_id": purchase.pk},
        schedule_time=purchase.anonymise_after,
    )
    return purchase


def negative_line_total_ids(purchase: Purchase) -> list[int]:
    """Line items that must not earn points.

    Returns and same-receipt refunds appear as negative totals. They are
    excluded from earning silently rather than subtracted, per the deliberate
    decision not to chase return fraud at this stage.
    """
    return list(
        purchase.line_items.filter(line_total__lt=Decimal("0")).values_list(
            "pk", flat=True
        )
    )


@atomic
def anonymise_purchase(purchase_id: int) -> bool:
    """Move a purchase from the player-linked layer to the anonymous one.

    Copies the purchase and its line items into `AnonymisedPurchase` /
    `AnonymisedLineItem` under a fresh token, severs any points-ledger
    reference, then deletes the original.

    Deleting rather than nulling the player FK is the whole point. The ledger
    holds a GenericForeignKey, so a surviving purchase row could be joined back
    to the player who earned points from it — the basket would still be
    identified in practice even with the FK cleared. Deletion removes the join
    target entirely.

    Idempotent: a purchase already anonymised (or never present) is a no-op, so
    a redelivered Cloud Task cannot duplicate the anonymous record.
    """
    purchase = (
        Purchase.objects.select_for_update()
        .prefetch_related("line_items")
        .filter(pk=purchase_id)
        .first()
    )
    if purchase is None:
        return False

    anonymised = AnonymisedPurchase.objects.create(
        store=purchase.store,
        purchased_at=purchase.purchased_at,
        total=purchase.total,
    )

    AnonymisedLineItem.objects.bulk_create(
        [
            AnonymisedLineItem(
                anonymised_purchase=anonymised,
                raw_text=item.raw_text,
                raw_text_normalised=item.raw_text_normalised,
                interpreted_name=item.interpreted_name,
                product=item.product,
                match_tier=item.match_tier,
                match_confidence=item.match_confidence,
                quantity=item.quantity,
                unit_price=item.unit_price,
                line_total=item.line_total,
            )
            for item in purchase.line_items.all()
        ]
    )

    # Sever the ledger reference. The transaction itself is permanent — it is
    # the player's game history — but it must not point at basket detail.
    # object_id is a plain integer with no FK constraint, so a dangling value
    # would survive the delete and still name the purchase.
    PointTransaction.objects.filter(
        content_type=ContentType.objects.get_for_model(Purchase),
        object_id=purchase.pk,
    ).update(content_type=None, object_id=None)

    purchase.delete()
    return True


def due_purchase_ids() -> list[int]:
    """Purchases past their retention window and still player-linked.

    The per-purchase task is scheduled at write time; this backs it up. Cloud
    Tasks can drop a scheduled task, and a purchase that silently outlives its
    window is a privacy failure, not a cosmetic one.
    """
    return list(
        Purchase.objects.filter(anonymise_after__lte=timezone.now()).values_list(
            "pk", flat=True
        )
    )


def expired_image_purchase_ids() -> list[int]:
    """Purchases whose receipt image is past the published deletion commitment."""
    hours: int = settings.SPENDIUM["IMAGE_RETENTION_HOURS"]
    cutoff = timezone.now() - timedelta(hours=hours)
    return list(
        Purchase.objects.filter(
            created_at__lte=cutoff, image_deleted_at__isnull=True
        ).values_list("pk", flat=True)
    )


def delete_receipt_image(purchase_id: int) -> bool:
    """Delete the stored image and stamp when it happened.

    Called immediately after successful processing, not at the 24-hour mark.
    The published commitment is an outer bound, not a target — once the text is
    extracted the image has no further use, and the cheapest way to honour a
    deletion promise is to have nothing left to delete.

    Idempotent, and stamps `image_deleted_at` even when there was no file, so a
    purchase whose upload failed does not sit in the sweeper's queue forever.
    """
    purchase = Purchase.objects.filter(pk=purchase_id).first()
    if purchase is None:
        return False
    if purchase.image_deleted_at is not None:
        return False

    if purchase.receipt_image:
        purchase.receipt_image.delete(save=False)

    purchase.receipt_image = None
    purchase.image_deleted_at = timezone.now()
    purchase.save(update_fields=["receipt_image", "image_deleted_at"])
    return True
