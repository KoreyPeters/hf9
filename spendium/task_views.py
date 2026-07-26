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


@task("sweep-pending-receipts")
def sweep_pending_receipts() -> None:
    """Read receipts that are still waiting.

    Picks up whatever waited out an emergency stop, and whatever lost its
    original task. Does nothing while the stop is on, since process_receipt
    returns early — so this can run on its normal schedule throughout.
    """
    for purchase_id in service.pending_purchase_ids():
        service.process_receipt(purchase_id)


@task("snapshot-metrics")
def snapshot_metrics() -> None:
    """Record today's convergence numbers, platform-wide and per store.

    Daily. The claim these exist to test is that the system improves without
    curation, and a rate computed once says nothing about whether it is moving.
    """
    from . import metrics

    metrics.take_snapshot()


@task("recompute-hotness")
def recompute_hotness() -> None:
    """Refresh which products are worth interrupting players about.

    Nightly. Manual admin flags survive this, because the situations that need
    them — a recall, a safety event — are exactly the ones no purchase-volume
    metric will have noticed yet.
    """
    from . import action_centre

    action_centre.recompute_hotness()


@task("send-action-centre-emails")
def send_action_centre_emails() -> None:
    """At most one email per player per week, and only when warranted.

    Routine items never qualify. Emailing about housekeeping is how a mailing
    list teaches people to ignore it, and then the one that mattered goes
    unread too.
    """
    from django.core.mail import send_mail
    from django.urls import reverse

    from . import action_centre

    for player in action_centre.players_with_live_purchases():
        kind = action_centre.email_due(player)
        if kind is None or not player.email:
            continue
        summary = action_centre.rateable_summary(player)
        url = reverse("spendium:action_centre")
        link = f"https://humanflourish.ing{url}"
        if kind == "onboarding":
            subject = "Your Spendium action centre"
            body = (
                "Everything waiting for you lives in one place.\n\n"
                f"Right now: {summary['total']} item(s).\n\n"
                f"{link}\n"
            )
        else:
            subject = "Something changed on a product you bought"
            body = (
                f"{summary['hot']} product(s) you have bought recently need "
                f"a look.\n\n{link}\n"
            )
        # Recorded before sending, not after. Cloud Tasks retries up to five
        # times, and a crash between the two would re-email whoever was in
        # flight. Recording first means a failure costs that player their email
        # this week instead of sending it twice — the safer way round for mail
        # nobody asked for.
        action_centre.record_email_sent(player, kind)
        send_mail(subject, body, None, [player.email], fail_silently=True)


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
