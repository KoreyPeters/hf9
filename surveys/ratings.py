from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.db.models import Model
from django.utils import timezone

from .models import CriterionAnswer, SurveyResponse


def compute_rating(subject: Model) -> float | None:
    cutoff = timezone.now() - timedelta(days=365)
    ct = ContentType.objects.get_for_model(subject)

    responses = SurveyResponse.objects.filter(
        content_type=ct,
        object_id=subject.pk,
        submitted_at__gte=cutoff,
    )
    if not responses.exists():
        return None

    answers = CriterionAnswer.objects.filter(
        survey_response__in=responses,
        criterion__is_active=True,
    ).select_related("criterion")

    total_weight = 0.0
    weighted_sum = 0.0
    for answer in answers:
        w = float(answer.criterion.weight)
        total_weight += w
        weighted_sum += w * (1.0 if answer.answer else 0.0)

    if total_weight == 0:
        return None
    return weighted_sum / total_weight
