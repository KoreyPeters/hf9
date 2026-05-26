from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from sqids import Sqids

from core.models import SqidMixin


class Player(SqidMixin, AbstractUser):
    email = models.EmailField(unique=True, verbose_name="email address")
    display_name = models.CharField(max_length=100, blank=True)
    email_verified = models.BooleanField(default=False)
    email_verified_at = models.DateTimeField(null=True, blank=True)
    jurisdiction_country = models.CharField(max_length=2, blank=True)
    jurisdiction_region = models.CharField(max_length=10, blank=True)
    total_points = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    def generate_sqid(self) -> str:
        sqids = Sqids(alphabet=settings.SQID_SALTS["player"])
        return sqids.encode([self.pk])

    class Meta:
        indexes = [models.Index(fields=["total_points"])]

    def __str__(self) -> str:
        return self.display_name or self.username


class Membership(models.Model):
    player = models.OneToOneField(Player, on_delete=models.CASCADE, related_name="membership")
    started_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    is_active = models.BooleanField(default=True)

    def __str__(self) -> str:
        return f"{self.player} membership (expires {self.expires_at.date()})"


class EmailVerification(models.Model):
    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name="email_verifications")
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["token_hash"])]


class PasskeyCredential(models.Model):
    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name="passkeys")
    credential_id = models.BinaryField(unique=True)
    public_key = models.BinaryField()
    sign_count = models.PositiveIntegerField(default=0)
    aaguid = models.CharField(max_length=36, blank=True)
    device_name = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
