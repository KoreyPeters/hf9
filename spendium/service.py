"""Purchase lifecycle: recording, extraction, anonymisation, image expiry."""

import logging
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.files.base import ContentFile
from django.db.transaction import atomic
from django.utils import timezone

from core.tasks import enqueue
from points.models import PointTransaction

from . import abuse, adjudication, extraction, imaging, matching, points, spending
from .models import (
    AnonymisedLineItem,
    AnonymisedPurchase,
    MatchConfig,
    MatchTier,
    ProductAlias,
    Purchase,
    PurchaseLineItem,
    Store,
)

logger = logging.getLogger(__name__)


def _enqueue_or_sweep(
    url_path: str,
    payload: dict[str, Any],
    schedule_time: datetime | None = None,
) -> None:
    """Enqueue follow-up work whose caller has already done the work that counts.

    Every call site here runs after the thing being protected is durable — the
    purchase committed and the image stored, or the receipt read and the points
    awarded. Raising at that point reports a loss that did not happen: the player
    gets a 500 for an upload that succeeded, and their obvious next move,
    uploading the photo again, is refused as a duplicate of the very row the
    error told them did not exist. Inside `process_receipt` it is worse, because
    that function is called in a loop by the sweep — one purchase whose task will
    not queue would stop every other pending receipt from being read, which is
    precisely the recovery path a queue outage depends on.

    What makes absorbing it safe is that all three tasks have a scheduled sweep
    behind them — `sweep-pending-receipts`, `sweep-purchase-anonymisation`,
    `sweep-receipt-images` — and every one of those calls its handler directly
    rather than re-enqueuing, so they still work when the queue does not. A
    dropped task costs a delay well inside the window it serves, not the work.

    Logged as an error because the 500 this replaces was also the alert: it fired
    the platform's 5xx policy and mailed a traceback. Absorbing the failure
    silently would buy the player's upload at the price of finding out from
    players that receipts have been slow for a week.
    """
    try:
        enqueue(url_path, payload, schedule_time=schedule_time)
    except Exception:
        logger.exception(
            "Could not enqueue '%s' for purchase %s. The receipt is stored and "
            "the scheduled sweep will pick it up.",
            url_path,
            payload.get("purchase_id"),
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


class UploadsPausedError(RuntimeError):
    """Receipt reading is stopped, and has been long enough that anything
    accepted now would be deleted before it could be read."""


class DuplicateReceiptError(RuntimeError):
    """This player has already uploaded a photo of this receipt."""


class UnsupportedImageError(RuntimeError):
    """The upload is not an image we can read."""


def find_duplicate(player: Any, phash: str) -> Purchase | None:
    """A recent purchase by this player that looks like the same receipt.

    Perceptual hashes are compared by Hamming distance, not equality. Two photos
    of one receipt never produce identical bytes — or identical hashes — so an
    exact-match check would never fire on the behaviour it exists to catch.

    Scoped to the uploading player. Two people can legitimately buy the same
    things at the same shop, and a global check would punish them for it.

    Failed purchases are excluded. Nothing was extracted from them, so there is
    nothing to double-count, and treating one as a duplicate locks the player
    out of re-submitting a receipt we never managed to read — for the full
    lookback window, which is far longer than the image survives.
    """
    threshold: int = settings.SPENDIUM["DUPLICATE_HASH_DISTANCE"]
    lookback: int = settings.SPENDIUM["DUPLICATE_LOOKBACK_DAYS"]
    cutoff = timezone.now() - timedelta(days=lookback)

    recent = (
        Purchase.objects.filter(player=player, created_at__gte=cutoff)
        .exclude(image_phash="")
        .exclude(processing_status=Purchase.STATUS_FAILED)
    )
    for candidate in recent:
        try:
            if imaging.hamming_distance(candidate.image_phash, phash) <= threshold:
                return candidate
        except ValueError:
            continue
    return None


def trial_uploads_used(player: object) -> int:
    """Uploads this player has spent against the free trial.

    Counts attempts rather than successes, with one exception: a receipt we
    failed to read is our bug, not their allowance, so it does not count. Every
    other outcome does — otherwise the number left is unpredictable, and a quota
    a player cannot reason about is worse than no quota.

    Derived rather than stored. A counter on `Player` would have to survive
    anonymisation, deletion and retries without drifting; this cannot drift, and
    the query is one indexed count per player.
    """
    return (
        Purchase.objects.filter(player=player)
        .exclude(processing_status=Purchase.STATUS_FAILED)
        .count()
    )


def trial_uploads_left(player: object) -> int:
    allowance: int = settings.SPENDIUM["FREE_TRIAL_UPLOADS"]
    return max(0, allowance - trial_uploads_used(player))


def is_member(player: object) -> bool:
    """Membership is what pays for receipt scanning past the free trial.

    Not a cost constraint — extraction is cheap per receipt. It is a deliberate
    product decision to make membership tangibly worth something. See
    `may_upload`, which is the gate callers actually want.
    """
    from django.core.exceptions import ObjectDoesNotExist

    try:
        membership = player.membership
    except ObjectDoesNotExist:
        return False
    return membership.is_active and membership.expires_at > timezone.now()


def may_upload(player: object) -> bool:
    """Members always; everyone else until the free trial runs out.

    The trial exists because the wall used to arrive before a player had any
    reason to care about membership — which reads as a bait and switch rather
    than as a thing worth paying for.

    Lives here rather than in `views` because it is upload policy, and it is now
    read from two places that are not each other: the view that serves the
    upload page, and the template tag deciding whether to offer the shortcut to
    it. A gate answered differently in those two places is a button that leads
    to a 403.
    """
    return is_member(player) or trial_uploads_left(player) > 0


def accept_upload(
    player: Any,
    image_bytes: bytes,
    filename: str = "receipt.jpg",
    content_type: str = "image/jpeg",
) -> Purchase:
    """Take an uploaded image and queue it for reading.

    Deliberately does no extraction. Reading a receipt costs one or two model
    calls, which is far too long to hold a request open, so this does only what
    must happen synchronously — validate, hash, store, reject duplicates — and
    hands the rest to a task.

    The hash is computed before anything is stored, because it is the only part
    of the image that outlives the 24-hour deletion window. Losing it would
    disable duplicate detection permanently.
    """
    if spending.uploads_paused():
        # Checked before anything is stored. Accepting a receipt we can already
        # tell we will delete unread would cost the player their photo as well
        # as their receipt.
        raise UploadsPausedError(
            "We are not able to read receipts at the moment. Please keep this "
            "one and upload it again later — nothing has been recorded."
        )

    max_bytes: int = settings.SPENDIUM["MAX_UPLOAD_BYTES"]
    if len(image_bytes) > max_bytes:
        raise UnsupportedImageError(
            f"That image is larger than {max_bytes // (1024 * 1024)}MB."
        )
    if content_type not in settings.SPENDIUM["ALLOWED_UPLOAD_TYPES"]:
        raise UnsupportedImageError("Please upload a photo of a receipt.")

    try:
        phash = imaging.perceptual_hash(image_bytes)
    except Exception as exc:  # Pillow raises a variety of decode errors.
        raise UnsupportedImageError("That file could not be read as an image.") from exc

    duplicate = find_duplicate(player, phash)
    if duplicate is not None:
        raise DuplicateReceiptError("You have already uploaded this receipt.")

    purchase = Purchase(
        player=player,
        purchased_at=timezone.now(),
        image_phash=phash,
        processing_status=Purchase.STATUS_PENDING,
    )
    purchase.receipt_image.save(filename, ContentFile(image_bytes), save=False)
    purchase.save()

    _enqueue_or_sweep("process-receipt", {"purchase_id": purchase.pk})
    _enqueue_or_sweep(
        "anonymise-purchase",
        {"purchase_id": purchase.pk},
        schedule_time=purchase.anonymise_after,
    )
    return purchase


def process_receipt(purchase_id: int, client: Any | None = None) -> Purchase | None:
    """Read a stored receipt and populate the purchase from it.

    Runs in a task. Failure is recorded on the purchase rather than raised: the
    player uploaded something and is owed an answer, even when the answer is
    that we could not read it.

    Deliberately not one transaction. Reading a receipt takes two calls to
    Gemini, and SQLite allows a single writer — so wrapping the whole function
    held the write lock across a model round trip, and everything else wanting to
    write, down to a session row on a page refresh, waited it out and failed with
    "database is locked".

    Instead: four phases, with the network calls outside the two short
    transactions. Read, persist, adjudicate, settle. The status only becomes
    `processed` in the last of them, which is what makes a run that dies partway
    safe — the purchase is left pending and the sweep starts it again from the
    top.
    """
    purchase = Purchase.objects.filter(pk=purchase_id).first()
    if purchase is None or purchase.processing_status != Purchase.STATUS_PENDING:
        return None

    if spending.is_stopped():
        # Checked here, not left to the client guard, so the receipt stays
        # pending instead of being marked failed. A failed receipt is gone and
        # the player must upload it again; a pending one just waits for the
        # sweeper. That difference is what makes the stop safe to pull.
        return None

    if not purchase.receipt_image:
        # Reachable when a receipt waited past the 24-hour image deletion — a
        # long emergency stop, or a task lost for a day. Worded as an action
        # because re-uploading now works: failed purchases are excluded from
        # duplicate detection.
        purchase.processing_status = Purchase.STATUS_FAILED
        purchase.processing_problems = [
            "We did not manage to read this receipt before its photo was "
            "deleted, as we promise to do within 24 hours. Please upload it "
            "again if you still have it."
        ]
        purchase.save(update_fields=["processing_status", "processing_problems"])
        return purchase

    # Phase 1 — read. No transaction: a fetch from GCS and a model call, neither
    # of which belongs anywhere near the write lock.
    try:
        # Read through a context manager so the handle is closed before the
        # deletion task runs. A leaked handle blocks the delete outright on
        # Windows, and on Linux leaks a descriptor per receipt — which is worse,
        # because it fails silently until the process runs out of them.
        with purchase.receipt_image.open("rb") as handle:
            image_bytes = handle.read()
        receipt = extraction.extract_receipt(image_bytes, client=client)
    except Exception as exc:
        purchase.processing_status = Purchase.STATUS_FAILED
        purchase.processing_problems = [str(exc)]
        purchase.save(update_fields=["processing_status", "processing_problems"])
        _enqueue_or_sweep("delete-receipt-image", {"purchase_id": purchase.pk})
        return purchase

    # Phase 2 — persist what was read.
    results = _record_extraction(purchase, receipt)

    # Phase 3 — ask the model about what matching could not place. Outside a
    # transaction, which is the entire point of the split: this is the call that
    # used to be made while holding the write lock.
    items = _adjudication_items(purchase, results)
    decisions = adjudication.adjudicate(items, client=client) if items else {}

    # Phase 4 — apply those decisions, then settle up. Order matters: points are
    # summed over the products on the line items, so paying before adjudication
    # would underpay the player for exactly the items the model resolved.
    _settle(purchase, decisions)

    # Anonymisation is already scheduled from accept_upload — the retention
    # window runs from when the player handed over the receipt, not from when
    # we got round to reading it.
    _enqueue_or_sweep("delete-receipt-image", {"purchase_id": purchase.pk})
    return purchase


@atomic
def _record_extraction(purchase: Purchase, receipt: Any) -> list[matching.MatchResult]:
    """Phase 2: everything the extraction produced, in one short transaction.

    Line items are recreated rather than appended to. A run that died between
    here and settlement leaves its lines behind and the sweep will retry the
    receipt, so appending would silently double it. Deleting first is safe
    because the player cannot have touched them yet — the disambiguation prompts
    render only for a processed purchase, and this one is still pending.
    """
    store = _resolve_store(receipt.store_name)
    purchase.store = store
    purchase.purchased_at = receipt.transaction_datetime or purchase.purchased_at
    purchase.subtotal = receipt.subtotal
    purchase.tax = receipt.tax
    purchase.total = receipt.total
    purchase.processing_problems = list(receipt.problems)
    purchase.save()

    purchase.line_items.all().delete()

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

    return results


@atomic
def _settle(purchase: Purchase, decisions: dict[int, Any]) -> None:
    """Phase 4: the model's decisions, the abuse check, the payout, the status."""
    _apply_adjudication(purchase, decisions)

    # Evaluated before payment, so a held purchase is never paid and then
    # clawed back. The receipt itself is already read and already counts toward
    # ratings — only the reward waits.
    abuse.evaluate(purchase)

    # Paid once the receipt has been read, not once it has been rated. Rating is
    # a bonus on top; gating the purchase reward on it would withhold points the
    # player has already earned.
    points.award_for_purchase(purchase)

    # Last, and deliberately so. Until this lands the purchase is still pending,
    # so a failure anywhere above leaves it to the sweep rather than stranding it
    # half-read and marked done.
    purchase.processing_status = Purchase.STATUS_PROCESSED
    purchase.save(update_fields=["processing_status"])


def _adjudication_items(
    purchase: Purchase,
    results: list[matching.MatchResult],
) -> list[adjudication.AdjudicationItem]:
    """What Tiers 0 and 1 missed, with their near-misses, for the model to judge.

    Only items with no product *and* at least one plausible candidate are worth
    asking about. An item with an empty candidate list has nothing to choose
    from, so a call would either return "none of these" or invent an answer.

    Read-only, and split from applying the answer, so the model call itself can
    sit outside a transaction. `index` is a position in the pk-ordered line
    items, which is the ordering `_apply_adjudication` reads back.
    """
    config = MatchConfig.get()
    top_k = config.adjudication_candidates
    if not top_k:
        return []

    lines = list(purchase.line_items.order_by("pk"))
    items = []
    for index, (line, result) in enumerate(zip(lines, results, strict=True)):
        if result.product is not None or not result.candidates:
            continue
        items.append(
            adjudication.AdjudicationItem(
                index=index,
                raw_text=line.raw_text,
                interpreted_name=line.interpreted_name,
                candidates=[
                    (candidate.product.pk, candidate.product.canonical_name)
                    for candidate in result.candidates[:top_k]
                ],
            )
        )

    return items


def _apply_adjudication(purchase: Purchase, decisions: dict[int, Any]) -> int:
    """Write what the model decided.

    A confident decision writes a provisional alias, which is what makes the
    resolution durable: the next receipt carrying that string hits Tier 0 and
    never reaches the model at all.
    """
    if not decisions:
        return 0

    lines = list(purchase.line_items.order_by("pk"))
    resolved = 0
    for index, decision in decisions.items():
        if not decision.resolved:
            continue
        line = lines[index]
        line.product_id = decision.product_id
        line.match_tier = MatchTier.ADJUDICATED
        # No confidence score: the model did not produce one, and inventing a
        # number here would be indistinguishable downstream from a measured
        # fuzzy score.
        line.match_confidence = None
        # Still prompted. A model decision is a strong signal, not a confirming
        # witness, and the shopper is the only party who held the product.
        line.disambiguation_state = PurchaseLineItem.STATE_PENDING
        line.save(
            update_fields=[
                "product",
                "match_tier",
                "match_confidence",
                "disambiguation_state",
            ]
        )
        _record_provisional_alias(line, decision.product_id, purchase.store)
        resolved += 1

    return resolved


def _record_provisional_alias(
    line: PurchaseLineItem, product_id: int, store: Store | None
) -> None:
    """Remember an adjudicated string so the next receipt resolves it for free.

    Provisional, never authoritative — promotion still needs two independent
    player confirmations. Sourced as adjudication so its later accuracy can be
    measured against what players decided.

    A string already claimed at this retailer is left alone. Uniqueness is
    global per (store, string), so overwriting would silently reassign every
    other receipt carrying it.
    """
    if not line.raw_text_normalised:
        return
    ProductAlias.objects.get_or_create(
        store=store,
        raw_text_normalised=line.raw_text_normalised,
        defaults={
            "product_id": product_id,
            "raw_text": line.raw_text,
            "source": ProductAlias.SOURCE_ADJUDICATION,
        },
    )


def delete_purchase(purchase: Purchase) -> None:
    """Erase one purchase at the player's request.

    The published policy is precise about what this does and does not touch:
    "Deleted purchases are removed from your record; aggregate store ratings
    derived from them are not retroactively altered." So the player-linked rows
    go, and any anonymous rows already written stay — they carry no route back
    to anyone.

    Aliases the player confirmed also stay. They are statements about what a
    receipt string means, not records of who bought what.
    """
    if purchase.receipt_image:
        purchase.receipt_image.delete(save=False)
    PointTransaction.objects.filter(
        content_type=ContentType.objects.get_for_model(Purchase),
        object_id=purchase.pk,
    ).update(content_type=None, object_id=None)
    purchase.delete()


def delete_purchase_history(player: Any) -> int:
    """Erase every purchase for a player. Returns how many were removed."""
    purchases = list(Purchase.objects.filter(player=player))
    for purchase in purchases:
        delete_purchase(purchase)
    return len(purchases)


def export_purchase_history(player: Any) -> list[dict[str, Any]]:
    """The player's purchase history, for the access right in the policy.

    Includes the raw receipt text alongside the resolved product, since the raw
    string is what the system actually acts on and a copy that hid it would be
    an incomplete answer.
    """
    export = []
    for purchase in (
        Purchase.objects.filter(player=player)
        .select_related("store")
        .prefetch_related("line_items__product")
        .order_by("-purchased_at")
    ):
        export.append(
            {
                "store": purchase.store.name if purchase.store else None,
                "purchased_at": purchase.purchased_at.isoformat(),
                "subtotal": str(purchase.subtotal) if purchase.subtotal else None,
                "tax": str(purchase.tax) if purchase.tax else None,
                "total": str(purchase.total),
                "status": purchase.processing_status,
                "anonymised_after": purchase.anonymise_after.isoformat(),
                "line_items": [
                    {
                        "receipt_text": line.raw_text,
                        "read_as": line.interpreted_name,
                        "matched_product": (
                            line.product.canonical_name if line.product else None
                        ),
                        "how_it_was_matched": line.match_tier,
                        "quantity": str(line.quantity),
                        "unit_price": str(line.unit_price) if line.unit_price else None,
                        "line_total": str(line.line_total),
                    }
                    for line in purchase.line_items.all()
                ],
            }
        )
    return export


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
    # The description is cleared alongside the reference. While the purchase
    # existed it merely repeated what the purchase row already held; once the
    # row is gone it would be the only surviving record of where and when
    # somebody shopped, and years of those amount to a movement trace more
    # revealing than the basket this whole step exists to destroy.
    PointTransaction.objects.filter(
        content_type=ContentType.objects.get_for_model(Purchase),
        object_id=purchase.pk,
    ).update(content_type=None, object_id=None, description="")

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


def pending_purchase_ids() -> list[int]:
    """Uploads still waiting to be read.

    Covers two cases with one sweep: receipts that waited out an emergency stop,
    and receipts whose `process-receipt` task was simply dropped. Before this
    existed the second case meant a receipt sat pending forever with nothing
    watching.

    A fresh upload is left alone for SWEEP_GRACE_MINUTES. Its own task is already
    in flight, and both running at once means two Gemini extractions of the same
    image and two writers contending for the one lock SQLite allows — which is
    how this sweep started causing the failures it exists to recover from.
    Neither case above is affected: both are older than the grace period by the
    time anything notices them.
    """
    grace: int = settings.SPENDIUM["SWEEP_GRACE_MINUTES"]
    cutoff = timezone.now() - timedelta(minutes=grace)
    return list(
        Purchase.objects.filter(
            processing_status=Purchase.STATUS_PENDING, created_at__lte=cutoff
        ).values_list("pk", flat=True)
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
