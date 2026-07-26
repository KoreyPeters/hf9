"""Turning an external product name into our canonical form.

`Product.canonical_name` is `[Brand] [Product Name] [Variant]` and deliberately
excludes size, because size variants collapse into one product line. External
catalogues do not follow that rule — Open Food Facts names routinely carry the
quantity, and often the brand as well — so imported names need reshaping before
they can sit alongside names produced by the rest of the system.

Getting this wrong is not cosmetic. A catalogue full of "Nutella 750g" and
"Nutella 400g" is a catalogue with two records for one product, each accruing
half the ratings and each taking twice as long to become publishable.
"""

import re

# Trailing size expressions: "250ml", "1.09 L", "300 g", "12pk", "6 x 330 ml".
_UNITS = (
    r"(?:mg|g|kg|ml|cl|l|litre|litres|liter|liters|oz|lb|lbs|pk|pack|packs|ct|count|x)"
)
_SIZE = re.compile(
    rf"""
    (?:^|\s|,|\(|-)
    \d+(?:[.,]\d+)?        # a number
    \s*
    (?:x\s*\d+(?:[.,]\d+)?\s*)?   # optional multipack "6 x 330"
    {_UNITS}\b
    \.?\)?                 # trailing punctuation
    """,
    re.IGNORECASE | re.VERBOSE,
)

_WHITESPACE = re.compile(r"\s{2,}")
_ORPHANED_PUNCTUATION = re.compile(r"\s+([,;])")


def strip_size(name: str) -> str:
    """Remove size expressions from a product name.

    Applied repeatedly because names carry more than one ("Coke 6 x 330ml
    Bottles 2L"). Anything that would strip the name down to nothing is left
    alone — for a product genuinely called "500g", the size *is* the name.
    """
    if not name:
        return ""

    cleaned = name
    for _ in range(4):
        stripped = _SIZE.sub(" ", cleaned)
        if stripped == cleaned:
            break
        cleaned = stripped

    # Removing a size from the middle of a name strands the punctuation that
    # separated it — "Bacon 1kg, Smoked" would otherwise become "Bacon , Smoked".
    cleaned = _ORPHANED_PUNCTUATION.sub(r"\1", cleaned)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip(" ,-–—()")
    return cleaned or name.strip()


def canonical_name(brand: str, product_name: str, quantity: str = "") -> str:
    """Assemble `[Brand] [Product Name]`, without the size.

    The quantity is removed by exact match first, since external catalogues
    usually state it in a dedicated field and repeat it verbatim in the name.
    That is more reliable than pattern-matching, which then handles whatever
    remains.
    """
    name = (product_name or "").strip()
    quantity = (quantity or "").strip()

    if quantity and quantity.lower() in name.lower():
        index = name.lower().index(quantity.lower())
        name = (name[:index] + " " + name[index + len(quantity) :]).strip()

    name = strip_size(name)
    brand = (brand or "").strip()

    if not brand:
        return _WHITESPACE.sub(" ", name).strip()
    if name.lower().startswith(brand.lower()):
        # Already brand-led; prefixing again would give "Heinz Heinz Ketchup".
        return _WHITESPACE.sub(" ", name).strip()
    return _WHITESPACE.sub(" ", f"{brand} {name}").strip()


def primary_brand(brands: str) -> str:
    """The first brand from a comma-separated list.

    Open Food Facts records every brand a product has ever carried. The first
    is the one on the packet.
    """
    if not brands:
        return ""
    return brands.split(",")[0].strip()
