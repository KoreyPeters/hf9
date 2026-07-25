"""Receipt image to structured line items.

This module's only job is *interpretation*: read what is printed on the receipt
and expand it into something a person would recognise. It does not match against
the catalogue, does not see catalogue records, and does not emit a confidence or
a product id. Matching is a separate stage (`spendium.matching`), and confidence
is a property of that match rather than of the model's reading.

That separation is not stylistic. The receipt image is deleted within 24 hours,
so a match welded to the image could never be revisited; keeping matching apart
means the stored raw text can be replayed against a larger catalogue months
later, with no image and no further model calls.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

QUALITY_GOOD = "good"
QUALITY_DEGRADED = "degraded"
QUALITY_POOR = "poor"

# Pure extraction. No catalogue_id and no match_type: the model is not being
# asked to decide what a line resolves to, only what it says.
RECEIPT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "store_name": {"type": "string"},
        "store_address": {"type": "string"},
        "transaction_datetime": {
            "type": "string",
            "description": "ISO 8601, e.g. 2026-05-30T14:23:00",
        },
        "image_quality": {
            "type": "string",
            "enum": [QUALITY_GOOD, QUALITY_DEGRADED, QUALITY_POOR],
            "description": (
                "good: all text clearly legible; degraded: some text unclear "
                "but most items readable; poor: significant portions unreadable"
            ),
        },
        "line_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "raw_text": {
                        "type": "string",
                        "description": "Verbatim text from the receipt, unaltered.",
                    },
                    "interpreted_name": {
                        "type": "string",
                        "description": "Expanded human-readable product name.",
                    },
                    "quantity": {"type": "number"},
                    "unit_price": {"type": "number"},
                    "line_total": {"type": "number"},
                },
                "required": ["raw_text", "interpreted_name", "line_total"],
            },
        },
        "subtotal": {"type": "number"},
        "tax": {"type": "number"},
        "total": {"type": "number"},
    },
    "required": ["store_name", "line_items", "total", "image_quality"],
}

SYSTEM_INSTRUCTION = """\
You are a receipt parsing specialist. Your job is to record exactly what a
customer purchased, expressed the way a consumer would recognise it rather than
in the store's internal codes.

For each line item on the receipt:
1. Copy the printed text verbatim into raw_text. Do not tidy or correct it.
   This string is a durable key elsewhere in the system, so it must be exact.
2. Expand abbreviations into interpreted_name using your knowledge of retail
   products, in the form: [Brand] [Product Name] [Variant] [Size].
   "TP-COLG-250"        -> "Colgate Toothpaste Bright Whitening 250ml"
   "PC BLK LBL COFF 300G" -> "President's Choice Black Label Ground Coffee 300g"
   "BNLS CHKN BRST KG"  -> "Chicken Breast Boneless per kg"
3. Never put a PLU code, UPC barcode or store SKU in interpreted_name.
4. Always give an interpreted_name, even when unsure. A best guess is more
   useful than a blank, and nothing downstream treats your reading as final.
5. Record discounts and returns as negative line_total values.

Assess the image honestly and report image_quality. Overstating legibility
causes silent misreads; saying an image is poor costs only a retry.

