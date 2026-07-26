"""Normalisation of raw receipt line-item text.

The normalised string is the primary alias key for Tier 0 matching, so this
function defines product identity at the string level. It is deliberately
conservative: over-normalising collapses genuinely distinct products onto one
key, which is far more damaging than leaving two spellings of the same product
un-merged. A missed alias costs one player prompt; a collided alias silently
awards ratings to the wrong product.
"""

import re
import unicodedata

_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")


def normalise_raw_text(raw: str | None) -> str:
    """Reduce a receipt string to its stable, comparable form.

    Lowercases, strips accents, replaces every run of non-alphanumeric
    characters with a single space, and collapses whitespace:

        "TP-COLG-250"       -> "tp colg 250"
        "TP  COLG 250"      -> "tp colg 250"
        "CAFÉ  BIO 300G"    -> "cafe bio 300g"

    Digits are preserved. They frequently carry the size or count that
    distinguishes one receipt string from another, and while size is not part
    of *product* identity, it is part of *string* identity — two size variants
    of one product line are two aliases pointing at the same record, not one
    shared alias.

    Retailer-specific noise (tax-code suffixes, department prefixes, loyalty
    markers) is deliberately not stripped here. Those rules vary per chain and
    belong with the retailer configuration introduced alongside the matching
    cascade, not in a shared normaliser applied to every receipt.
    """
    if not raw:
        return ""

    decomposed = unicodedata.normalize("NFKD", raw)
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = _NON_ALPHANUMERIC.sub(" ", without_accents.casefold())
    return " ".join(cleaned.split())
