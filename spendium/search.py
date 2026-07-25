"""FTS5 narrowing index over the product catalogue.

Scoring every canonical name for every line item does not scale: a seeded
catalogue of ~200k products against ~30 line items is ~6M string comparisons per
receipt, growing linearly with the catalogue forever. This module is Stage A of
Tier 1 — it reduces the catalogue to a few hundred plausible candidates using an
indexed BM25 query, so the cost of Stage B is bounded by the shortlist size
rather than by how large the catalogue has grown.

BM25 also supplies IDF weighting for free, which is the behaviour that matters
here: "colgate" is rare and dominates the ranking, while "toothpaste" and "ml"
are common and contribute almost nothing.

The table is deliberately *not* an FTS5 external-content table. External content
mirrors one source table column-for-column, and the text we need to search is
derived — a product's canonical name concatenated with every alias that resolves
to it. So this is a standalone table kept in step by signals, with a management
command to rebuild it wholesale after a bulk import.
"""

from django.db import connection

TABLE = "spendium_product_fts"


def _search_text(product_id: int) -> str | None:
    """Canonical name plus every alias string, as one searchable blob.

    Aliases are included so a garbled receipt string can still fuzzy-match a
    product through a *previously seen* spelling of itself, not only through
    the model's interpretation of it.
    """
    from .models import Product, ProductAlias

    product = Product.objects.filter(pk=product_id).first()
    if product is None:
        return None

    parts = [product.canonical_name]
    parts.extend(
        ProductAlias.objects.filter(product_id=product_id)
        .exclude(status=ProductAlias.STATUS_DEMOTED)
        .values_list("raw_text_normalised", flat=True)
    )
    return " ".join(part for part in parts if part)


def remove_product(product_id: int) -> None:
    with connection.cursor() as cursor:
        cursor.execute(f"DELETE FROM {TABLE} WHERE product_id = %s", [product_id])


def index_product(product_id: int) -> None:
    """Refresh one product's row. Called on every Product/ProductAlias write."""
    text = _search_text(product_id)
    remove_product(product_id)
    if not text:
        return
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {TABLE} (search_text, product_id) VALUES (%s, %s)",
            [text, product_id],
        )


def rebuild() -> int:
    """Rebuild the whole index. For use after seeding or a schema change."""
    from .models import Product

    with connection.cursor() as cursor:
        cursor.execute(f"DELETE FROM {TABLE}")

    count = 0
    for product_id in Product.objects.values_list("pk", flat=True).iterator():
        index_product(product_id)
        count += 1
    return count


def _match_expression(normalised_query: str) -> str:
    """Build an FTS5 MATCH expression from an already-normalised query.

    Tokens are OR-ed rather than AND-ed. This stage is for recall — BM25 does
    the discriminating, and requiring every token would drop the very cases
    that need fuzzy scoring, where the receipt is missing or mangling a word.

    Normalisation has already reduced the string to alphanumerics and single
    spaces, so no FTS5 syntax characters can survive to be injected here.
    """
    tokens = [token for token in normalised_query.split() if token]
    return " OR ".join(tokens)


def narrow(normalised_query: str, limit: int) -> list[int]:
    """Return candidate product IDs, best BM25 rank first."""
    expression = _match_expression(normalised_query)
    if not expression:
        return []

    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT product_id FROM {TABLE} WHERE {TABLE} MATCH %s "
            f"ORDER BY rank LIMIT %s",
            [expression, limit],
        )
        return [row[0] for row in cursor.fetchall()]
