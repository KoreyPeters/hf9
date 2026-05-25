from django.db import models


class SpendiumWaitlist(models.Model):
    email = models.EmailField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Spendium waitlist entry"
        verbose_name_plural = "Spendium waitlist entries"

    def __str__(self) -> str:
        return self.email
