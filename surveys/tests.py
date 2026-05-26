from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from accounts.models import Player
from points.models import PointTransaction
from surveys.models import Category, Criterion, CriterionAnswer, SurveyConfig, SurveyResponse
from surveys.ratings import compute_rating
from surveys.service import CoolDownError, check_cooldown, submit_survey


@pytest.fixture
def polium_category(db: None) -> Category:
    return Category.objects.create(name="Test Category", description="", game="polium")


@pytest.fixture
def active_criterion(db: None, polium_category: Category) -> Criterion:
    return Criterion.objects.create(category=polium_category, question="Q?", weight=1.0)


@pytest.fixture
def survey_config(db: None) -> SurveyConfig:
    return SurveyConfig.objects.create(pk=1, cooldown_days=30)


@pytest.fixture
def criterion(db: None, polium_category: Category) -> Criterion:
    return Criterion.objects.create(category=polium_category, question="Does X?", weight=1.0)


@pytest.fixture
def candidate(db: None):
    from polium.models import Candidate, Jurisdiction
    jurisdiction = Jurisdiction.objects.create(name="Test Jurisdiction", level="federal")
    return Candidate.objects.create(
        name="Test Candidate", jurisdiction=jurisdiction, office="Senator"
    )


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


# §15.6 — cool-down tests


@pytest.mark.django_db
def test_first_survey_no_cooldown(player: Player, candidate, survey_config: SurveyConfig) -> None:
    assert check_cooldown(player, candidate) is None


@pytest.mark.django_db
def test_submit_creates_response(
    player: Player, candidate, criterion: Criterion, survey_config: SurveyConfig
) -> None:
    response = submit_survey(player, candidate, {criterion.pk: True})
    assert SurveyResponse.objects.filter(player=player).count() == 1
    assert response.answers.filter(criterion=criterion, answer=True).exists()


@pytest.mark.django_db
def test_cooldown_blocks_immediate_resubmit(
    player: Player, candidate, criterion: Criterion, survey_config: SurveyConfig
) -> None:
    submit_survey(player, candidate, {criterion.pk: True})
    with pytest.raises(CoolDownError) as exc_info:
        submit_survey(player, candidate, {criterion.pk: False})
    assert exc_info.value.remaining.days >= 29


@pytest.mark.django_db
def test_cooldown_allows_resubmit_after_expiry(
    player: Player, candidate, criterion: Criterion, survey_config: SurveyConfig
) -> None:
    submit_survey(player, candidate, {criterion.pk: True})
    SurveyResponse.objects.filter(player=player).update(
        submitted_at=timezone.now() - timedelta(days=31)
    )
    response = submit_survey(player, candidate, {criterion.pk: False})
    assert response.answers.filter(answer=False).exists()


@pytest.mark.django_db
def test_resubmit_replaces_answers(
    player: Player, candidate, criterion: Criterion, survey_config: SurveyConfig
) -> None:
    submit_survey(player, candidate, {criterion.pk: True})
    SurveyResponse.objects.filter(player=player).update(
        submitted_at=timezone.now() - timedelta(days=31)
    )
    submit_survey(player, candidate, {criterion.pk: False})
    assert SurveyResponse.objects.filter(player=player).count() == 1
    assert CriterionAnswer.objects.filter(survey_response__player=player).count() == 1
    assert CriterionAnswer.objects.get(survey_response__player=player).answer is False


@pytest.mark.django_db
def test_resubmit_updates_submitted_at(
    player: Player, candidate, criterion: Criterion, survey_config: SurveyConfig
) -> None:
    submit_survey(player, candidate, {criterion.pk: True})
    SurveyResponse.objects.filter(player=player).update(
        submitted_at=timezone.now() - timedelta(days=31)
    )
    before = timezone.now()
    submit_survey(player, candidate, {criterion.pk: False})
    r = SurveyResponse.objects.get(player=player)
    assert r.submitted_at >= before


@pytest.mark.django_db
def test_resubmit_preserves_created_at(
    player: Player, candidate, criterion: Criterion, survey_config: SurveyConfig
) -> None:
    submit_survey(player, candidate, {criterion.pk: True})
    r = SurveyResponse.objects.get(player=player)
    original_created_at = r.created_at
    SurveyResponse.objects.filter(player=player).update(
        submitted_at=timezone.now() - timedelta(days=31)
    )
    submit_survey(player, candidate, {criterion.pk: False})
    r.refresh_from_db()
    assert r.created_at == original_created_at


@pytest.mark.django_db
def test_cooldown_respects_config_value(
    player: Player, candidate, criterion: Criterion, db: None
) -> None:
    SurveyConfig.objects.create(pk=1, cooldown_days=7)
    submit_survey(player, candidate, {criterion.pk: True})
    SurveyResponse.objects.filter(player=player).update(
        submitted_at=timezone.now() - timedelta(days=8)
    )
    response = submit_survey(player, candidate, {criterion.pk: False})
    assert response is not None


