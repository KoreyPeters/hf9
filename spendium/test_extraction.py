"""Receipt extraction, arithmetic checks, and the image deletion commitment.

No test here reaches the network. The model is replaced by a fake returning
recorded JSON, which is enough because this module's contract is the *shape and
sanity* of what comes back, not the model's reading ability. Testing against
live calls would be slow, non-deterministic, and would tell us nothing about our
own code that a recorded response does not.
"""

import json
from datetime import timedelta
from decimal import Decimal
from io import BytesIO

import pytest
from django.utils import timezone
from PIL import Image

from accounts.models import Player
from spendium import extraction, imaging, service
from spendium.models import Product, ProductAlias, Purchase, PurchaseLineItem, Store


# ── Fakes and fixtures ────────────────────────────────────────────────────────


class FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeModels:
    def __init__(self, payloads: list[str]) -> None:
        self._payloads = list(payloads)
        self.calls: list[dict] = []

    def generate_content(
        self, *, model: str, contents: list, config: object
    ) -> FakeResponse:
        self.calls.append({"model": model, "config": config, "contents": contents})
        payload = (
            self._payloads.pop(0) if len(self._payloads) > 1 else self._payloads[0]
        )
        return FakeResponse(payload)


class FakeClient:
    """Stands in for genai.Client. Returns each recorded payload in turn."""

    def __init__(self, *payloads: str) -> None:
        self.models = FakeModels(list(payloads))


def receipt_payload(**overrides: object) -> str:
    data = {
        "store_name": "Shoppers Drug Mart",
        "store_address": "123 Main St",
        "transaction_datetime": timezone.now().isoformat(),
        "image_quality": "good",
        "line_items": [
            {
                "raw_text": "TP-COLG-250",
                "interpreted_name": "Colgate Toothpaste Bright Whitening 250ml",
                "quantity": 1,
                "unit_price": 4.99,
                "line_total": 4.99,
            },
            {
                "raw_text": "HEINZ KETCHUP 750ML",
                "interpreted_name": "Heinz Tomato Ketchup 750ml",
                "quantity": 1,
                "unit_price": 3.49,
                "line_total": 3.49,
            },
        ],
        "subtotal": 8.48,
        "tax": 1.10,
        "total": 9.58,
    }
    data.update(overrides)
    return json.dumps(data)


