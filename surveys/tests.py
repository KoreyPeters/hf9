import pytest
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from accounts.models import Player
from surveys.models import Category, Criterion, CriterionAnswer, SurveyResponse
from surveys.ratings import compute_rating


@pytest.fixture
def polium_category(db: None) -> Category:
    return Category.objects.create(name="Test Category", description="", game="polium")


@pytest.fixture
def active_criterion(db: None, polium_category: Category) -> Criterion:
    return Criterion.objects.create(category=polium_category, question="Q?", weight=1.0)


def make_response(player: Player, subject: Player) -> SurveyResponse:
    ct = ContentType.objects.get_for_model(subject)
    return SurveyResponse.objects.create(player=player, content_type=ct, object_id=subject.pk)


@pytest.mark.django_db
def test_returns_none_with_no_responses(player: Player) -> None:
    assert compute_rating(player) is None


@pytest.mark.django_db
def test_correct_weighted_average(player: Player, polium_category: Category) -> None:
    crit_heavy = Criterion.objects.create(category=polium_category, question="Q1?", weight=2.0)
    crit_light = Criterion.objects.create(category=polium_category, question="Q2?", weight=1.0)
    response = make_response(player, player)
    CriterionAnswer.objects.create(survey_response=response, criterion=crit_heavy, answer=True)
    CriterionAnswer.objects.create(survey_response=response, criterion=crit_light, answer=False)
    result = compute_rating(player)
    assert result == pytest.approx(2.0 / 3.0)


@pytest.mark.django_db
def test_excludes_responses_older_than_365_days(
    player: Player, active_criterion: Criterion
) -> None:
    response = make_response(player, player)
    CriterionAnswer.objects.create(survey_response=response, criterion=active_criterion, answer=True)
    SurveyResponse.objects.filter(pk=response.pk).update(
        submitted_at=timezone.now() - timezone.timedelta(days=366)
    )
    assert compute_rating(player) is None


@pytest.mark.django_db
def test_excludes_inactive_criteria(player: Player, polium_category: Category) -> None:
    inactive = Criterion.objects.create(
        category=polium_category, question="Inactive?", weight=1.0, is_active=False
    )
    response = make_response(player, player)
    CriterionAnswer.objects.create(survey_response=response, criterion=inactive, answer=True)
    assert compute_rating(player) is None


@pytest.mark.django_db
def test_returns_none_when_total_weight_is_zero(
    player: Player, polium_category: Category
) -> None:
    zero_weight = Criterion.objects.create(
        category=polium_category, question="Zero?", weight=0.0
    )
    response = make_response(player, player)
    CriterionAnswer.objects.create(survey_response=response, criterion=zero_weight, answer=True)
    assert compute_rating(player) is None
