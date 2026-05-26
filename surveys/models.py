from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone


class Category(models.Model):
    name = models.CharField(max_length=200)
    description = models.TextField()
    game = models.CharField(max_length=50)

    class Meta:
        verbose_name_plural = "categories"

    def __str__(self) -> str:
        return f"{self.game} / {self.name}"


class Criterion(models.Model):
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="criteria")
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

    class Meta:
        indexes = [models.Index(fields=["content_type", "object_id"])]


class CriterionAnswer(models.Model):
    survey_response = models.ForeignKey(
        SurveyResponse, on_delete=models.CASCADE, related_name="answers"
    )
    criterion = models.ForeignKey(Criterion, on_delete=models.PROTECT, related_name="answers")
    answer = models.BooleanField()


class SurveyConfig(models.Model):
    cooldown_days = models.PositiveIntegerField(
        default=30,
        help_text="Minimum days a player must wait before re-surveying a subject.",
    )
    survey_points_first = models.PositiveIntegerField(default=100)
    survey_points_second = models.PositiveIntegerField(default=50)
    survey_points_subsequent = models.PositiveIntegerField(default=25)

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
