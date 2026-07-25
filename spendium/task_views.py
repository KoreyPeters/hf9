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
