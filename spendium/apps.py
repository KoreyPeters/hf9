from django.apps import AppConfig


class SpendiumConfig(AppConfig):
    name = "spendium"

    def ready(self) -> None:
        from . import signals  # noqa: F401  (registers FTS index sync)
