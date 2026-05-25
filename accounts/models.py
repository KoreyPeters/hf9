from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from sqids import Sqids

from core.models import SqidMixin


class Player(SqidMixin, AbstractUser):
    total_points = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    def generate_sqid(self) -> str:
        sqids = Sqids(alphabet=settings.SQID_SALTS["player"])
        return sqids.encode([self.pk])

    class Meta:
        indexes = [models.Index(fields=["total_points"])]

    def __str__(self) -> str:
        return self.username


class Membership(models.Model):
    player = models.OneToOneField(Player, on_delete=models.CASCADE, related_name="membership")
    started_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    is_active = models.BooleanField(default=True)

    def __str__(self) -> str:
        return f"{self.player} membership (expires {self.expires_at.date()})"
