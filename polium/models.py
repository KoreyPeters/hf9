from django.conf import settings
from django.db import models
from sqids import Sqids

from core.models import SqidMixin
from lifecycle.models import LifecycleMixin


class Jurisdiction(SqidMixin, LifecycleMixin):
    name = models.CharField(max_length=300)
    level = models.CharField(max_length=100)
    parent = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="children"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="jurisdictions_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def generate_sqid(self) -> str:
        return Sqids(alphabet=settings.SQID_SALTS["jurisdiction"]).encode([self.pk])

    @property
    def flag_count(self) -> int:
        return self.duplicate_flags.count()

    def _winning_jurisdiction(self) -> "Jurisdiction | None":
        flag = self.duplicate_flags.order_by("-created_at").first()
        return flag.points_to if flag else None

    def delete(self, *args: object, **kwargs: object) -> tuple[int, dict[str, int]]:
        winning = self._winning_jurisdiction()
        if winning:
            self.children.all().update(parent=winning)
            for follow in self.followers.all():
                follow.jurisdiction = winning
                follow.save()
        return super().delete(*args, **kwargs)

    class Meta:
        indexes = [
            models.Index(fields=["status", "active_engagement"]),
            models.Index(fields=["parent", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.level})"


class JurisdictionDuplicateFlag(models.Model):
    flagging_player = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="jurisdiction_flags",
    )
    flagged_jurisdiction = models.ForeignKey(
        Jurisdiction, on_delete=models.CASCADE, related_name="duplicate_flags"
    )
    points_to = models.ForeignKey(
        Jurisdiction, on_delete=models.CASCADE, related_name="flagged_as_duplicate_of"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [["flagging_player", "flagged_jurisdiction"]]


class JurisdictionFollow(models.Model):
    DEPTH_THIS = "this"
    DEPTH_ALL = "all"
    DEPTH_CHOICES = [
        (DEPTH_THIS, "This level only"),
        (DEPTH_ALL, "This level and below"),
    ]

    player = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="followed_jurisdictions",
    )
    jurisdiction = models.ForeignKey(
        Jurisdiction, on_delete=models.CASCADE, related_name="followers"
    )
    depth = models.CharField(max_length=10, choices=DEPTH_CHOICES, default=DEPTH_ALL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [["player", "jurisdiction"]]


class Election(SqidMixin):
    name = models.CharField(max_length=300)
    jurisdiction = models.ForeignKey(
        Jurisdiction, on_delete=models.SET_NULL, null=True, related_name="elections"
    )
    election_date = models.DateField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="elections_created",
    )
    external_reference = models.URLField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def generate_sqid(self) -> str:
        return Sqids(alphabet=settings.SQID_SALTS["election"]).encode([self.pk])

    class Meta:
        indexes = [models.Index(fields=["jurisdiction", "election_date"])]

    def __str__(self) -> str:
        return self.name


class Candidate(SqidMixin):
    name = models.CharField(max_length=300)
    jurisdiction = models.ForeignKey(
        Jurisdiction, on_delete=models.SET_NULL, null=True, related_name="candidates"
    )
    office = models.CharField(max_length=200)
    election = models.ForeignKey(
        Election, on_delete=models.SET_NULL, null=True, blank=True, related_name="candidates"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="candidates_created",
    )
    external_reference = models.URLField(blank=True)
    bio = models.TextField(blank=True)
    current_rating = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    is_blacklisted = models.BooleanField(default=False)
    blacklisted_at = models.DateTimeField(null=True, blank=True)
    engagement_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    duplicates = models.ManyToManyField("self", blank=True, symmetrical=True)

    def generate_sqid(self) -> str:
        return Sqids(alphabet=settings.SQID_SALTS["candidate"]).encode([self.pk])

    class Meta:
        indexes = [
            models.Index(fields=["jurisdiction", "current_rating"]),
            models.Index(fields=["engagement_count"]),
            models.Index(fields=["is_blacklisted"]),
        ]

    def __str__(self) -> str:
        return self.name


class OfficeHistory(models.Model):
    candidate = models.ForeignKey(
        Candidate, on_delete=models.CASCADE, related_name="office_history"
    )
    office = models.CharField(max_length=300)
    jurisdiction = models.CharField(max_length=300)
    started_at = models.DateField()
    ended_at = models.DateField(null=True, blank=True)
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="office_history_entries",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-started_at"]
        verbose_name_plural = "office histories"


class BlacklistHistory(models.Model):
    candidate = models.ForeignKey(
        Candidate, on_delete=models.CASCADE, related_name="blacklist_history"
    )
    blacklisted_at = models.DateTimeField()
    lifted_at = models.DateTimeField(null=True, blank=True)
    rating_at_blacklist = models.DecimalField(max_digits=5, decimal_places=2)
    rating_at_lift = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)


class VoteDeclaration(models.Model):
    player = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="vote_declarations",
    )
    candidate = models.ForeignKey(
        Candidate, on_delete=models.CASCADE, related_name="vote_declarations"
    )
    election = models.ForeignKey(
        Election, on_delete=models.CASCADE, related_name="vote_declarations"
    )
    shared_on_social = models.BooleanField(default=False)
    shared_at = models.DateTimeField(null=True, blank=True)
    declared_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [["player", "election"]]
