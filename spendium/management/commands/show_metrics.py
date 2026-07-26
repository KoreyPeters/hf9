"""Print the convergence numbers. For an ad-hoc look without the admin."""

from typing import Any

from django.core.management.base import BaseCommand

from spendium import metrics


class Command(BaseCommand):
    help = "Show Spendium matching and catalogue metrics."

    def handle(self, *args: Any, **options: Any) -> None:
        summary = metrics.summary()
        tiers = summary.pop("tier_distribution")

        self.stdout.write(self.style.MIGRATE_HEADING("Matching tiers"))
        total = sum(tiers.values()) or 1
        for tier, count in tiers.items():
            self.stdout.write(f"  {tier:<12} {count:>8}  ({count / total:.1%})")

        self.stdout.write(self.style.MIGRATE_HEADING("Rates"))
        for name, value in summary.items():
            shown = (
                "—"
                if value is None
                else (f"{value:.1%}" if isinstance(value, float) else str(value))
            )
            self.stdout.write(f"  {name:<24} {shown}")
