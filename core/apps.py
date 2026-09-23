from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "core"

    def ready(self) -> None:
        # Registers the @task handlers, same reasoning as spendium/apps.py:
        # importing hf.task_urls would do it too, but only once URLs are loaded,
        # so anything calling enqueue() outside a request would not find them.
        from . import task_views  # noqa: F401
