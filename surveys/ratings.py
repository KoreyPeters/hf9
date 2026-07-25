from datetime import timedelta
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.db.models import Count, Model
from django.utils import timezone

from .models import Criterion, CriterionAnswer, SurveyConfig, SurveyResponse


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


def compute_declaration_points(subject: Model) -> Decimal:
    """Σ (criterion weight × yes_probability) for criteria with ≥ k survey responses."""
    cutoff = timezone.now() - timedelta(days=365)
    ct = ContentType.objects.get_for_model(subject)

    responses = SurveyResponse.objects.filter(
        content_type=ct,
        object_id=subject.pk,
        submitted_at__gte=cutoff,
    )
    if not responses.exists():
        return Decimal("0")

    rows = (
        CriterionAnswer.objects
        .filter(survey_response__in=responses, criterion__is_active=True)
        .values("criterion_id", "answer")
        .annotate(n=Count("pk"))
    )

    yes_counts: dict[int, int] = {}
    total_counts: dict[int, int] = {}
    for row in rows:
        cid = row["criterion_id"]
        total_counts[cid] = total_counts.get(cid, 0) + row["n"]
        if row["answer"]:
            yes_counts[cid] = yes_counts.get(cid, 0) + row["n"]

    k = SurveyConfig.get().min_survey_threshold
    eligible = [cid for cid, n in total_counts.items() if n >= k]
    if not eligible:
        return Decimal("0")

    weights: dict[int, Decimal] = {
        c.pk: c.weight
        for c in Criterion.objects.filter(pk__in=eligible, is_active=True)
    }

    total = Decimal("0")
    for cid in eligible:
        if cid not in weights:
            continue
        prob = Decimal(yes_counts.get(cid, 0)) / Decimal(total_counts[cid])
        total += weights[cid] * prob

    return total.quantize(Decimal("0.01"))
