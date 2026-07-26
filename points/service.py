from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db.models import F, Model
from django.db.transaction import atomic
from django.utils import timezone

from .models import PointTransaction

if TYPE_CHECKING:
    from accounts.models import Player


def _membership_multiplier(player: Player) -> Decimal:
    try:
        m = player.membership
    except Exception:
        return Decimal("1")
    if not m.is_active or m.expires_at < timezone.now():
        return Decimal("1")
    if m.tier == "sustaining_member":
        return Decimal(str(settings.SUSTAINING_MEMBER_MULTIPLIER))
    return Decimal(str(settings.MEMBER_MULTIPLIER))


def award_points(
    player: Player,
    amount: Decimal,
    reason: str,
    source: Model | None = None,
) -> Decimal:
    if not player.email_verified:
        return Decimal("0")
    multiplier = _membership_multiplier(player)
    final_amount = (Decimal(str(amount)) * multiplier).quantize(Decimal("0.01"))
    with atomic():
        PointTransaction.objects.create(
            player=player,
            amount=final_amount,
            reason=reason,
            content_type=ContentType.objects.get_for_model(source)
            if source is not None
            else None,
            object_id=source.pk if source is not None else None,
        )
        get_user_model().objects.filter(pk=player.pk).update(
            total_points=F("total_points") + final_amount,
        )
    return final_amount
