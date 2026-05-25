from django.core.management.base import BaseCommand

from surveys.models import Category, Criterion

INITIAL_CRITERIA: list[dict] = [
    {
        "category": "Climate and Environment",
        "game": "polium",
        "criteria": [
            ("Has the candidate voted consistently to reduce carbon emissions?", 2.0),
            ("Has the candidate opposed subsidies for fossil fuel industries?", 1.5),
        ],
    },
]


class Command(BaseCommand):
    help = "Seed initial survey criteria for Polium"

    def handle(self, *args: object, **options: object) -> None:
        for block in INITIAL_CRITERIA:
            cat, _ = Category.objects.get_or_create(
                name=block["category"],
                game=block["game"],
                defaults={"description": ""},
            )
            for question, weight in block["criteria"]:
                Criterion.objects.get_or_create(
                    category=cat,
                    question=question,
                    defaults={"weight": weight},
                )
        self.stdout.write(self.style.SUCCESS("Criteria seeded."))
