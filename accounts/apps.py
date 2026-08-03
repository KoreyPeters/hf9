from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "accounts"

    def ready(self) -> None:
        import accounts.signals  # noqa: F401

        # Imported for its side effect: registering the system check that stops
        # a production deploy with no Turnstile keys. Without this import the
        # check is never registered and the guard silently does not exist.
        import accounts.turnstile  # noqa: F401
