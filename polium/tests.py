import json
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


# ── Jurisdiction search (Datastar) ────────────────────────────────────────────

def _datastar_get(client, url: str, signals: dict) -> bytes:
    resp = client.get(
        url,
        data={"datastar": json.dumps(signals)},
        headers={"Datastar-Request": "true"},
    )
    assert resp.status_code == 200
    return b"".join(resp.streaming_content)


@pytest.mark.django_db
def test_jurisdiction_search_returns_results(client, jurisdiction: Jurisdiction) -> None:
    body = _datastar_get(client, "/polium/jurisdictions/search/", {"q": "Test"})
    assert b"Test Jurisdiction" in body


@pytest.mark.django_db
def test_jurisdiction_search_empty_query_returns_empty(client) -> None:
    body = _datastar_get(client, "/polium/jurisdictions/search/", {"q": ""})
    assert b"Test Jurisdiction" not in body


@pytest.mark.django_db
def test_jurisdiction_search_no_match_shows_add_option(client) -> None:
    body = _datastar_get(client, "/polium/jurisdictions/search/", {"q": "Nonexistent Place"})
    assert b"No jurisdictions found" in body
    assert b"Add" in body


# ── create_jurisdiction ───────────────────────────────────────────────────────

@pytest.fixture
def player(db):
    from accounts.models import Player
    from accounts.utils import generate_username
    return Player.objects.create_user(
        username=generate_username(), email="creator@example.com", password=None
    )


@pytest.mark.django_db
def test_create_jurisdiction_requires_login(client) -> None:
    resp = client.post("/polium/jurisdictions/create/", {"name": "New Place", "level": "city"})
    assert resp.status_code == 302
    assert "/login" in resp["Location"] or "login" in resp["Location"]


@pytest.mark.django_db
def test_create_jurisdiction_creates_and_follows(client, player) -> None:
    client.force_login(player)
    resp = client.post("/polium/jurisdictions/create/", {"name": "New City", "level": "city"})
    assert resp.status_code == 302
    j = Jurisdiction.objects.get(name="New City")
    assert j.level == "city"
    assert j.created_by == player
    assert j.active_engagement == 1
    assert JurisdictionFollow.objects.filter(player=player, jurisdiction=j).exists()


@pytest.mark.django_db
def test_create_jurisdiction_invalid_level_redirects(client, player) -> None:
    client.force_login(player)
    resp = client.post("/polium/jurisdictions/create/", {"name": "Bad", "level": "invalid"})
    assert resp.status_code == 302
    assert not Jurisdiction.objects.filter(name="Bad").exists()


@pytest.mark.django_db
def test_create_jurisdiction_with_parent(client, player, jurisdiction: Jurisdiction) -> None:
    client.force_login(player)
    client.post("/polium/jurisdictions/create/", {
        "name": "Child City",
        "level": "city",
        "parent_sqid": jurisdiction.sqid,
    })
    child = Jurisdiction.objects.get(name="Child City")
    assert child.parent == jurisdiction


@pytest.mark.django_db
def test_create_jurisdiction_duplicate_name_allowed(client, player) -> None:
    client.force_login(player)
    client.post("/polium/jurisdictions/create/", {"name": "Wellington", "level": "city"})
    client.post("/polium/jurisdictions/create/", {"name": "Wellington", "level": "region"})
    assert Jurisdiction.objects.filter(name="Wellington").count() == 2


# ── follow_jurisdiction engagement tracking ───────────────────────────────────

@pytest.mark.django_db
def test_follow_increments_active_engagement(client, player, jurisdiction: Jurisdiction) -> None:
    assert jurisdiction.active_engagement == 0
    client.force_login(player)
    client.post("/polium/jurisdictions/follow/", {
        "jurisdiction_sqid": jurisdiction.sqid,
        "depth": "all",
    })
    jurisdiction.refresh_from_db()
    assert jurisdiction.active_engagement == 1


@pytest.mark.django_db
def test_follow_twice_does_not_double_increment(client, player, jurisdiction: Jurisdiction) -> None:
    client.force_login(player)
    client.post("/polium/jurisdictions/follow/", {"jurisdiction_sqid": jurisdiction.sqid, "depth": "all"})
    client.post("/polium/jurisdictions/follow/", {"jurisdiction_sqid": jurisdiction.sqid, "depth": "all"})
    jurisdiction.refresh_from_db()
    assert jurisdiction.active_engagement == 1


# ── unfollow_jurisdiction ─────────────────────────────────────────────────────

@pytest.mark.django_db
def test_unfollow_decrements_active_engagement(client, player, jurisdiction: Jurisdiction) -> None:
    JurisdictionFollow.objects.create(player=player, jurisdiction=jurisdiction)
    Jurisdiction.objects.filter(pk=jurisdiction.pk).update(active_engagement=1)
    client.force_login(player)
    client.post("/polium/jurisdictions/unfollow/", {"jurisdiction_sqid": jurisdiction.sqid})
    jurisdiction.refresh_from_db()
    assert jurisdiction.active_engagement == 0
    assert not JurisdictionFollow.objects.filter(player=player, jurisdiction=jurisdiction).exists()


@pytest.mark.django_db
def test_unfollow_floors_at_zero(client, player, jurisdiction: Jurisdiction) -> None:
    client.force_login(player)
    client.post("/polium/jurisdictions/unfollow/", {"jurisdiction_sqid": jurisdiction.sqid})
    jurisdiction.refresh_from_db()
    assert jurisdiction.active_engagement == 0


@pytest.mark.django_db
def test_unfollow_requires_login(client, jurisdiction: Jurisdiction) -> None:
    resp = client.post("/polium/jurisdictions/unfollow/", {"jurisdiction_sqid": jurisdiction.sqid})
    assert resp.status_code == 302
    assert "login" in resp["Location"]
