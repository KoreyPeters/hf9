"""Seed the catalogue from an Open Food Facts export.

Reads a local dump rather than calling the API. Open Food Facts asks that bulk
work go through their published exports, and their search endpoint is heavily
rate-limited in any case — it returned 503 when probed. Download the JSONL dump
from https://world.openfoodfacts.org/data and point this at the file.

What seeding is and is not for
------------------------------

It does **not** help Tier 0. Open databases contain product names and barcodes,
never `TP-COLG-250`, so no alias can come from here — and this command
deliberately writes none.

Its value is at Tier 1 and in what it prevents. Against an empty catalogue every
first purchase matches nothing, so every line creates a new unverified record:
maximum duplicate fragmentation and the largest possible merge queue, arriving
exactly when the first players do. Seeding turns "no match, create a record"
into "weak match, confirm this" — a better prompt, and fragmentation avoided.

Crowd-sourced names are inconsistent. That is expected and acceptable; the alias
mechanism is what resolves it over time.
"""

import gzip
import json
from pathlib import Path
from typing import Any, Iterator

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from spendium import naming, search
from spendium.models import Manufacturer, Product, ProductUpc

# Grocery, pharmacy and household. Open Food Facts tags are hierarchical, so
# matching on a prefix catches the whole subtree.
DEFAULT_CATEGORY_PREFIXES = (
    "en:beverages",
    "en:dairies",
    "en:snacks",
    "en:groceries",
    "en:meats",
    "en:seafood",
    "en:plant-based-foods",
    "en:breakfasts",
    "en:frozen-foods",
    "en:canned-foods",
    "en:condiments",
    "en:baby-foods",
    "en:hygiene",
    "en:beauty",
    "en:household",
)


def _open(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("rt", encoding="utf-8", errors="replace")


def read_records(path: Path) -> Iterator[dict[str, Any]]:
    """Yield one product per line, skipping anything unparseable.

    A dump of several million crowd-sourced rows will contain malformed lines.
    Abandoning an import because of one is worse than ignoring it.
    """
    with _open(path) as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def is_wanted(record: dict[str, Any], country: str, prefixes: tuple[str, ...]) -> bool:
    """Whether this record is worth importing.

    Filtering hard matters: the full export is millions of products, most of
    them sold nowhere near our players. A catalogue stuffed with irrelevant
    records makes every Tier 1 query slower and every wrong match more likely.
    """
    if not (record.get("code") or "").strip():
        return False
    if not (record.get("product_name") or "").strip():
        return False

    countries = record.get("countries_tags") or []
    if country and not any(country in tag.lower() for tag in countries):
        return False

    if prefixes:
        categories = record.get("categories_tags") or []
        if not any(tag.startswith(prefixes) for tag in categories):
            return False
    return True


class Command(BaseCommand):
    help = (
        "Seed the product catalogue from an Open Food Facts JSONL export. "
        "Download the dump from https://world.openfoodfacts.org/data first."
    )

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "path", type=str, help="Path to the .jsonl or .jsonl.gz dump"
        )
        parser.add_argument(
            "--country",
            default="canada",
            help="Country tag to filter on. Empty string imports every country.",
        )
        parser.add_argument(
            "--limit", type=int, default=0, help="Stop after this many imports."
        )
        parser.add_argument(
            "--batch-size", type=int, default=1000, help="Rows per database batch."
        )
        parser.add_argument(
            "--all-categories",
            action="store_true",
            help="Skip category filtering.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        path = Path(options["path"])
        if not path.exists():
            raise CommandError(f"No such file: {path}")

        prefixes = () if options["all_categories"] else DEFAULT_CATEGORY_PREFIXES
        country = (options["country"] or "").lower()
        limit = options["limit"]

        created = skipped = existing = 0
        manufacturers: dict[str, Manufacturer] = {}
        batch: list[tuple[str, str, str]] = []

        def flush(rows: list[tuple[str, str, str]]) -> tuple[int, int]:
            """Write a batch. Returns (created, already present)."""
            nonlocal manufacturers
            made = seen = 0
            codes = [code for code, _, _ in rows]
            taken = set(
                ProductUpc.objects.filter(upc__in=codes).values_list("upc", flat=True)
            )
            with transaction.atomic():
                for code, name, brand in rows:
                    # Idempotency hangs on the UPC, which is unique. Re-running
                    # the same dump must not duplicate the catalogue.
                    if code in taken:
                        seen += 1
                        continue
                    manufacturer = None
                    if brand:
                        manufacturer = manufacturers.get(brand.lower())
                        if manufacturer is None:
                            manufacturer, _ = Manufacturer.objects.get_or_create(
                                name=brand
                            )
                            manufacturers[brand.lower()] = manufacturer
                    product = Product.objects.create(
                        canonical_name=name,
                        status=Product.STATUS_UNVERIFIED,
                        confidence_source=Product.SOURCE_UPC_LOOKUP,
                        manufacturer=manufacturer,
                    )
                    ProductUpc.objects.create(product=product, upc=code)
                    taken.add(code)
                    made += 1
            return made, seen

        for record in read_records(path):
            if not is_wanted(record, country, prefixes):
                skipped += 1
                continue

            brand = naming.primary_brand(record.get("brands") or "")
            name = naming.canonical_name(
                brand,
                record.get("product_name") or "",
                record.get("quantity") or "",
            )
            if not name:
                skipped += 1
                continue

            batch.append(((record["code"] or "").strip(), name, brand))
            if len(batch) >= options["batch_size"]:
                made, seen = flush(batch)
                created += made
                existing += seen
                batch = []
                self.stdout.write(f"  … {created} imported, {existing} already present")

            if limit and created >= limit:
                break

        if batch:
            made, seen = flush(batch)
            created += made
            existing += seen

        # bulk creation and get_or_create both bypass the per-row signal that
        # maintains the search index, so it is rebuilt once at the end rather
        # than per row.
        indexed = search.rebuild()

        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {created}, already present {existing}, "
                f"skipped {skipped}. Search index rebuilt over {indexed} products."
            )
        )
