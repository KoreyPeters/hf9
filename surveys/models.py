from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone


class Category(models.Model):
    name = models.CharField(max_length=200)
    description = models.TextField()
    game = models.CharField(max_length=50)
    criteria_version = models.PositiveIntegerField(
        default=1,
        help_text="Bumped whenever the question set changes. Responses record "
        "the version they were given under, so answers to different questions "
        "are never averaged together as though they were the same.",
    )
    criteria_are_provisional = models.BooleanField(
        default=True,
        help_text="True while the criteria are founder-set rather than decided "
        "by the membership. Shown to players, because members determining the "
        "criteria is the point of HF and a temporary exception should look like "
        "one.",
    )

    class Meta:
        verbose_name_plural = "categories"

    def bump_criteria_version(self) -> int:
        """Record that the question set has changed.

        Ratings under the old and new sets stay distinguishable rather than
        being silently pooled — a rating means "answers to these questions", and
        changing the questions changes what the number means.
        """
        self.criteria_version += 1
        self.save(update_fields=["criteria_version"])
        return self.criteria_version

    def __str__(self) -> str:
        return f"{self.game} / {self.name}"


class Criterion(models.Model):
    category = models.ForeignKey(
        Category, on_delete=models.PROTECT, related_name="criteria"
    )
    question = models.TextField()
    weight = models.DecimalField(max_digits=5, decimal_places=2, default=1.0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.question[:80]


class SurveyResponse(models.Model):
    player = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="survey_responses",
    )
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    subject = GenericForeignKey("content_type", "object_id")
    created_at = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(default=timezone.now)
    submit_count = models.PositiveIntegerField(default=1)
    is_verified = models.BooleanField(
        default=False,
        db_index=True,
        help_text="The response was anchored to evidence the platform could "
        "check — in Spendium, a receipt showing the player actually bought the "
        "product. Recorded as a flag rather than a link so the anchor survives "
        "the purchase being anonymised.",
    )
    criteria_version = models.PositiveIntegerField(
        default=1,
        help_text="The Category.criteria_version in force when this was answered.",
    )

    class Meta:
        indexes = [models.Index(fields=["content_type", "object_id"])]


class CriterionAnswer(models.Model):
    survey_response = models.ForeignKey(
        SurveyResponse, on_delete=models.CASCADE, related_name="answers"
    )
    criterion = models.ForeignKey(
        Criterion, on_delete=models.PROTECT, related_name="answers"
    )
    answer = models.BooleanField()


class SurveyConfig(models.Model):
    cooldown_days = models.PositiveIntegerField(
        default=30,
        help_text="Minimum days a player must wait before re-surveying a subject.",
    )
    survey_points_first = models.PositiveIntegerField(default=100)
    survey_points_second = models.PositiveIntegerField(default=50)
    survey_points_subsequent = models.PositiveIntegerField(default=25)
    min_survey_threshold = models.PositiveIntegerField(
        default=5,
        help_text="Minimum survey responses a criterion must have to count toward declaration points.",
    )

    class Meta:
        verbose_name = "Survey configuration"
        verbose_name_plural = "Survey configuration"

    def save(self, *args: object, **kwargs: object) -> None:
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls) -> "SurveyConfig":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
