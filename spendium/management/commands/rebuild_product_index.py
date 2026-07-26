from typing import Any

from django.core.management.base import BaseCommand

from spendium import search


class Command(BaseCommand):
    help = (
        "Rebuild the FTS5 product narrowing index from scratch. "
        "Needed after a bulk catalogue import, which bypasses per-row signals."
    )

    def handle(self, *args: Any, **options: Any) -> None:
        count = search.rebuild()
        self.stdout.write(self.style.SUCCESS(f"Indexed {count} product(s)."))
