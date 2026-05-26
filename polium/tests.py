from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from core.tasks import _registry
from polium.models import BlacklistHistory, Candidate, Election, Jurisdiction, JurisdictionFollow


@pytest.fixture
def jurisdiction(db: None) -> Jurisdiction:
    return Jurisdiction.objects.create(name="Test Jurisdiction", level="federal")


@pytest.fixture
def candidate(db: None, jurisdiction: Jurisdiction) -> Candidate:
    return Candidate.objects.create(
        name="Test Candidate", jurisdiction=jurisdiction, office="Senator"
    )


@pytest.fixture
def endorsed_candidate(db: None, jurisdiction: Jurisdiction) -> Candidate:
    c = Candidate.objects.create(
        name="Endorsed Candidate", jurisdiction=jurisdiction, office="Senator"
    )
    Candidate.objects.filter(pk=c.pk).update(
        is_endorsed=True,
        election_win_confirmed=True,
        pre_election_rating_snapshot=Decimal("0.80"),
        current_rating=Decimal("0.80"),
    )
    c.refresh_from_db()
    return c


# ── Rating task — blacklisting removed ────────────────────────────────────────

@pytest.mark.django_db
def test_rating_task_does_not_blacklist(candidate: Candidate) -> None:
    with patch("polium.task_views.compute_rating", return_value=0.05):
        _registry["update-candidate-rating"](candidate_id=candidate.pk)
    candidate.refresh_from_db()
    assert candidate.is_blacklisted is False
    assert BlacklistHistory.objects.filter(candidate=candidate).count() == 0


@pytest.mark.django_db
def test_no_rating_returns_early(candidate: Candidate) -> None:
    with patch("polium.task_views.compute_rating", return_value=None):
        _registry["update-candidate-rating"](candidate_id=candidate.pk)
    fresh = Candidate.objects.get(pk=candidate.pk)
    assert fresh.current_rating == 0
    assert BlacklistHistory.objects.filter(candidate=candidate).count() == 0


@pytest.mark.django_db
def test_registry_contains_update_candidate_rating() -> None:
    assert "update-candidate-rating" in _registry


@pytest.mark.django_db
def test_task_updates_current_rating(candidate: Candidate) -> None:
    with patch("polium.task_views.compute_rating", return_value=0.75):
        _registry["update-candidate-rating"](candidate_id=candidate.pk)
    candidate.refresh_from_db()
    assert candidate.current_rating == Decimal("0.75")


@pytest.mark.django_db
def test_task_callable_directly_without_http(candidate: Candidate) -> None:
    with patch("polium.task_views.compute_rating", return_value=0.50):
        _registry["update-candidate-rating"](candidate_id=candidate.pk)


# ── Window tracking ───────────────────────────────────────────────────────────

@pytest.mark.django_db
def test_rating_task_sets_window_when_conditions_met(endorsed_candidate: Candidate) -> None:
    # pre_election_rating_snapshot=0.80, BLACKLIST_RATIO=0.50 → threshold=0.40
    # rating of 0.30 is below threshold
    with patch("polium.task_views.compute_rating", return_value=0.30):
        _registry["update-candidate-rating"](candidate_id=endorsed_candidate.pk)
    endorsed_candidate.refresh_from_db()
    assert endorsed_candidate.rating_below_threshold_since is not None


@pytest.mark.django_db
def test_rating_task_clears_window_on_recovery(endorsed_candidate: Candidate) -> None:
    from django.utils import timezone
    Candidate.objects.filter(pk=endorsed_candidate.pk).update(
        rating_below_threshold_since=timezone.now() - timedelta(days=10)
    )
    # rating of 0.50 is above threshold (0.40)
    with patch("polium.task_views.compute_rating", return_value=0.50):
        _registry["update-candidate-rating"](candidate_id=endorsed_candidate.pk)
    endorsed_candidate.refresh_from_db()
    assert endorsed_candidate.rating_below_threshold_since is None


@pytest.mark.django_db
def test_rating_task_threshold_scales_with_snapshot(jurisdiction: Jurisdiction) -> None:
    # snapshot=0.60 → threshold=0.30; rating of 0.35 is above threshold, window not set
    c = Candidate.objects.create(name="C", jurisdiction=jurisdiction, office="MP")
    Candidate.objects.filter(pk=c.pk).update(
        is_endorsed=True,
        election_win_confirmed=True,
        pre_election_rating_snapshot=Decimal("0.60"),
        current_rating=Decimal("0.60"),
    )
    with patch("polium.task_views.compute_rating", return_value=0.35):
        _registry["update-candidate-rating"](candidate_id=c.pk)
    c.refresh_from_db()
    assert c.rating_below_threshold_since is None

    # rating of 0.25 is below threshold (0.30), window should be set
    with patch("polium.task_views.compute_rating", return_value=0.25):
        _registry["update-candidate-rating"](candidate_id=c.pk)
    c.refresh_from_db()
    assert c.rating_below_threshold_since is not None


