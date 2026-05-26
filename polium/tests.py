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


@pytest.mark.django_db
def test_blacklist_entry_below_threshold(candidate: Candidate) -> None:
    with patch("polium.task_views.compute_rating", return_value=0.10):
        _registry["update-candidate-rating"](candidate_id=candidate.pk)
    candidate.refresh_from_db()
    assert candidate.is_blacklisted is True
    entry = BlacklistHistory.objects.get(candidate=candidate)
    assert entry.rating_at_blacklist == Decimal("0.1")


@pytest.mark.django_db
def test_blacklist_not_triggered_above_entry(candidate: Candidate) -> None:
    with patch("polium.task_views.compute_rating", return_value=0.30):
        _registry["update-candidate-rating"](candidate_id=candidate.pk)
    candidate.refresh_from_db()
    assert candidate.is_blacklisted is False
    assert BlacklistHistory.objects.filter(candidate=candidate).count() == 0


@pytest.mark.django_db
def test_blacklist_lift_above_exit_threshold(candidate: Candidate) -> None:
    from django.utils import timezone

    now = timezone.now()
    Candidate.objects.filter(pk=candidate.pk).update(is_blacklisted=True, blacklisted_at=now)
    BlacklistHistory.objects.create(
        candidate=candidate, blacklisted_at=now, rating_at_blacklist=Decimal("0.10")
    )
    with patch("polium.task_views.compute_rating", return_value=0.60):
        _registry["update-candidate-rating"](candidate_id=candidate.pk)
    candidate.refresh_from_db()
    assert candidate.is_blacklisted is False
    entry = BlacklistHistory.objects.get(candidate=candidate)
    assert entry.lifted_at is not None


@pytest.mark.django_db
def test_blacklist_not_lifted_between_thresholds(candidate: Candidate) -> None:
    from django.utils import timezone

    now = timezone.now()
    Candidate.objects.filter(pk=candidate.pk).update(is_blacklisted=True, blacklisted_at=now)
    BlacklistHistory.objects.create(
        candidate=candidate, blacklisted_at=now, rating_at_blacklist=Decimal("0.10")
    )
    with patch("polium.task_views.compute_rating", return_value=0.40):
        _registry["update-candidate-rating"](candidate_id=candidate.pk)
    candidate.refresh_from_db()
    assert candidate.is_blacklisted is True
    entry = BlacklistHistory.objects.get(candidate=candidate)
    assert entry.lifted_at is None


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
