from decimal import Decimal
from unittest.mock import patch

import pytest

from accounts.models import Player
from django.contrib.contenttypes.models import ContentType
from points.models import PointTransaction
from points.service import award_points


@pytest.mark.django_db
def test_award_points_creates_transaction(player: Player) -> None:
    award_points(player, Decimal("10.00"), "test")
    assert PointTransaction.objects.filter(player=player).count() == 1
    tx = PointTransaction.objects.get(player=player)
    assert tx.amount == Decimal("10.00")
    assert tx.reason == "test"


@pytest.mark.django_db
def test_award_points_increments_total(player: Player) -> None:
    award_points(player, Decimal("10.00"), "first")
    award_points(player, Decimal("5.00"), "second")
    player.refresh_from_db()
    assert player.total_points == Decimal("15.00")


@pytest.mark.django_db
def test_award_points_with_source_sets_content_type(player: Player) -> None:
    award_points(player, Decimal("1.00"), "test", source=player)
    tx = PointTransaction.objects.get(player=player)
    assert tx.content_type == ContentType.objects.get_for_model(Player)
    assert tx.object_id == player.pk


@pytest.mark.django_db
def test_award_points_is_atomic(player: Player) -> None:
    with patch("points.service.get_user_model") as mock_get_user_model:
        mock_get_user_model.return_value.objects.filter.return_value.update.side_effect = (
            Exception("simulated failure")
        )
        with pytest.raises(Exception, match="simulated failure"):
            award_points(player, Decimal("10.00"), "test")
    assert PointTransaction.objects.filter(player=player).count() == 0
