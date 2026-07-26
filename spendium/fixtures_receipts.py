"""Labelled receipt strings for tuning the matching thresholds.

Thresholds cannot be set meaningfully by argument — they need a fixed set of
real inputs with known right answers, so a change to `MatchConfig` can be judged
by what it does to precision and recall rather than by how reasonable the number
sounds.

This set is deliberately small and hand-written, covering the failure shapes we
expect from Canadian grocery and pharmacy receipts. It should grow from real
misses observed in production — that is the point of keeping it in code rather
than generating it.

Each entry is (raw receipt string, model interpretation, expected canonical name
or None where nothing in the catalogue should match).
"""

CATALOGUE = [
    "Colgate Toothpaste Bright Whitening",
    "Colgate Toothpaste Cavity Protection",
    "Crest 3D White Toothpaste",
    "President's Choice Black Label Ground Coffee",
    "President's Choice Organic Whole Milk",
    "Heinz Tomato Ketchup",
    "Kirkland Signature Bacon",
    "Tide Laundry Detergent Original",
]

# (raw_text, interpreted_name, expected_canonical_name | None)
LABELLED = [
    # Store SKU codes — the case Tier 0 exists for, before an alias is learned.
    (
        "TP-COLG-250",
        "Colgate Toothpaste Bright Whitening 250ml",
        "Colgate Toothpaste Bright Whitening",
    ),
    (
        "COLG TP BWHT 250",
        "Colgate Toothpaste Bright Whitening 250ml",
        "Colgate Toothpaste Bright Whitening",
    ),
    (
        "COLG TP CAV 100",
        "Colgate Toothpaste Cavity Protection 100ml",
        "Colgate Toothpaste Cavity Protection",
    ),
    # Size variants must collapse onto one product line.
    (
        "TP-COLG-100",
        "Colgate Toothpaste Bright Whitening 100ml",
        "Colgate Toothpaste Bright Whitening",
    ),
    # Store-brand abbreviations.
    (
        "PC BLK LBL COFF 300G",
        "President's Choice Black Label Ground Coffee 300g",
        "President's Choice Black Label Ground Coffee",
    ),
    (
        "PC ORG WHL MILK 2L",
        "President's Choice Organic Whole Milk 2L",
        "President's Choice Organic Whole Milk",
    ),
    # Competing brands in one category must not cross-match.
    (
        "CREST 3DW TP 75ML",
        "Crest 3D White Toothpaste 75ml",
        "Crest 3D White Toothpaste",
    ),
    # Ordinary branded goods.
    ("HEINZ KETCHUP 750ML", "Heinz Tomato Ketchup 750ml", "Heinz Tomato Ketchup"),
    (
        "TIDE ORIG HE 1.09L",
        "Tide Laundry Detergent Original 1.09L",
        "Tide Laundry Detergent Original",
    ),
    ("KIRKLAND BACON 1KG", "Kirkland Signature Bacon 1kg", "Kirkland Signature Bacon"),
    # Weight-priced items with nothing in the catalogue — must not force a match.
    ("BNLS CHKN BRST KG", "Chicken Breast Boneless per kg", None),
    ("BANANAS 4011", "Bananas", None),
]
