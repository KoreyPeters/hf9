from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Model
from django.utils import timezone

from .models import CriterionAnswer, SurveyConfig, SurveyResponse


class CoolDownError(Exception):
    def __init__(self, remaining: timedelta) -> None:
        self.remaining = remaining
        super().__init__(f"Cool-down active: {remaining.days} days remaining.")


def _get_existing(
    player: object,
    content_type: ContentType,
    object_id: int,
) -> SurveyResponse | None:
    return (
        SurveyResponse.objects.filter(
            player=player,
            content_type=content_type,
            object_id=object_id,
        )
        .order_by("-submitted_at")
        .first()
    )


def check_cooldown(player: object, subject: Model) -> timedelta | None:
    """Return remaining cool-down timedelta, or None if the player may submit now."""
    ct = ContentType.objects.get_for_model(subject)
    existing = _get_existing(player, ct, subject.pk)
    if existing is None:
        return None
    config = SurveyConfig.get()
    cooldown = timedelta(days=config.cooldown_days)
    elapsed = timezone.now() - existing.submitted_at
    if elapsed < cooldown:
        return cooldown - elapsed
    return None


@transaction.atomic
def submit_survey(
    player: object,
    subject: Model,
    answers: dict[int, bool],
) -> SurveyResponse:
    """
    Create or replace the player's survey response for subject.

    answers maps criterion PKs to boolean responses.
    Raises CoolDownError if the player is within the cool-down window.
    """
    ct = ContentType.objects.get_for_model(subject)
    existing = _get_existing(player, ct, subject.pk)

    remaining = check_cooldown(player, subject)
    if remaining is not None:
        raise CoolDownError(remaining)

    if existing is not None:
        existing.answers.all().delete()
        existing.submitted_at = timezone.now()
        existing.save(update_fields=["submitted_at"])
        response = existing
    else:
        response = SurveyResponse.objects.create(
            player=player,
            content_type=ct,
            object_id=subject.pk,
        )

    CriterionAnswer.objects.bulk_create([
        CriterionAnswer(survey_response=response, criterion_id=cid, answer=val)
        for cid, val in answers.items()
    ])
    return response
