from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models


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
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["content_type", "object_id"])]


class CriterionAnswer(models.Model):
    survey_response = models.ForeignKey(
        SurveyResponse, on_delete=models.CASCADE, related_name="answers"
    )
    criterion = models.ForeignKey(Criterion, on_delete=models.PROTECT, related_name="answers")
    answer = models.BooleanField()
