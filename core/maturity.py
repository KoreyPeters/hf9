from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings
from django.utils import timezone

if TYPE_CHECKING:
    from accounts.models import Player


def account_is_mature(player: Player) -> bool:
    cfg = settings.LIFECYCLE
    age_ok = (timezone.now() - player.date_joined).days >= cfg["MATURITY_ACCOUNT_AGE_DAYS"]
    surveys_ok = player.survey_responses.count() >= cfg["MATURITY_SURVEY_COUNT"]
    return age_ok and surveys_ok
