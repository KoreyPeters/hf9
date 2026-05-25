from datetime import timedelta

import pytest
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from accounts.models import Player
from surveys.models import SurveyResponse


@pytest.fixture
def player(db: None) -> Player:
    return Player.objects.create_user(username="testplayer", password="pass")


@pytest.fixture
def mature_player(db: None, player: Player) -> Player:
    Player.objects.filter(pk=player.pk).update(
        date_joined=timezone.now() - timedelta(days=8)
    )
    ct = ContentType.objects.get_for_model(Player)
    for _ in range(3):
        SurveyResponse.objects.create(player=player, content_type=ct, object_id=player.pk)
    player.refresh_from_db()
    return player
