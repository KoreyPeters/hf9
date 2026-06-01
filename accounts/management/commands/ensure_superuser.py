import os

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    def handle(self, *args, **options):
        email = os.environ.get("DJANGO_SUPERUSER_EMAIL")
        password = os.environ.get("DJANGO_SUPERUSER_PASSWORD")
        if not email or not password:
            return

        from accounts.models import Player

        if Player.objects.filter(is_superuser=True).exists():
            return

        Player.objects.create_superuser(username=email, email=email, password=password)
        self.stdout.write(f"Superuser created: {email}")
