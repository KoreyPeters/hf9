from core.tasks import task

from . import service


@task("anonymise-purchase")
def anonymise_purchase(purchase_id: int) -> None:
    """Scheduled at write time for each purchase, at the end of its window."""
    service.anonymise_purchase(purchase_id)


@task("sweep-purchase-anonymisation")
def sweep_purchase_anonymisation() -> None:
    """Safety net for purchases whose scheduled task never arrived.

    Runs daily. `anonymise_purchase` is idempotent, so overlapping with the
    per-purchase task is harmless.
    """
    for purchase_id in service.due_purchase_ids():
        service.anonymise_purchase(purchase_id)


@task("snapshot-product-ratings")
def snapshot_product_ratings() -> None:
    """Record today's rating for every product.

    Daily, because a rating computed over a rolling window cannot be
    reconstructed later — the responses behind it age out. A missed day is a
    gap in the trend line, not a correctness problem.
    """
    from . import ratings

    ratings.snapshot_all()


@task("retro-match")
def retro_match() -> None:
    """Re-run matching over recorded line items against the current catalogue.

    Scheduled rather than triggered: the catalogue improves continuously, and
    nothing about this is urgent enough to justify reacting to every change.
    """
    from . import retro

    retro.run()


@task("process-receipt")
def process_receipt(purchase_id: int) -> None:
    """Read an uploaded receipt. Enqueued the moment the upload is accepted."""
    service.process_receipt(purchase_id)


@task("delete-receipt-image")
def delete_receipt_image(purchase_id: int) -> None:
    """Enqueued as soon as extraction finishes."""
    service.delete_receipt_image(purchase_id)


@task("sweep-receipt-images")
def sweep_receipt_images() -> None:
    """Backstop for the published 24-hour image deletion commitment.

    Runs hourly rather than daily. The commitment is a hard 24 hours, so a
    daily sweep could leave an image up to 48 hours old if a deletion was
    missed just after a run.
    """
    for purchase_id in service.expired_image_purchase_ids():
        service.delete_receipt_image(purchase_id)
