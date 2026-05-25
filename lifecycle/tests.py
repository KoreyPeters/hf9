import pytest

from accounts.models import Player
from polium.models import Jurisdiction, JurisdictionDuplicateFlag, JurisdictionFollow


@pytest.fixture
def jurisdiction(db: None) -> Jurisdiction:
    return Jurisdiction.objects.create(name="Parent", level="federal")


@pytest.mark.django_db
def test_should_deprecate_false_with_no_flags_no_engagement(
    jurisdiction: Jurisdiction,
) -> None:
    assert jurisdiction.should_deprecate() is False


@pytest.mark.django_db
def test_should_deprecate_true_with_flag_and_zero_engagement(
    jurisdiction: Jurisdiction, player: Player
) -> None:
    winning = Jurisdiction.objects.create(name="Winning", level="federal")
    JurisdictionDuplicateFlag.objects.create(
        flagging_player=player, flagged_jurisdiction=jurisdiction, points_to=winning
    )
    assert jurisdiction.should_deprecate() is True


@pytest.mark.django_db
def test_should_deprecate_false_below_ratio(
    jurisdiction: Jurisdiction, player: Player
) -> None:
    Jurisdiction.objects.filter(pk=jurisdiction.pk).update(active_engagement=10)
    jurisdiction.refresh_from_db()
    assert jurisdiction.should_deprecate() is False


@pytest.mark.django_db
def test_should_deprecate_true_at_ratio(
    jurisdiction: Jurisdiction, player: Player
) -> None:
    winning = Jurisdiction.objects.create(name="Winning", level="federal")
    Jurisdiction.objects.filter(pk=jurisdiction.pk).update(active_engagement=10)
    jurisdiction.refresh_from_db()
    JurisdictionDuplicateFlag.objects.create(
        flagging_player=player, flagged_jurisdiction=jurisdiction, points_to=winning
    )
    assert jurisdiction.should_deprecate() is True


@pytest.mark.django_db
def test_should_deprecate_false_just_below_ratio(
    jurisdiction: Jurisdiction, player: Player
) -> None:
    winning = Jurisdiction.objects.create(name="Winning", level="federal")
    Jurisdiction.objects.filter(pk=jurisdiction.pk).update(active_engagement=20)
    jurisdiction.refresh_from_db()
    JurisdictionDuplicateFlag.objects.create(
        flagging_player=player, flagged_jurisdiction=jurisdiction, points_to=winning
    )
    assert jurisdiction.should_deprecate() is False


@pytest.mark.django_db
def test_delete_migrates_children_to_winning_jurisdiction(
    jurisdiction: Jurisdiction, player: Player
) -> None:
    child = Jurisdiction.objects.create(name="Child", level="state", parent=jurisdiction)
    winning = Jurisdiction.objects.create(name="Winning", level="federal")
    JurisdictionDuplicateFlag.objects.create(
        flagging_player=player, flagged_jurisdiction=jurisdiction, points_to=winning
    )
    jurisdiction.delete()
    child.refresh_from_db()
    assert child.parent == winning


@pytest.mark.django_db
def test_delete_migrates_followers_to_winning_jurisdiction(
    jurisdiction: Jurisdiction, player: Player
) -> None:
    winning = Jurisdiction.objects.create(name="Winning", level="federal")
    follow = JurisdictionFollow.objects.create(player=player, jurisdiction=jurisdiction)
    JurisdictionDuplicateFlag.objects.create(
        flagging_player=player, flagged_jurisdiction=jurisdiction, points_to=winning
    )
    jurisdiction.delete()
    follow.refresh_from_db()
    assert follow.jurisdiction == winning


@pytest.mark.django_db
def test_delete_with_no_flags_removes_cleanly(jurisdiction: Jurisdiction) -> None:
    pk = jurisdiction.pk
    jurisdiction.delete()
    assert not Jurisdiction.objects.filter(pk=pk).exists()
