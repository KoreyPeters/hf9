"""Catalogue seeding from an Open Food Facts export.

Runs against a small fixture dump written per test rather than the real
multi-gigabyte file, so the tests exercise the filtering, naming and idempotency
rules without a download.
"""

import gzip
import json
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from spendium import naming, search
from spendium.models import Manufacturer, Product, ProductAlias, ProductUpc


def record(**overrides) -> dict:
    base = {
        "code": "0064420000064",
        "product_name": "Tomato Ketchup 750 ml",
        "brands": "Heinz",
        "quantity": "750 ml",
        "countries_tags": ["en:canada", "en:united-states"],
        "categories_tags": ["en:condiments", "en:sauces"],
    }
    base.update(overrides)
    return base


def write_dump(tmp_path: Path, *records: dict, gzipped: bool = False) -> Path:
    lines = "\n".join(json.dumps(r) for r in records)
    if gzipped:
        path = tmp_path / "dump.jsonl.gz"
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(lines)
    else:
        path = tmp_path / "dump.jsonl"
        path.write_text(lines, encoding="utf-8")
    return path


def run_import(path: Path, **kwargs) -> str:
    out = StringIO()
    call_command("import_open_food_facts", str(path), stdout=out, **kwargs)
    return out.getvalue()


# ── Size stripping ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Nutella 750g", "Nutella"),
        ("Nutella 750 g", "Nutella"),
        ("Coca-Cola 1.5 L", "Coca-Cola"),
        ("Eggs Large 12pk", "Eggs Large"),
        ("Water 6 x 500ml", "Water"),
        ("Milk 2L Organic", "Milk Organic"),
        # Punctuation the size was sitting behind is tidied, not left stranded.
        ("Bacon 1kg, Smoked", "Bacon, Smoked"),
        ("Heinz Tomato Ketchup", "Heinz Tomato Ketchup"),
    ],
)
def test_strip_size(raw: str, expected: str) -> None:
    assert naming.strip_size(raw) == expected


def test_a_name_that_is_only_a_size_is_left_alone() -> None:
    """Stripping everything would leave a product with no name at all."""
    assert naming.strip_size("500g") == "500g"


def test_canonical_name_removes_the_stated_quantity() -> None:
    """External catalogues repeat the quantity verbatim in the name."""
    assert (
        naming.canonical_name("Heinz", "Tomato Ketchup 750 ml", "750 ml")
        == "Heinz Tomato Ketchup"
    )


def test_canonical_name_does_not_repeat_the_brand() -> None:
    assert (
        naming.canonical_name("Heinz", "Heinz Tomato Ketchup", "")
        == "Heinz Tomato Ketchup"
    )


def test_canonical_name_prepends_a_missing_brand() -> None:
    assert (
        naming.canonical_name("Heinz", "Tomato Ketchup", "") == "Heinz Tomato Ketchup"
    )


def test_primary_brand_takes_the_first() -> None:
    """Open Food Facts lists every brand a product has ever carried."""
    assert naming.primary_brand("Heinz,H.J. Heinz,Kraft Heinz") == "Heinz"


# ── Importing ─────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_import_creates_unverified_products(tmp_path: Path) -> None:
    run_import(write_dump(tmp_path, record()))
    product = Product.objects.get()
    assert product.canonical_name == "Heinz Tomato Ketchup"
    assert product.status == Product.STATUS_UNVERIFIED
    assert product.confidence_source == Product.SOURCE_UPC_LOOKUP


@pytest.mark.django_db
def test_import_populates_upcs(tmp_path: Path) -> None:
    run_import(write_dump(tmp_path, record()))
    assert ProductUpc.objects.get().upc == "0064420000064"


@pytest.mark.django_db
def test_import_creates_manufacturers(tmp_path: Path) -> None:
    run_import(write_dump(tmp_path, record()))
    assert Manufacturer.objects.get().name == "Heinz"
    assert Product.objects.get().manufacturer.name == "Heinz"


@pytest.mark.django_db
def test_import_writes_no_aliases(tmp_path: Path) -> None:
    """Open data never contains retailer strings, so it cannot seed Tier 0.

    Inventing aliases from product names would put unconfirmed guesses on the
    one path that matches deterministically and silently.
    """
    run_import(write_dump(tmp_path, record()))
    assert ProductAlias.objects.count() == 0


