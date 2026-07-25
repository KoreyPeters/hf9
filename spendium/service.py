"""Purchase lifecycle: recording, anonymisation, image expiry."""

from datetime import timedelta

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db.transaction import atomic
from django.utils import timezone

from points.models import PointTransaction

from .models import (
    AnonymisedLineItem,
    AnonymisedPurchase,
    Purchase,
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