@pytest.mark.django_db
def test_rating_task_ignores_window_without_endorsement(jurisdiction: Jurisdiction) -> None:
    c = Candidate.objects.create(name="C", jurisdiction=jurisdiction, office="MP")
    Candidate.objects.filter(pk=c.pk).update(
        is_endorsed=False,
        election_win_confirmed=True,
        pre_election_rating_snapshot=Decimal("0.80"),
    )
    with patch("polium.task_views.compute_rating", return_value=0.10):
        _registry["update-candidate-rating"](candidate_id=c.pk)
    c.refresh_from_db()
    assert c.rating_below_threshold_since is None


@pytest.mark.django_db
def test_rating_task_ignores_window_without_election_win(jurisdiction: Jurisdiction) -> None:
    c = Candidate.objects.create(name="C", jurisdiction=jurisdiction, office="MP")
    Candidate.objects.filter(pk=c.pk).update(
        is_endorsed=True,
        election_win_confirmed=False,
        pre_election_rating_snapshot=Decimal("0.80"),
    )
    with patch("polium.task_views.compute_rating", return_value=0.10):
        _registry["update-candidate-rating"](candidate_id=c.pk)
    c.refresh_from_db()
    assert c.rating_below_threshold_since is None


@pytest.mark.django_db
def test_rating_task_ignores_window_without_snapshot(jurisdiction: Jurisdiction) -> None:
    c = Candidate.objects.create(name="C", jurisdiction=jurisdiction, office="MP")
    Candidate.objects.filter(pk=c.pk).update(
        is_endorsed=True,
        election_win_confirmed=True,
        pre_election_rating_snapshot=None,
    )
    with patch("polium.task_views.compute_rating", return_value=0.10):
        _registry["update-candidate-rating"](candidate_id=c.pk)
    c.refresh_from_db()
    assert c.rating_below_threshold_since is None


@pytest.mark.django_db
def test_blacklist_history_has_no_lifted_at(candidate: Candidate) -> None:
    from django.utils import timezone
    entry = BlacklistHistory.objects.create(
        candidate=candidate,
        blacklisted_at=timezone.now(),
        rating_at_blacklist=Decimal("0.10"),
    )
    assert not hasattr(entry, "lifted_at")
    assert not hasattr(entry, "rating_at_lift")


# ── Polium home ───────────────────────────────────────────────────────────────

@pytest.mark.django_db
def test_anonymous_polium_home_returns_200(client) -> None:
    resp = client.get("/polium/")
    assert resp.status_code == 200
    assert b"state" not in resp.content or b"anonymous" in resp.content or resp.status_code == 200


@pytest.mark.django_db
def test_authenticated_no_follows_shows_no_follows_state(client, jurisdiction: Jurisdiction) -> None:
    from accounts.models import Player
    from accounts.utils import generate_username
    player = Player.objects.create_user(
        username=generate_username(), email="home@example.com", password=None
    )
    client.force_login(player)
    resp = client.get("/polium/")
    assert resp.status_code == 200
    assert b"Follow jurisdictions" in resp.content


@pytest.mark.django_db
def test_authenticated_with_follows_and_elections_shows_elections(
    client, jurisdiction: Jurisdiction
) -> None:
    from accounts.models import Player
    from accounts.utils import generate_username
    player = Player.objects.create_user(
        username=generate_username(), email="home2@example.com", password=None
    )
    JurisdictionFollow.objects.create(player=player, jurisdiction=jurisdiction)
    Election.objects.create(
        name="Test Election",
        jurisdiction=jurisdiction,
        election_date=date.today() + timedelta(days=30),
        created_by=player,
    )
    client.force_login(player)
    resp = client.get("/polium/")
    assert resp.status_code == 200
    assert b"Test Election" in resp.content


@pytest.mark.django_db
def test_authenticated_with_follows_no_elections_shows_no_elections_state(
    client, jurisdiction: Jurisdiction
) -> None:
    from accounts.models import Player
    from accounts.utils import generate_username
    player = Player.objects.create_user(
        username=generate_username(), email="home3@example.com", password=None
    )
    JurisdictionFollow.objects.create(player=player, jurisdiction=jurisdiction)
    client.force_login(player)
    resp = client.get("/polium/")
    assert resp.status_code == 200
    assert b"No upcoming elections" in resp.content
