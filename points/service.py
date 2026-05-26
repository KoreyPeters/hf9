from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db.models import F, Model
from django.db.transaction import atomic

from .models import PointTransaction

if TYPE_CHECKING:
    from accounts.models import Player


def award_points(
    player: Player,
    amount: Decimal,
    reason: str,
    source: Model | None = None,
) -> None:
    if not player.email_verified:
        return
    with atomic():
        PointTransaction.objects.create(
            player=player,
            amount=amount,
            reason=reason,
            content_type=ContentType.objects.get_for_model(source) if source is not None else None,
            object_id=source.pk if source is not None else None,
        )
        get_user_model().objects.filter(pk=player.pk).update(
            total_points=F("total_points") + amount,
        )
