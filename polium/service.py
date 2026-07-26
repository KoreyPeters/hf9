from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Sum

from points.models import PointTransaction
from points.service import award_points
from surveys.ratings import compute_declaration_points

from .models import Candidate, Election, VoteDeclaration

if TYPE_CHECKING:
    from accounts.models import Player


def _vote_multiplier(candidate: Candidate) -> Decimal:
    if candidate.is_endorsed:
        return Decimal(str(settings.POLIUM["ENDORSED_MULTIPLIER"]))
    if candidate.is_blacklisted:
        return Decimal(str(settings.POLIUM["BLACKLIST_MULTIPLIER"]))
    return Decimal("1")


def _previously_awarded(player: Player, declaration: VoteDeclaration) -> Decimal:
    ct = ContentType.objects.get_for_model(VoteDeclaration)
    total = PointTransaction.objects.filter(
        player=player,
        content_type=ct,
        object_id=declaration.pk,
    ).aggregate(total=Sum("amount"))["total"]
    return total or Decimal("0")


def declare_vote(player: Player, candidate: Candidate, election: Election) -> Decimal:
    base = compute_declaration_points(candidate)
    new_points = (base * _vote_multiplier(candidate)).quantize(Decimal("0.01"))

    with transaction.atomic():
        try:
            declaration = VoteDeclaration.objects.select_for_update().get(
                player=player, election=election
            )
        except VoteDeclaration.DoesNotExist:
            declaration = VoteDeclaration.objects.create(
                player=player,
                candidate=candidate,
                election=election,
            )
            awarded = award_points(
                player, new_points, "vote_declaration", source=declaration
            )
            return awarded

        if declaration.candidate_id == candidate.pk:
            return Decimal("0")

        previously = _previously_awarded(player, declaration)
        delta = (new_points - previously).quantize(Decimal("0.01"))
        declaration.candidate = candidate
        declaration.save(update_fields=["candidate"])
        if delta != Decimal("0"):
            award_points(player, delta, "vote_declaration", source=declaration)
        return delta
