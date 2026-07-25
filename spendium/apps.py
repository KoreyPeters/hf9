from django.apps import AppConfig


class SpendiumConfig(AppConfig):
    name = "spendium"

    def ready(self) -> None:
        from . import signals  # noqa: F401  (registers FTS index sync)

        # Registers the @task handlers. Importing hf.task_urls would do it too,
        # but only once URLs are loaded — so anything calling enqueue() outside
        # a request, tests included, would not find them.
        from . import task_views  # noqa: F401
