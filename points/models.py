from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models


class PointTransaction(models.Model):
    player = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="point_transactions",
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    reason = models.CharField(max_length=100)
    description = models.CharField(
        max_length=200,
        blank=True,
        help_text="Human-readable context, kept because the ledger outlives "
        "what it refers to. A Spendium purchase is anonymised after thirty "
        "days, so without this the player's history reads as an unexplained "
        "row of numbers.",
    )
    content_type = models.ForeignKey(ContentType, on_delete=models.SET_NULL, null=True)
    object_id = models.PositiveIntegerField(null=True)
    source = GenericForeignKey("content_type", "object_id")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["player", "created_at"])]

    def __str__(self) -> str:
        return f"{self.player} +{self.amount} ({self.reason})"
