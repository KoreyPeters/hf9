from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models

from surveys.models import Criterion


class Evidence(models.Model):
    STATUS_VISIBLE = "visible"
    STATUS_HIDDEN = "hidden"
    STATUS_REMOVED = "removed"
    STATUS_CHOICES = [
        (STATUS_VISIBLE, "Visible"),
        (STATUS_HIDDEN, "Hidden"),
        (STATUS_REMOVED, "Removed"),
    ]

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    subject = GenericForeignKey("content_type", "object_id")
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="evidence_submitted",
    )
    url = models.URLField()
    note = models.TextField()
    criterion = models.ForeignKey(
        Criterion,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="evidence",
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_VISIBLE)
    net_usefulness_score = models.IntegerField(default=0)
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["content_type", "object_id", "status", "net_usefulness_score"])
        ]

    def __str__(self) -> str:
        return f"{self.url} ({self.status})"


class EvidenceUsefulness(models.Model):
    player = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="evidence_usefulness_votes",
    )
    evidence = models.ForeignKey(Evidence, on_delete=models.CASCADE, related_name="usefulness_votes")
    is_useful = models.BooleanField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [["player", "evidence"]]


class EvidenceFlag(models.Model):
    REASON_IRRELEVANT = "irrelevant"
    REASON_LOW_QUALITY = "low_quality"
    REASON_MISLEADING = "misleading"
    REASON_MALICIOUS = "malicious"
    REASON_CHOICES = [
        (REASON_IRRELEVANT, "Irrelevant"),
        (REASON_LOW_QUALITY, "Low Quality"),
        (REASON_MISLEADING, "Misleading"),
        (REASON_MALICIOUS, "Malicious"),
    ]

    flagging_player = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="evidence_flags",
    )
    evidence = models.ForeignKey(Evidence, on_delete=models.CASCADE, related_name="flags")
    reason = models.CharField(max_length=20, choices=REASON_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [["flagging_player", "evidence"]]