@pytest.mark.django_db
def test_import_reads_gzipped_dumps(tmp_path: Path) -> None:
    run_import(write_dump(tmp_path, record(), gzipped=True))
    assert Product.objects.count() == 1


@pytest.mark.django_db
def test_import_rebuilds_the_search_index(tmp_path: Path) -> None:
    """Bulk paths bypass the per-row signal that maintains it."""
    run_import(write_dump(tmp_path, record()))
    assert search.narrow("heinz ketchup", 10) == [Product.objects.get().pk]


# ── Idempotency ───────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_re_running_the_same_dump_changes_nothing(tmp_path: Path) -> None:
    """Seeding will be re-run as the dump is refreshed."""
    path = write_dump(tmp_path, record())
    run_import(path)
    output = run_import(path)
    assert Product.objects.count() == 1
    assert "already present 1" in output


@pytest.mark.django_db
def test_a_duplicate_upc_within_one_dump_is_imported_once(tmp_path: Path) -> None:
    run_import(write_dump(tmp_path, record(), record(product_name="Ketchup Again")))
    assert Product.objects.count() == 1


# ── Filtering ─────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_products_not_sold_in_the_country_are_skipped(tmp_path: Path) -> None:
    run_import(write_dump(tmp_path, record(countries_tags=["en:france"])))
    assert Product.objects.count() == 0


@pytest.mark.django_db
def test_the_country_filter_can_be_disabled(tmp_path: Path) -> None:
    run_import(write_dump(tmp_path, record(countries_tags=["en:france"])), country="")
    assert Product.objects.count() == 1


@pytest.mark.django_db
def test_out_of_scope_categories_are_skipped(tmp_path: Path) -> None:
    """A catalogue full of irrelevant records slows every query and worsens matching."""
    run_import(write_dump(tmp_path, record(categories_tags=["en:car-parts"])))
    assert Product.objects.count() == 0


@pytest.mark.django_db
def test_category_filtering_can_be_disabled(tmp_path: Path) -> None:
    run_import(
        write_dump(tmp_path, record(categories_tags=["en:car-parts"])),
        all_categories=True,
    )
    assert Product.objects.count() == 1


@pytest.mark.django_db
def test_records_without_a_name_are_skipped(tmp_path: Path) -> None:
    run_import(write_dump(tmp_path, record(product_name="")))
    assert Product.objects.count() == 0


@pytest.mark.django_db
def test_records_without_a_barcode_are_skipped(tmp_path: Path) -> None:
    """The UPC is what makes re-running idempotent."""
    run_import(write_dump(tmp_path, record(code="")))
    assert Product.objects.count() == 0


# ── Robustness ────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_malformed_lines_do_not_stop_the_import(tmp_path: Path) -> None:
    """A crowd-sourced dump of millions of rows will contain broken ones."""
    path = tmp_path / "dump.jsonl"
    path.write_text(
        "\n".join(["{not json", json.dumps(record()), "", "[1,2,3]"]), encoding="utf-8"
    )
    run_import(path)
    assert Product.objects.count() == 1


@pytest.mark.django_db
def test_a_missing_file_is_reported_clearly(tmp_path: Path) -> None:
    with pytest.raises(CommandError):
        run_import(tmp_path / "nope.jsonl")


@pytest.mark.django_db
def test_the_limit_is_respected(tmp_path: Path) -> None:
    records = [record(code=f"000000000000{i}") for i in range(5)]
    run_import(write_dump(tmp_path, *records), limit=2, batch_size=1)
    assert Product.objects.count() == 2


# ── What seeding is worth ─────────────────────────────────────────────────────


@pytest.mark.django_db
def test_seeding_turns_no_match_into_a_candidate(tmp_path: Path) -> None:
    """The actual point: fragmentation avoided, not Tier 0 hits.

    Against an empty catalogue every first purchase creates a new record. With a
    seeded one there is something to match against, so the player gets a
    prompt to confirm instead of a duplicate nobody asked for.
    """
    from spendium import matching

    assert matching.match_line_item("HEINZ KETCHUP", "Heinz Ketchup").candidates == []
    run_import(write_dump(tmp_path, record()))
    assert matching.match_line_item("HEINZ KETCHUP", "Heinz Ketchup").candidates
