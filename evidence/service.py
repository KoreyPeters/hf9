from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db.models import Model
from django.utils import timezone

from .models import Evidence, EvidenceFlag, EvidenceUsefulness

if TYPE_CHECKING:
    from accounts.models import Player
    from surveys.models import Criterion


class NotMatureError(Exception):
    pass


class AlreadyFlaggedError(Exception):
    pass


def recompute_usefulness_score(evidence: Evidence) -> None:
    useful = evidence.usefulness_votes.filter(is_useful=True).count()
    not_useful = evidence.usefulness_votes.filter(is_useful=False).count()
    Evidence.objects.filter(pk=evidence.pk).update(net_usefulness_score=useful - not_useful)


def recompute_evidence_status(evidence: Evidence) -> None:
    flag_count = evidence.flags.count()
    threshold = max(1, evidence.net_usefulness_score / 10)
    if flag_count >= threshold and evidence.status == Evidence.STATUS_VISIBLE:
        Evidence.objects.filter(pk=evidence.pk).update(status=Evidence.STATUS_HIDDEN)


def submit_evidence(
    player: Player,
    subject: Model,
    url: str,
    note: str,
    criterion: Criterion | None = None,
) -> Evidence:
    ct = ContentType.objects.get_for_model(subject)
    return Evidence.objects.create(
        content_type=ct,
        object_id=subject.pk,
        submitted_by=player,
        url=url,
        note=note,
        criterion=criterion,
    )


def vote_usefulness(player: Player, evidence: Evidence, is_useful: bool) -> EvidenceUsefulness:
    vote, _ = EvidenceUsefulness.objects.update_or_create(
        player=player,
        evidence=evidence,
        defaults={"is_useful": is_useful},
    )
    recompute_usefulness_score(evidence)
    evidence.refresh_from_db()
    recompute_evidence_status(evidence)
    return vote


def flag_evidence(player: Player, evidence: Evidence, reason: str) -> EvidenceFlag:
    from surveys.models import SurveyResponse

    maturity_days = settings.LIFECYCLE["MATURITY_ACCOUNT_AGE_DAYS"]
    maturity_surveys = settings.LIFECYCLE["MATURITY_SURVEY_COUNT"]
    account_age = (timezone.now() - player.date_joined).days
    survey_count = SurveyResponse.objects.filter(player=player).count()

    if account_age < maturity_days or survey_count < maturity_surveys:
        raise NotMatureError(
            f"Account must be {maturity_days}+ days old with {maturity_surveys}+ surveys."
        )

    if EvidenceFlag.objects.filter(flagging_player=player, evidence=evidence).exists():
        raise AlreadyFlaggedError("You have already flagged this evidence.")

    flag = EvidenceFlag.objects.create(
        flagging_player=player,
        evidence=evidence,
        reason=reason,
    )
    recompute_evidence_status(evidence)
    return flag