def png_bytes(colour: int = 128, size: tuple[int, int] = (64, 64)) -> bytes:
    """A minimal valid image, for tests that only need decodable bytes."""
    buffer = BytesIO()
    Image.new("L", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def patterned_image(seed: int = 0, size: tuple[int, int] = (64, 64)) -> Image.Image:
    """A deterministic image with variation along both axes.

    Difference hashing compares each pixel with its right-hand neighbour, so
    hash tests need horizontal structure — the kind a receipt's text has, and
    a flat field or a vertical gradient does not.

    `seed` alters the horizontal frequency rather than the brightness. Offsetting
    every pixel by a constant would leave neighbour differences untouched, so the
    hash would rightly call the result the same image — dHash is brightness
    invariant by design, which is what lets it survive a change in lighting.
    """
    image = Image.new("L", size)
    pixels = image.load()
    horizontal = 5 + seed * 11
    for y in range(size[1]):
        for x in range(size[0]):
            pixels[x, y] = (x * horizontal + y * 13) % 256
    return image


@pytest.fixture
def shopper(db: None) -> Player:
    return Player.objects.create_user(username="shopper", email="shopper@example.com")


@pytest.fixture(autouse=True)
def isolated_media(settings, tmp_path) -> None:
    """Keep uploaded files out of the working tree."""
    settings.MEDIA_ROOT = tmp_path


# ── Schema ────────────────────────────────────────────────────────────────────


def test_schema_asks_only_for_extraction() -> None:
    """The model interprets; it never decides which catalogue record applies.

    Confidence is a property of the match, not of the reading, so a schema that
    invited the model to emit one would put the decision in the wrong place —
    and would weld the result to an image deleted within 24 hours.
    """
    properties = extraction.RECEIPT_SCHEMA["properties"]["line_items"]["items"][
        "properties"
    ]
    assert "catalogue_id" not in properties
    assert "match_type" not in properties
    assert "confidence" not in properties
    assert {"raw_text", "interpreted_name", "line_total"} <= set(properties)


def test_prompt_tells_the_model_not_to_alter_raw_text() -> None:
    """raw_text is the durable alias key — a tidied one would not match again."""
    assert "verbatim" in extraction.SYSTEM_INSTRUCTION.lower()


# ── Parsing ───────────────────────────────────────────────────────────────────


def test_parses_a_clean_receipt() -> None:
    receipt = extraction.parse_response(receipt_payload())
    assert receipt.store_name == "Shoppers Drug Mart"
    assert receipt.total == Decimal("9.58")
    assert len(receipt.line_items) == 2
    assert receipt.line_items[0].raw_text == "TP-COLG-250"
    assert receipt.is_reliable


def test_money_is_parsed_as_decimal_not_float() -> None:
    """Float arithmetic on money accumulates error the tolerance would mask."""
    receipt = extraction.parse_response(receipt_payload())
    assert isinstance(receipt.total, Decimal)
    assert isinstance(receipt.line_items[0].line_total, Decimal)


def test_invalid_json_raises() -> None:
    with pytest.raises(extraction.ExtractionError):
        extraction.parse_response("not json at all")


def test_missing_total_raises() -> None:
    payload = json.loads(receipt_payload())
    del payload["total"]
    with pytest.raises(extraction.ExtractionError):
        extraction.parse_response(json.dumps(payload))


def test_line_without_a_total_is_dropped() -> None:
    payload = json.loads(receipt_payload())
    payload["line_items"].append({"raw_text": "???", "interpreted_name": ""})
    receipt = extraction.parse_response(json.dumps(payload))
    assert len(receipt.line_items) == 2


def test_naive_datetimes_are_made_aware() -> None:
    receipt = extraction.parse_response(
        receipt_payload(transaction_datetime="2026-07-20T14:23:00")
    )
    assert receipt.transaction_datetime is not None
    assert timezone.is_aware(receipt.transaction_datetime)


# ── Arithmetic checks ─────────────────────────────────────────────────────────


def test_line_items_must_sum_to_subtotal() -> None:
    """Structured output guarantees shape, never correctness."""
    receipt = extraction.parse_response(receipt_payload(subtotal=99.99))
    assert not receipt.is_reliable
    assert any("subtotal" in p.lower() for p in receipt.problems)


def test_subtotal_plus_tax_must_equal_total() -> None:
    receipt = extraction.parse_response(receipt_payload(total=50.00))
    assert not receipt.is_reliable


def test_small_rounding_is_tolerated() -> None:
    """Weight-priced lines round; that is not a misread."""
    receipt = extraction.parse_response(receipt_payload(total=9.60))
    assert receipt.is_reliable


def test_future_dated_receipt_is_flagged() -> None:
    future = (timezone.now() + timedelta(days=5)).isoformat()
    receipt = extraction.parse_response(receipt_payload(transaction_datetime=future))
    assert any("future" in p.lower() for p in receipt.problems)


def test_receipt_older_than_the_window_is_flagged() -> None:
    """A receipt that can never be rated is not worth accepting."""
    old = (timezone.now() - timedelta(days=45)).isoformat()
    receipt = extraction.parse_response(receipt_payload(transaction_datetime=old))
    assert any("old" in p.lower() for p in receipt.problems)


def test_empty_receipt_is_flagged() -> None:
    receipt = extraction.parse_response(
        receipt_payload(line_items=[], subtotal=0, tax=0, total=0)
    )
    assert any("no line items" in p.lower() for p in receipt.problems)


def test_negative_lines_are_accepted_as_refunds() -> None:
    """Refunds are legitimate receipt content; they just must not earn points."""
    receipt = extraction.parse_response(
        receipt_payload(
            line_items=[
                {
                    "raw_text": "MILK 2L",
                    "interpreted_name": "Milk 2L",
                    "line_total": 5.00,
                },
                {
                    "raw_text": "REFUND",
                    "interpreted_name": "Refund",
                    "line_total": -2.00,
                },
            ],
            subtotal=3.00,
            tax=0.00,
            total=3.00,
        )
    )
    assert receipt.is_reliable
    assert len(receipt.line_items) == 2


# ── High-resolution retry ─────────────────────────────────────────────────────


def test_clean_first_pass_does_not_retry() -> None:
    client = FakeClient(receipt_payload())
    extraction.extract_receipt(png_bytes(), client=client)
    assert len(client.models.calls) == 1


def test_bad_arithmetic_on_a_degraded_image_retries() -> None:
    client = FakeClient(
        receipt_payload(image_quality="degraded", total=99.99),
        receipt_payload(),
    )
    receipt = extraction.extract_receipt(png_bytes(), client=client)
    assert len(client.models.calls) == 2
    assert receipt.is_reliable


def test_bad_arithmetic_on_a_good_image_does_not_retry() -> None:
    """If the model read the image cleanly, a sharper copy will not change it.

    The numbers are wrong for some other reason, so a second pass spends tokens
    to get the same answer.
    """
    client = FakeClient(receipt_payload(image_quality="good", total=99.99))
    receipt = extraction.extract_receipt(png_bytes(), client=client)
    assert len(client.models.calls) == 1
    assert not receipt.is_reliable


def test_retry_uses_high_media_resolution() -> None:
    client = FakeClient(
        receipt_payload(image_quality="poor", total=99.99),
        receipt_payload(),
    )
    extraction.extract_receipt(png_bytes(), client=client)
    first, second = client.models.calls
    assert "MEDIUM" in str(first["config"].media_resolution)
    assert "HIGH" in str(second["config"].media_resolution)


def test_a_receipt_failing_twice_is_returned_not_raised() -> None:
    """A data-quality problem is for review; it should not lose the raw text."""
    client = FakeClient(receipt_payload(image_quality="poor", total=99.99))
    receipt = extraction.extract_receipt(png_bytes(), client=client)
    assert not receipt.is_reliable
    assert receipt.line_items


# ── Perceptual hashing ────────────────────────────────────────────────────────


def test_hash_is_stable_for_identical_bytes() -> None:
    image = png_bytes()
    assert imaging.perceptual_hash(image) == imaging.perceptual_hash(image)


def test_hash_survives_rescaling() -> None:
    """Two photos of one receipt differ byte-for-byte; the hash must not."""
    original = patterned_image()
    big, small = BytesIO(), BytesIO()
    original.save(big, format="PNG")
    original.resize((160, 160)).save(small, format="PNG")
    distance = imaging.hamming_distance(
        imaging.perceptual_hash(big.getvalue()),
        imaging.perceptual_hash(small.getvalue()),
    )
    assert distance <= 6


def test_hash_survives_recompression() -> None:
    lossless, lossy = BytesIO(), BytesIO()
    image = patterned_image().convert("RGB")
    image.save(lossless, format="PNG")
    image.save(lossy, format="JPEG", quality=40)
    distance = imaging.hamming_distance(
        imaging.perceptual_hash(lossless.getvalue()),
        imaging.perceptual_hash(lossy.getvalue()),
    )
    assert distance <= 6


def test_hash_differs_for_different_images() -> None:
    buffers = []
    for seed in (0, 5):
        buffer = BytesIO()
        patterned_image(seed=seed).save(buffer, format="PNG")
        buffers.append(buffer.getvalue())
    distance = imaging.hamming_distance(
        imaging.perceptual_hash(buffers[0]), imaging.perceptual_hash(buffers[1])
    )
    assert distance > 6


def test_images_without_horizontal_variation_collide() -> None:
    """A documented limitation of difference hashing, recorded deliberately.

    Every bit compares a pixel with its right-hand neighbour, so an image that
    is uniform across each row carries no information — a flat field and a
    vertical gradient both hash to zero. Receipts are dense horizontal text and
    never look like this, but the check is worth stating rather than
    rediscovering as a mysterious duplicate flag.
    """
    vertical_gradient = BytesIO()
    Image.linear_gradient("L").save(vertical_gradient, format="PNG")
    assert imaging.perceptual_hash(vertical_gradient.getvalue()) == "0" * 16
    assert imaging.perceptual_hash(png_bytes()) == "0" * 16


def test_hamming_distance_rejects_mismatched_lengths() -> None:
    """Silently returning a big number would read as 'not a duplicate'."""
    with pytest.raises(ValueError):
        imaging.hamming_distance("abcd", "abcdef")


# ── Recording a receipt end to end ────────────────────────────────────────────


@pytest.mark.django_db
def test_record_receipt_creates_purchase_and_lines(shopper: Player) -> None:
    purchase = service.record_receipt(
        shopper, png_bytes(), client=FakeClient(receipt_payload())
    )
    assert purchase.player == shopper
    assert purchase.total == Decimal("9.58")
    assert purchase.line_items.count() == 2


@pytest.mark.django_db
def test_record_receipt_creates_the_store(shopper: Player) -> None:
    service.record_receipt(shopper, png_bytes(), client=FakeClient(receipt_payload()))
    assert Store.objects.filter(name="Shoppers Drug Mart").exists()


@pytest.mark.django_db
def test_record_receipt_reuses_an_existing_store(shopper: Player) -> None:
    Store.objects.create(name="Shoppers Drug Mart")
    service.record_receipt(shopper, png_bytes(), client=FakeClient(receipt_payload()))
    assert Store.objects.filter(name__iexact="Shoppers Drug Mart").count() == 1


@pytest.mark.django_db
def test_record_receipt_normalises_raw_text(shopper: Player) -> None:
    """bulk_create skips save(), so normalisation has to be applied explicitly."""
    service.record_receipt(shopper, png_bytes(), client=FakeClient(receipt_payload()))
    line = PurchaseLineItem.objects.get(raw_text="TP-COLG-250")
    assert line.raw_text_normalised == "tp colg 250"


@pytest.mark.django_db
def test_record_receipt_matches_against_the_catalogue(shopper: Player) -> None:
    product = Product.objects.create(canonical_name="Heinz Tomato Ketchup")
    service.record_receipt(shopper, png_bytes(), client=FakeClient(receipt_payload()))
    line = PurchaseLineItem.objects.get(raw_text="HEINZ KETCHUP 750ML")
    assert line.product == product


@pytest.mark.django_db
def test_record_receipt_uses_retailer_scoped_aliases(shopper: Player) -> None:
    store = Store.objects.create(name="Shoppers Drug Mart")
    product = Product.objects.create(
        canonical_name="Colgate Toothpaste Bright Whitening"
    )
    ProductAlias.objects.create(product=product, store=store, raw_text="TP-COLG-250")
    service.record_receipt(shopper, png_bytes(), client=FakeClient(receipt_payload()))
    line = PurchaseLineItem.objects.get(raw_text="TP-COLG-250")
    assert line.product == product
    assert line.match_confidence == Decimal("1.000")


@pytest.mark.django_db
def test_unmatched_lines_are_marked_for_disambiguation(shopper: Player) -> None:
    service.record_receipt(shopper, png_bytes(), client=FakeClient(receipt_payload()))
    assert PurchaseLineItem.objects.filter(
        disambiguation_state=PurchaseLineItem.STATE_PENDING
    ).exists()


@pytest.mark.django_db
def test_record_receipt_stores_the_hash(shopper: Player) -> None:
    image = png_bytes()
    purchase = service.record_receipt(
        shopper, image, client=FakeClient(receipt_payload())
    )
    assert purchase.image_phash == imaging.perceptual_hash(image)


@pytest.mark.django_db
def test_negative_lines_are_identified_for_exclusion(shopper: Player) -> None:
    payload = receipt_payload(
        line_items=[
            {"raw_text": "MILK 2L", "interpreted_name": "Milk 2L", "line_total": 5.00},
            {"raw_text": "REFUND", "interpreted_name": "Refund", "line_total": -2.00},
        ],
        subtotal=3.00,
        tax=0.00,
        total=3.00,
    )
    purchase = service.record_receipt(shopper, png_bytes(), client=FakeClient(payload))
    assert len(service.negative_line_total_ids(purchase)) == 1


# ── The image deletion commitment ─────────────────────────────────────────────


@pytest.mark.django_db
def test_image_is_deleted_immediately_after_processing(shopper: Player) -> None:
    """DEBUG runs enqueued tasks inline, so the deletion happens during the call.

    The published promise is 24 hours, but the cheapest way to honour a deletion
    promise is to have nothing left to delete.
    """
    purchase = service.record_receipt(
        shopper, png_bytes(), client=FakeClient(receipt_payload())
    )
    purchase.refresh_from_db()
    assert not purchase.receipt_image
    assert purchase.image_deleted_at is not None


@pytest.mark.django_db
def test_deleting_an_image_is_idempotent(shopper: Player) -> None:
    purchase = service.record_receipt(
        shopper, png_bytes(), client=FakeClient(receipt_payload())
    )
    assert service.delete_receipt_image(purchase.pk) is False


@pytest.mark.django_db
def test_hash_outlives_the_image(shopper: Player) -> None:
    """Duplicate detection has to keep working after the image is gone."""
    purchase = service.record_receipt(
        shopper, png_bytes(), client=FakeClient(receipt_payload())
    )
    purchase.refresh_from_db()
    assert not purchase.receipt_image
    assert purchase.image_phash


@pytest.mark.django_db
def test_sweeper_stamps_purchases_with_no_file(shopper: Player) -> None:
    """A failed upload must not sit in the sweeper's queue forever."""
    purchase = Purchase.objects.create(
        player=shopper,
        purchased_at=timezone.now(),
        total=Decimal("1.00"),
    )
    assert service.delete_receipt_image(purchase.pk) is True
    purchase.refresh_from_db()
    assert purchase.image_deleted_at is not None
