from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import transaction

from points.service import award_points

from .models import Candidate, Election, VoteDeclaration

if TYPE_CHECKING:
    from accounts.models import Player


def _vote_multiplier(candidate: Candidate) -> Decimal:
    if candidate.is_endorsed:
        return Decimal(str(settings.POLIUM["ENDORSED_MULTIPLIER"]))
    if candidate.is_blacklisted:
        return Decimal(str(settings.POLIUM["BLACKLIST_MULTIPLIER"]))
    return Decimal("1")


def declare_vote(player: Player, candidate: Candidate, election: Election) -> Decimal:
    base = Decimal(settings.POLIUM["VOTE_DECLARATION_BASE"])
    amount = (base * (candidate.current_rating / 100) * _vote_multiplier(candidate)).quantize(
        Decimal("0.01")
    )

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
            award_points(player, amount, "vote_declaration", source=declaration)
            return amount

        if declaration.candidate_id == candidate.pk:
            return Decimal("0")

        declaration.candidate = candidate
        declaration.save(update_fields=["candidate"])
        return Decimal("0")