Do not attempt to identify which specific catalogue product a line refers to.
That decision is made elsewhere.
"""


@dataclass(frozen=True)
class ExtractedLineItem:
    raw_text: str
    interpreted_name: str
    line_total: Decimal
    quantity: Decimal = Decimal("1")
    unit_price: Decimal | None = None


@dataclass(frozen=True)
class ExtractedReceipt:
    store_name: str
    line_items: list[ExtractedLineItem]
    total: Decimal
    image_quality: str
    store_address: str = ""
    transaction_datetime: datetime | None = None
    subtotal: Decimal | None = None
    tax: Decimal | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def is_reliable(self) -> bool:
        return not self.problems


class ExtractionError(RuntimeError):
    """The model returned something unusable."""


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except InvalidOperation, ValueError:
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def check_arithmetic(receipt: ExtractedReceipt) -> list[str]:
    """Sanity-check the numbers.

    Structured output guarantees the *shape* of the response, never its
    correctness — a schema cannot notice that the line items do not add up. A
    receipt whose arithmetic is wrong has almost certainly been misread, and
    awarding points from it would award them for products nobody bought.
    """
    tolerance = Decimal(str(settings.SPENDIUM["ARITHMETIC_TOLERANCE"]))
    problems = []

    line_sum = sum((item.line_total for item in receipt.line_items), Decimal("0"))
    if receipt.subtotal is not None and abs(line_sum - receipt.subtotal) > tolerance:
        problems.append(
            f"Line items sum to {line_sum}, subtotal reads {receipt.subtotal}."
        )

    if receipt.subtotal is not None and receipt.tax is not None:
        expected = receipt.subtotal + receipt.tax
        if abs(expected - receipt.total) > tolerance:
            problems.append(
                f"Subtotal plus tax is {expected}, total reads {receipt.total}."
            )
    elif receipt.subtotal is None and abs(line_sum - receipt.total) > tolerance:
        problems.append(f"Line items sum to {line_sum}, total reads {receipt.total}.")

    if receipt.transaction_datetime is not None:
        now = timezone.now()
        max_age: int = settings.SPENDIUM["MAX_RECEIPT_AGE_DAYS"]
        if receipt.transaction_datetime > now + timedelta(days=1):
            problems.append("Transaction date is in the future.")
        elif receipt.transaction_datetime < now - timedelta(days=max_age):
            problems.append(f"Transaction date is more than {max_age} days old.")

    if not receipt.line_items:
        problems.append("No line items were read.")

    return problems


def parse_response(payload: str | dict[str, Any]) -> ExtractedReceipt:
    """Turn the model's JSON into a receipt, then check its arithmetic."""
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ExtractionError(f"Response was not valid JSON: {exc}") from exc
    else:
        data = payload

    if not isinstance(data, dict):
        raise ExtractionError("Response was not a JSON object.")

    total = _decimal(data.get("total"))
    if total is None:
        raise ExtractionError("Response had no usable total.")

    items = []
    for raw_item in data.get("line_items") or []:
        line_total = _decimal(raw_item.get("line_total"))
        if line_total is None:
            continue
        items.append(
            ExtractedLineItem(
                raw_text=str(raw_item.get("raw_text", "")).strip(),
                interpreted_name=str(raw_item.get("interpreted_name", "")).strip(),
                line_total=line_total,
                quantity=_decimal(raw_item.get("quantity")) or Decimal("1"),
                unit_price=_decimal(raw_item.get("unit_price")),
            )
        )

    receipt = ExtractedReceipt(
        store_name=str(data.get("store_name", "")).strip(),
        store_address=str(data.get("store_address", "")).strip(),
        transaction_datetime=_parse_datetime(data.get("transaction_datetime")),
        image_quality=str(data.get("image_quality", QUALITY_GOOD)),
        line_items=items,
        subtotal=_decimal(data.get("subtotal")),
        tax=_decimal(data.get("tax")),
        total=total,
    )
    return ExtractedReceipt(
        **{**receipt.__dict__, "problems": check_arithmetic(receipt)}
    )


def _client() -> Any:
    from google import genai

    return genai.Client(
        vertexai=True,
        project=settings.GCP_PROJECT,
        location=settings.SPENDIUM["GEMINI_LOCATION"],
    )


def _call(
    client: Any, image_bytes: bytes, mime_type: str, high_resolution: bool
) -> str:
    from google.genai import types

    resolution = (
        types.MediaResolution.MEDIA_RESOLUTION_HIGH
        if high_resolution
        else types.MediaResolution.MEDIA_RESOLUTION_MEDIUM
    )
    response = client.models.generate_content(
        model=settings.SPENDIUM["GEMINI_MODEL"],
        contents=[types.Part.from_bytes(data=image_bytes, mime_type=mime_type)],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=RECEIPT_SCHEMA,
            temperature=settings.SPENDIUM["GEMINI_TEMPERATURE"],
            media_resolution=resolution,
        ),
    )
    return response.text


def extract_receipt(
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
    client: Any | None = None,
) -> ExtractedReceipt:
    """Read a receipt image, retrying once at high resolution if it looks wrong.

    Medium resolution is the default because high resolution costs more tokens
    and more latency for little gain on a single-page thermal receipt. The retry
    is conditional rather than automatic: paying for a second pass is worth it
    only when the first pass shows evidence of having misread something — the
    arithmetic failing to reconcile, on an image the model itself called
    degraded or poor.

    A receipt that fails twice is returned with its problems attached rather
    than raised. It is a data-quality issue for review, not a crash: the caller
    still has the raw text, which is the durable part.
    """
    client = client or _client()

    receipt = parse_response(
        _call(client, image_bytes, mime_type, high_resolution=False)
    )
    if receipt.is_reliable:
        return receipt

    if receipt.image_quality == QUALITY_GOOD:
        # The model believes it read the image cleanly, so a sharper image is
        # unlikely to change its answer. The numbers are wrong for some other
        # reason and this needs review, not more tokens.
        return receipt

    logger.info(
        "Retrying receipt extraction at high resolution: quality=%s problems=%s",
        receipt.image_quality,
        receipt.problems,
    )
    retried = parse_response(
        _call(client, image_bytes, mime_type, high_resolution=True)
    )
    return retried if retried.is_reliable else retried
