"""Tier 2: targeted adjudication of what exact and fuzzy matching missed.

A second model call, text only and no image, handling just the line items that
survived Tiers 0 and 1 — each presented with the few catalogue candidates that
scored closest without clearing the bar.

This is the receipt draft's in-prompt matching idea applied at the right scope.
Feeding catalogue candidates to the model for *every* line on *every* receipt
would put confidence in the model's hands and make matching untestable offline.
Doing it for the handful of genuinely hard residuals costs a fraction of a cent
and plays to what a language model is actually good at: knowing that
`PC BLK LBL COFF` and "President's Choice Black Label Ground Coffee" are the
same thing, which no amount of string similarity will establish.

Every item it resolves is an item that would otherwise have consumed the
scarcest resource in the system — a player's attention.

Its answers are never treated as authoritative. A confident adjudication writes
a *provisional* alias: the model is a strong signal, not a confirming witness,
and two independent player confirmations are still what makes an alias
authoritative.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

ADJUDICATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "The item index given in the request.",
                    },
                    "product_id": {
                        "type": "integer",
                        "description": (
                            "The chosen candidate's id. Ignored when "
                            "none_of_these is true."
                        ),
                    },
                    "none_of_these": {
                        "type": "boolean",
                        "description": (
                            "True when no candidate is the product on the receipt line."
                        ),
                    },
                    "confident": {
                        "type": "boolean",
                        "description": (
                            "True only if you would stand behind this against "
                            "the person who bought it."
                        ),
                    },
                },
                "required": ["index", "none_of_these", "confident"],
            },
        }
    },
    "required": ["decisions"],
}

SYSTEM_INSTRUCTION = """\
You are resolving supermarket and pharmacy receipt lines against a product
catalogue. Each item gives the text printed on the receipt, a reading of what
that text means, and a short list of candidate catalogue products.

For each item, either choose the candidate that is the same product as the
receipt line, or set none_of_these to true.

Judge by product identity, not string similarity. The candidates have already
been scored for textual closeness and none of them won, so the question is
whether your knowledge of real products makes one of them right.

Size and packaging are not part of identity. A 250ml and a 100ml tube of the
same toothpaste are the same product, and a candidate that matches in brand and
variant is correct even if the receipt names a different size.

Brand and variant *are* part of identity. Two different brands of the same kind
of product are different products, and so are two variants of one brand.

Choosing none_of_these is a good answer, not a failure. A wrong choice writes a
lasting association that will be applied silently to every future receipt
carrying that string. Being unsure and saying so costs one question to the
shopper; being wrong costs a corrupted catalogue entry.

Set confident to false whenever you are guessing. An unconfident decision is
discarded, so there is no benefit in overstating certainty.
"""


@dataclass(frozen=True)
class AdjudicationItem:
    """One residual line and the candidates it will be judged against."""

    index: int
    raw_text: str
    interpreted_name: str
    candidates: list[tuple[int, str]]  # (product_id, canonical_name)


@dataclass(frozen=True)
class Adjudication:
    index: int
    product_id: int | None
    confident: bool

    @property
    def resolved(self) -> bool:
        """Only confident, positive decisions are worth acting on."""
        return self.confident and self.product_id is not None


def build_prompt(items: list[AdjudicationItem]) -> str:
    lines = []
    for item in items:
        lines.append(f"Item {item.index}")
        lines.append(f'  receipt text: "{item.raw_text}"')
        if item.interpreted_name:
            lines.append(f'  reading: "{item.interpreted_name}"')
        lines.append("  candidates:")
        for product_id, name in item.candidates:
            lines.append(f"    - id {product_id}: {name}")
        lines.append("")
    return "\n".join(lines)


def parse_response(payload: str | dict[str, Any]) -> list[Adjudication]:
    data = json.loads(payload) if isinstance(payload, str) else payload
    if not isinstance(data, dict):
        return []

    results = []
    for raw in data.get("decisions") or []:
        index = raw.get("index")
        if index is None:
            continue
        none_of_these = bool(raw.get("none_of_these"))
        product_id = raw.get("product_id")
        results.append(
            Adjudication(
                index=int(index),
                product_id=None if none_of_these else product_id,
                confident=bool(raw.get("confident")),
            )
        )
    return results


def _client() -> Any:
    from google import genai

    return genai.Client(
        vertexai=True,
        project=settings.GCP_PROJECT,
        location=settings.SPENDIUM["GEMINI_LOCATION"],
    )


def adjudicate(
    items: list[AdjudicationItem], client: Any | None = None
) -> dict[int, Adjudication]:
    """Resolve residual line items, returning decisions keyed by item index.

    One call for the whole receipt rather than one per line — the items are
    short, and batching keeps this to a single round trip no matter how long
    the receipt is.

    Decisions naming a candidate that was not offered are dropped. The model
    should not be inventing product ids, and honouring one would attach a line
    to an arbitrary catalogue record.
    """
    if not items:
        return {}

    from google.genai import types

    client = client or _client()
    response = client.models.generate_content(
        model=settings.SPENDIUM["GEMINI_MODEL"],
        contents=[build_prompt(items)],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=ADJUDICATION_SCHEMA,
            temperature=settings.SPENDIUM["GEMINI_TEMPERATURE"],
        ),
    )

    offered = {item.index: {pid for pid, _ in item.candidates} for item in items}
    decisions = {}
    for decision in parse_response(response.text):
        if decision.index not in offered:
            continue
        if (
            decision.product_id is not None
            and decision.product_id not in offered[decision.index]
        ):
            logger.warning(
                "Adjudication named product %s for item %s, which was not offered.",
                decision.product_id,
                decision.index,
            )
            continue
        decisions[decision.index] = decision
    return decisions
