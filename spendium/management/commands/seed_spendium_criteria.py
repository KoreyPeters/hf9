"""Founder-set opening criteria for product ratings.

These questions are a bootstrapping concession, not a design feature. Members
determining the criteria is the foundational point of HF, and until that
governance exists somebody has to write the first set — so the category is
marked provisional and says so to players.

What the questions *are* is a content decision, not an engineering one. They are
placeholders, and the engine is built to receive whatever the membership later
decides rather than to have any particular set baked in.
"""

from typing import Any

from django.core.management.base import BaseCommand

from surveys.models import Category, Criterion

CATEGORY_NAME = "Product ethics"
CATEGORY_DESCRIPTION = (
    "Opening questions about the ethics of a product and the company behind it. "
    "Set by the founder to get started, and intended to be replaced by criteria "
    "the membership decides."
)

# (question, weight)
OPENING_CRITERIA = [
    ("Does the manufacturer treat its workers fairly?", 100),
    ("Is this product made without avoidable environmental harm?", 100),
    ("Is the company honest about what this product contains?", 75),
    ("Does the company avoid lobbying against public-interest regulation?", 75),
    ("Is the packaging reasonable for what is inside it?", 25),
]


class Command(BaseCommand):
    help = "Create or update the founder-set opening criteria for product ratings."

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
        from django.contrib.contenttypes.models import ContentType

        from spendium.models import Product

        # Scoped to Product explicitly. Spendium now has a second rateable
        # subject, and an unscoped category would be offered for stores too.
        product_type = ContentType.objects.get_for_model(Product)
        category, created = Category.objects.get_or_create(
            game="spendium",
            name=CATEGORY_NAME,
            defaults={
                "description": CATEGORY_DESCRIPTION,
                "criteria_are_provisional": True,
                "subject_type": product_type,
            },
        )
        # Set on an existing row too: a database seeded before subject types
        # existed has this null, which would silently serve product questions
        # on store pages.
        if category.subject_type_id != product_type.pk:
            category.subject_type = product_type
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