# ── Survey points ─────────────────────────────────────────────────────────────

@pytest.mark.django_db
def test_first_survey_awards_100_points(
    player: Player, candidate, criterion: Criterion, survey_config: SurveyConfig
) -> None:
    submit_survey(player, candidate, {criterion.pk: True})
    tx = PointTransaction.objects.get(player=player)
    assert tx.amount == Decimal(str(survey_config.survey_points_first))
    assert tx.reason == "survey"


@pytest.mark.django_db
def test_second_survey_awards_50_points(
    player: Player, candidate, criterion: Criterion, survey_config: SurveyConfig
) -> None:
    submit_survey(player, candidate, {criterion.pk: True})
    SurveyResponse.objects.filter(player=player).update(
        submitted_at=timezone.now() - timedelta(days=31)
    )
    submit_survey(player, candidate, {criterion.pk: False})
    amounts = list(
        PointTransaction.objects.filter(player=player).order_by("created_at").values_list("amount", flat=True)
    )
    assert amounts[0] == Decimal(str(survey_config.survey_points_first))
    assert amounts[1] == Decimal(str(survey_config.survey_points_second))


@pytest.mark.django_db
def test_third_survey_awards_25_points(
    player: Player, candidate, criterion: Criterion, survey_config: SurveyConfig
) -> None:
    submit_survey(player, candidate, {criterion.pk: True})
    SurveyResponse.objects.filter(player=player).update(
        submitted_at=timezone.now() - timedelta(days=31)
    )
    submit_survey(player, candidate, {criterion.pk: False})
    SurveyResponse.objects.filter(player=player).update(
        submitted_at=timezone.now() - timedelta(days=31)
    )
    submit_survey(player, candidate, {criterion.pk: True})
    amounts = list(
        PointTransaction.objects.filter(player=player).order_by("created_at").values_list("amount", flat=True)
    )
    assert amounts[2] == Decimal(str(survey_config.survey_points_subsequent))


@pytest.mark.django_db
def test_subsequent_surveys_all_award_25_points(
    player: Player, candidate, criterion: Criterion, survey_config: SurveyConfig
) -> None:
    for _ in range(5):
        submit_survey(player, candidate, {criterion.pk: True})
        SurveyResponse.objects.filter(player=player).update(
            submitted_at=timezone.now() - timedelta(days=31)
        )
    amounts = list(
        PointTransaction.objects.filter(player=player).order_by("created_at").values_list("amount", flat=True)
    )
    assert all(a == Decimal(str(survey_config.survey_points_subsequent)) for a in amounts[2:])


@pytest.mark.django_db
def test_submit_count_increments_on_resubmit(
    player: Player, candidate, criterion: Criterion, survey_config: SurveyConfig
) -> None:
    submit_survey(player, candidate, {criterion.pk: True})
    r = SurveyResponse.objects.get(player=player)
    assert r.submit_count == 1

    SurveyResponse.objects.filter(player=player).update(
        submitted_at=timezone.now() - timedelta(days=31)
    )
    submit_survey(player, candidate, {criterion.pk: False})
    r.refresh_from_db()
    assert r.submit_count == 2


@pytest.mark.django_db
def test_points_not_awarded_to_unverified_player(
    candidate, criterion: Criterion, survey_config: SurveyConfig, db: None
) -> None:
    unverified = Player.objects.create_user(
        username="unverified", email="unverified@example.com", password=None
    )
    assert not unverified.email_verified
    submit_survey(unverified, candidate, {criterion.pk: True})
    assert PointTransaction.objects.filter(player=unverified).count() == 0


@pytest.mark.django_db
def test_points_source_is_survey_response(
    player: Player, candidate, criterion: Criterion, survey_config: SurveyConfig
) -> None:
    response = submit_survey(player, candidate, {criterion.pk: True})
    tx = PointTransaction.objects.get(player=player)
    assert tx.object_id == response.pk
    assert tx.content_type == ContentType.objects.get_for_model(SurveyResponse)


@pytest.mark.django_db
def test_cooldown_error_does_not_award_points(
    player: Player, candidate, criterion: Criterion, survey_config: SurveyConfig
) -> None:
    submit_survey(player, candidate, {criterion.pk: True})
    with pytest.raises(CoolDownError):
        submit_survey(player, candidate, {criterion.pk: False})
    assert PointTransaction.objects.filter(player=player).count() == 1


@pytest.mark.django_db
def test_config_values_used_for_points(
    player: Player, candidate, criterion: Criterion, db: None
) -> None:
    SurveyConfig.objects.create(pk=1, cooldown_days=0, survey_points_first=200)
    submit_survey(player, candidate, {criterion.pk: True})
    tx = PointTransaction.objects.get(player=player)
    assert tx.amount == Decimal("200")
