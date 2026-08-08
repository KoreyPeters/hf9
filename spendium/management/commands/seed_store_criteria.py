"""Founder-set opening criteria for store ratings.

Same bootstrapping concession as `seed_spendium_criteria`, and the same
disclaimer: members deciding the criteria is the foundational point of HF, so
this category is marked provisional and says so to players.

The five questions below are placeholders and are the weakest part of the plan
they came from. They are meant to be overwritten. What is not a placeholder is
the scoping — the category is bound to `Store`, so these are never offered on a
product page and product questions are never offered here.

Weights total 350 points per dollar for a store satisfying everything, on top of
whatever the basket itself earns. That magnitude is deliberate and is the
membership's to set rather than engineering's.
"""

from typing import Any

from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand

from spendium.models import Store
from surveys.models import Category, Criterion

CATEGORY_NAME = "Store ethics"
CATEGORY_DESCRIPTION = (
    "Opening questions about how a retailer behaves — toward its staff, its "
    "suppliers, and the public. Set by the founder to get started, and intended "
    "to be replaced by criteria the membership decides."
)

# (question, weight)
OPENING_CRITERIA = [
    ("Does this retailer pay its store staff a living wage?", 100),
    ("Does it treat its suppliers fairly on price and payment terms?", 75),
    ("Does it avoid lobbying against public-interest regulation?", 75),
    ("Does it take responsibility for waste from what it sells?", 50),
    ("Is it honest in its advertising and pricing?", 50),
]


class Command(BaseCommand):
    help = "Create or update the founder-set opening criteria for store ratings."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--bump-version",
            action="store_true",
            help=(
                "Record that the question set has changed. Use when editing "
                "criteria after ratings exist, so old and new answers stay "
                "distinguishable rather than being pooled."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        store_type = ContentType.objects.get_for_model(Store)
        category, created = Category.objects.get_or_create(
            game="spendium",
            name=CATEGORY_NAME,
            defaults={
                "description": CATEGORY_DESCRIPTION,
                "criteria_are_provisional": True,
                "subject_type": store_type,
            },
        )
        # Corrected on an existing row as well. A category left unscoped would
        # be offered for products too, which is the exact failure the subject
        # type exists to prevent.
        if category.subject_type_id != store_type.pk:
            category.subject_type = store_type
            category.save(update_fields=["subject_type"])

        added = 0
        for question, weight in OPENING_CRITERIA:
            _, made = Criterion.objects.get_or_create(
                category=category,
                question=question,
                defaults={"weight": weight, "is_active": True},
            )
            added += int(made)

        if options["bump_version"]:
            version = category.bump_criteria_version()
            self.stdout.write(f"Criteria version is now {version}.")

        self.stdout.write(
            self.style.SUCCESS(
                f"{'Created' if created else 'Updated'} '{CATEGORY_NAME}' "
                f"({added} new criteria, version {category.criteria_version}, "
                f"provisional={category.criteria_are_provisional})."
            )
        )
