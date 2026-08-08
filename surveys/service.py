from datetime import timedelta
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Model, Q
from django.utils import timezone

from points.service import award_points

from .models import Category, Criterion, CriterionAnswer, SurveyConfig, SurveyResponse


def categories_for(subject: Model, game: str) -> list[Category]:
    """Question sets that apply to this subject.

    A category scoped to this subject's type applies to it. A category with no
    subject type applies to everything in the game — which is what Polium has
    always done, and is now stated rather than implied.

    Replaces two different improvisations. Polium read every active criterion in
    the game; Spendium took `Category.objects.filter(game=…).order_by("pk").first()`
    and hoped. Both were correct only while each game had exactly one rateable
    subject, and Spendium is about to have two: a store category with a lower
    primary key than the product one would have served product questions on
    store pages, silently and with no error anywhere.
    """
    ct = ContentType.objects.get_for_model(subject)
    return list(
        Category.objects.filter(game=game)
        .filter(Q(subject_type=ct) | Q(subject_type__isnull=True))
        .order_by("pk")
    )


def criteria_for(subject: Model, game: str) -> list[Criterion]:
    """Active questions to ask about this subject, in a stable order."""
    return list(
        Criterion.objects.filter(
            category__in=categories_for(subject, game), is_active=True
        )
        .select_related("category")
        .order_by("category__pk", "pk")
    )


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
    is_verified: bool = False,
) -> tuple[SurveyResponse, Decimal]:
    """Record a survey and award its points.

    `is_verified` defaults to the value Polium has always behaved as though it
    used, so its callers are unaffected.

    The criteria version is no longer a parameter. It is read per answer from
    the criterion's own category, which is the only place it is well defined: a
    subject may be asked several categories' questions and each keeps its own
    counter, so a single number supplied by the caller could only ever describe
    one of them. Nothing aggregates on it yet — that is item 9 in
    plans/operational-debt.md — but what is recorded is now unambiguous.
    """
    ct = ContentType.objects.get_for_model(subject)
    existing = _get_existing(player, ct, subject.pk)

    remaining = check_cooldown(player, subject)
    if remaining is not None:
        raise CoolDownError(remaining)

    config = SurveyConfig.get()

    if existing is not None:
        new_count = existing.submit_count + 1
        existing.answers.all().delete()
        existing.submitted_at = timezone.now()
        existing.submit_count = new_count
        existing.is_verified = is_verified
        existing.save(
            update_fields=[
                "submitted_at",
                "submit_count",
                "is_verified",
            ]
        )
        response = existing
    else:
        new_count = 1
        response = SurveyResponse.objects.create(
            player=player,
            content_type=ct,
            object_id=subject.pk,
            is_verified=is_verified,
        )

    # One query for the versions rather than one per answer: each answer records
    # the version of its own criterion's category, which is what makes the field
    # meaningful when two categories apply at once.
    versions = dict(
        Criterion.objects.filter(pk__in=answers).values_list(
            "pk", "category__criteria_version"
        )
    )
    CriterionAnswer.objects.bulk_create(
        [
            CriterionAnswer(
                survey_response=response,
                criterion_id=cid,
                answer=val,
                criteria_version=versions.get(cid, 1),
            )
            for cid, val in answers.items()
        ]
    )

    if new_count == 1:
        amount = config.survey_points_first
    elif new_count == 2:
        amount = config.survey_points_second
    else:
        amount = config.survey_points_subsequent

    points = award_points(player, amount, "survey", source=response)
    return response, points
