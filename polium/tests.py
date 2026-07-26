import json
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from accounts.models import Player
from accounts.utils import generate_username
from core.tasks import _registry
from polium.models import (
    BlacklistHistory,
    Candidate,
    Election,
    Jurisdiction,
    JurisdictionDuplicateFlag,
    JurisdictionFollow,
    VoteDeclaration,
)
from surveys.models import (
    Category,
    Criterion,
    CriterionAnswer,
    SurveyConfig,
    SurveyResponse,
)


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
def test_rating_task_sets_window_when_conditions_met(
    endorsed_candidate: Candidate,
) -> None:
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
def test_rating_task_ignores_window_without_endorsement(
    jurisdiction: Jurisdiction,
) -> None:
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
def test_rating_task_ignores_window_without_election_win(
    jurisdiction: Jurisdiction,
) -> None:
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
def test_rating_task_ignores_window_without_snapshot(
    jurisdiction: Jurisdiction,
) -> None:
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
    assert (
        b"state" not in resp.content
        or b"anonymous" in resp.content
        or resp.status_code == 200
    )


@pytest.mark.django_db
def test_authenticated_no_follows_shows_no_follows_state(
    client, jurisdiction: Jurisdiction
) -> None:
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
def test_jurisdiction_search_returns_results(
    client, jurisdiction: Jurisdiction
) -> None:
    body = _datastar_get(client, "/polium/jurisdictions/search/", {"q": "Test"})
    assert b"Test Jurisdiction" in body


@pytest.mark.django_db
def test_jurisdiction_search_empty_query_returns_empty(client) -> None:
    body = _datastar_get(client, "/polium/jurisdictions/search/", {"q": ""})
    assert b"Test Jurisdiction" not in body


@pytest.mark.django_db
def test_jurisdiction_search_no_match_shows_add_option(client) -> None:
    body = _datastar_get(
        client, "/polium/jurisdictions/search/", {"q": "Nonexistent Place"}
    )
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
    resp = client.post(
        "/polium/jurisdictions/create/", {"name": "New Place", "level": "city"}
    )
    assert resp.status_code == 302
    assert "/login" in resp["Location"] or "login" in resp["Location"]


@pytest.mark.django_db
def test_create_jurisdiction_creates_and_follows(client, player) -> None:
    client.force_login(player)
    resp = client.post(
        "/polium/jurisdictions/create/", {"name": "New City", "level": "city"}
    )
    assert resp.status_code == 302
    j = Jurisdiction.objects.get(name="New City")
    assert j.level == "city"
    assert j.created_by == player
    assert j.active_engagement == 1
    assert JurisdictionFollow.objects.filter(player=player, jurisdiction=j).exists()


@pytest.mark.django_db
def test_create_jurisdiction_invalid_level_redirects(client, player) -> None:
    client.force_login(player)
    resp = client.post(
        "/polium/jurisdictions/create/", {"name": "Bad", "level": "invalid"}
    )
    assert resp.status_code == 302
    assert not Jurisdiction.objects.filter(name="Bad").exists()


@pytest.mark.django_db
def test_create_jurisdiction_with_parent(
    client, player, jurisdiction: Jurisdiction
) -> None:
    client.force_login(player)
    client.post(
        "/polium/jurisdictions/create/",
        {
            "name": "Child City",
            "level": "city",
            "parent_sqid": jurisdiction.sqid,
        },
    )
    child = Jurisdiction.objects.get(name="Child City")
    assert child.parent == jurisdiction


@pytest.mark.django_db
def test_create_jurisdiction_duplicate_name_allowed(client, player) -> None:
    client.force_login(player)
    client.post(
        "/polium/jurisdictions/create/", {"name": "Wellington", "level": "city"}
    )
    client.post(
        "/polium/jurisdictions/create/", {"name": "Wellington", "level": "region"}
    )
    assert Jurisdiction.objects.filter(name="Wellington").count() == 2


# ── follow_jurisdiction engagement tracking ───────────────────────────────────


@pytest.mark.django_db
def test_follow_increments_active_engagement(
    client, player, jurisdiction: Jurisdiction
) -> None:
    assert jurisdiction.active_engagement == 0
    client.force_login(player)
    client.post(
        "/polium/jurisdictions/follow/",
        {
            "jurisdiction_sqid": jurisdiction.sqid,
            "depth": "all",
        },
    )
    jurisdiction.refresh_from_db()
    assert jurisdiction.active_engagement == 1


@pytest.mark.django_db
def test_follow_twice_does_not_double_increment(
    client, player, jurisdiction: Jurisdiction
) -> None:
    client.force_login(player)
    client.post(
        "/polium/jurisdictions/follow/",
        {"jurisdiction_sqid": jurisdiction.sqid, "depth": "all"},
    )
    client.post(
        "/polium/jurisdictions/follow/",
        {"jurisdiction_sqid": jurisdiction.sqid, "depth": "all"},
    )
    jurisdiction.refresh_from_db()
    assert jurisdiction.active_engagement == 1


# ── unfollow_jurisdiction ─────────────────────────────────────────────────────


@pytest.mark.django_db
def test_unfollow_decrements_active_engagement(
    client, player, jurisdiction: Jurisdiction
) -> None:
    JurisdictionFollow.objects.create(player=player, jurisdiction=jurisdiction)
    Jurisdiction.objects.filter(pk=jurisdiction.pk).update(active_engagement=1)
    client.force_login(player)
    client.post(
        "/polium/jurisdictions/unfollow/", {"jurisdiction_sqid": jurisdiction.sqid}
    )
    jurisdiction.refresh_from_db()
    assert jurisdiction.active_engagement == 0
    assert not JurisdictionFollow.objects.filter(
        player=player, jurisdiction=jurisdiction
    ).exists()


@pytest.mark.django_db
def test_unfollow_floors_at_zero(client, player, jurisdiction: Jurisdiction) -> None:
    client.force_login(player)
    client.post(
        "/polium/jurisdictions/unfollow/", {"jurisdiction_sqid": jurisdiction.sqid}
    )
    jurisdiction.refresh_from_db()
    assert jurisdiction.active_engagement == 0


@pytest.mark.django_db
def test_unfollow_requires_login(client, jurisdiction: Jurisdiction) -> None:
    resp = client.post(
        "/polium/jurisdictions/unfollow/", {"jurisdiction_sqid": jurisdiction.sqid}
    )
    assert resp.status_code == 302
    assert "login" in resp["Location"]


# ── Datastar POST helper ──────────────────────────────────────────────────────


def _datastar_post(client, url: str, signals: dict) -> bytes:
    resp = client.post(
        url,
        data=json.dumps(signals),
        content_type="application/json",
        headers={"Datastar-Request": "true"},
    )
    assert resp.status_code == 200
    return b"".join(resp.streaming_content)


# ── jurisdiction_detail ────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_jurisdiction_detail_returns_200(client, jurisdiction: Jurisdiction) -> None:
    resp = client.get(f"/polium/jurisdictions/{jurisdiction.sqid}/")
    assert resp.status_code == 200
    assert jurisdiction.name.encode() in resp.content


@pytest.mark.django_db
def test_jurisdiction_detail_404_for_unknown_sqid(client) -> None:
    resp = client.get("/polium/jurisdictions/unknownsqid/")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_jurisdiction_detail_deprecated_shows_notice(
    client, jurisdiction: Jurisdiction
) -> None:
    Jurisdiction.objects.filter(pk=jurisdiction.pk).update(
        status=Jurisdiction.STATUS_DEPRECATED
    )
    resp = client.get(f"/polium/jurisdictions/{jurisdiction.sqid}/")
    assert resp.status_code == 200
    assert b"community review" in resp.content


@pytest.mark.django_db
def test_jurisdiction_detail_shows_children(client, jurisdiction: Jurisdiction) -> None:
    child = Jurisdiction.objects.create(
        name="Child City", level="city", parent=jurisdiction
    )
    resp = client.get(f"/polium/jurisdictions/{jurisdiction.sqid}/")
    assert child.name.encode() in resp.content


@pytest.mark.django_db
def test_jurisdiction_detail_shows_elections(
    client, jurisdiction: Jurisdiction, player
) -> None:
    Election.objects.create(
        name="Big Election",
        jurisdiction=jurisdiction,
        election_date=date.today() + timedelta(days=30),
        created_by=player,
    )
    resp = client.get(f"/polium/jurisdictions/{jurisdiction.sqid}/")
    assert b"Big Election" in resp.content


@pytest.mark.django_db
def test_jurisdiction_detail_shows_candidates(
    client, jurisdiction: Jurisdiction, candidate: Candidate
) -> None:
    resp = client.get(f"/polium/jurisdictions/{jurisdiction.sqid}/")
    assert candidate.name.encode() in resp.content


@pytest.mark.django_db
def test_jurisdiction_detail_shows_follow_button_when_authenticated(
    client, player, jurisdiction: Jurisdiction
) -> None:
    client.force_login(player)
    resp = client.get(f"/polium/jurisdictions/{jurisdiction.sqid}/")
    assert resp.status_code == 200
    assert b"Follow" in resp.content


# ── jurisdiction_follow_detail / jurisdiction_unfollow_detail ─────────────────


@pytest.mark.django_db
def test_follow_detail_creates_follow_and_returns_sse(
    client, player, jurisdiction: Jurisdiction
) -> None:
    client.force_login(player)
    body = _datastar_post(
        client,
        f"/polium/jurisdictions/{jurisdiction.sqid}/follow/",
        {"follow_depth": "all"},
    )
    assert JurisdictionFollow.objects.filter(
        player=player, jurisdiction=jurisdiction
    ).exists()
    assert b"follow-section" in body


@pytest.mark.django_db
def test_follow_detail_increments_active_engagement(
    client, player, jurisdiction: Jurisdiction
) -> None:
    client.force_login(player)
    _datastar_post(
        client,
        f"/polium/jurisdictions/{jurisdiction.sqid}/follow/",
        {"follow_depth": "all"},
    )
    jurisdiction.refresh_from_db()
    assert jurisdiction.active_engagement == 1


@pytest.mark.django_db
def test_follow_detail_idempotent(client, player, jurisdiction: Jurisdiction) -> None:
    client.force_login(player)
    _datastar_post(
        client,
        f"/polium/jurisdictions/{jurisdiction.sqid}/follow/",
        {"follow_depth": "all"},
    )
    _datastar_post(
        client,
        f"/polium/jurisdictions/{jurisdiction.sqid}/follow/",
        {"follow_depth": "all"},
    )
    jurisdiction.refresh_from_db()
    assert jurisdiction.active_engagement == 1


@pytest.mark.django_db
def test_unfollow_detail_deletes_follow_and_returns_sse(
    client, player, jurisdiction: Jurisdiction
) -> None:
    JurisdictionFollow.objects.create(player=player, jurisdiction=jurisdiction)
    Jurisdiction.objects.filter(pk=jurisdiction.pk).update(active_engagement=1)
    client.force_login(player)
    body = _datastar_post(
        client, f"/polium/jurisdictions/{jurisdiction.sqid}/unfollow/", {}
    )
    assert not JurisdictionFollow.objects.filter(
        player=player, jurisdiction=jurisdiction
    ).exists()
    jurisdiction.refresh_from_db()
    assert jurisdiction.active_engagement == 0
    assert b"follow-section" in body


@pytest.mark.django_db
def test_follow_detail_requires_login(client, jurisdiction: Jurisdiction) -> None:
    resp = client.post(
        f"/polium/jurisdictions/{jurisdiction.sqid}/follow/",
        data=json.dumps({}),
        content_type="application/json",
        headers={"Datastar-Request": "true"},
    )
    assert resp.status_code == 302
    assert "login" in resp["Location"]


# ── add_election ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_add_election_creates_and_returns_sse(
    client, player, jurisdiction: Jurisdiction
) -> None:
    client.force_login(player)
    body = _datastar_post(
        client,
        f"/polium/jurisdictions/{jurisdiction.sqid}/add-election/",
        {
            "election_name": "Test Election",
            "election_date": "2025-11-04",
            "election_external_reference": "",
        },
    )
    assert Election.objects.filter(
        name="Test Election", jurisdiction=jurisdiction
    ).exists()
    assert b"elections-section" in body


@pytest.mark.django_db
def test_add_election_missing_name_returns_error(
    client, player, jurisdiction: Jurisdiction
) -> None:
    client.force_login(player)
    body = _datastar_post(
        client,
        f"/polium/jurisdictions/{jurisdiction.sqid}/add-election/",
        {
            "election_name": "",
            "election_date": "2025-11-04",
            "election_external_reference": "",
        },
    )
    assert not Election.objects.filter(jurisdiction=jurisdiction).exists()
    assert b"required" in body.lower()


@pytest.mark.django_db
def test_add_election_missing_date_returns_error(
    client, player, jurisdiction: Jurisdiction
) -> None:
    client.force_login(player)
    body = _datastar_post(
        client,
        f"/polium/jurisdictions/{jurisdiction.sqid}/add-election/",
        {
            "election_name": "My Election",
            "election_date": "",
            "election_external_reference": "",
        },
    )
    assert not Election.objects.filter(jurisdiction=jurisdiction).exists()
    assert b"required" in body.lower()


@pytest.mark.django_db
def test_add_election_requires_login(client, jurisdiction: Jurisdiction) -> None:
    resp = client.post(
        f"/polium/jurisdictions/{jurisdiction.sqid}/add-election/",
        data=json.dumps({"election_name": "X", "election_date": "2025-11-04"}),
        content_type="application/json",
        headers={"Datastar-Request": "true"},
    )
    assert resp.status_code == 302


# ── add_candidate ─────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_add_candidate_creates_and_returns_sse(
    client, player, jurisdiction: Jurisdiction
) -> None:
    client.force_login(player)
    body = _datastar_post(
        client,
        f"/polium/jurisdictions/{jurisdiction.sqid}/add-candidate/",
        {
            "candidate_name": "Alice",
            "candidate_office": "Mayor",
            "candidate_election_id": "",
            "candidate_external_reference": "",
            "candidate_bio": "",
        },
    )
    assert Candidate.objects.filter(name="Alice", jurisdiction=jurisdiction).exists()
    assert b"candidates-section" in body


@pytest.mark.django_db
def test_add_candidate_missing_name_returns_error(
    client, player, jurisdiction: Jurisdiction
) -> None:
    client.force_login(player)
    body = _datastar_post(
        client,
        f"/polium/jurisdictions/{jurisdiction.sqid}/add-candidate/",
        {
            "candidate_name": "",
            "candidate_office": "Mayor",
            "candidate_election_id": "",
            "candidate_external_reference": "",
            "candidate_bio": "",
        },
    )
    assert not Candidate.objects.filter(jurisdiction=jurisdiction).exists()
    assert b"required" in body.lower()


@pytest.mark.django_db
def test_add_candidate_rejects_cross_jurisdiction_election(
    client, player, jurisdiction: Jurisdiction
) -> None:
    other_jurisdiction = Jurisdiction.objects.create(name="Other", level="city")
    election = Election.objects.create(
        name="Other Election",
        jurisdiction=other_jurisdiction,
        election_date=date.today() + timedelta(days=10),
        created_by=player,
    )
    client.force_login(player)
    body = _datastar_post(
        client,
        f"/polium/jurisdictions/{jurisdiction.sqid}/add-candidate/",
        {
            "candidate_name": "Bob",
            "candidate_office": "MP",
            "candidate_election_id": str(election.pk),
            "candidate_external_reference": "",
            "candidate_bio": "",
        },
    )
    assert not Candidate.objects.filter(name="Bob").exists()
    assert b"does not belong" in body or b"invalid" in body.lower()


@pytest.mark.django_db
def test_add_candidate_requires_login(client, jurisdiction: Jurisdiction) -> None:
    resp = client.post(
        f"/polium/jurisdictions/{jurisdiction.sqid}/add-candidate/",
        data=json.dumps({"candidate_name": "X", "candidate_office": "Y"}),
        content_type="application/json",
        headers={"Datastar-Request": "true"},
    )
    assert resp.status_code == 302


# ── flag_jurisdiction_duplicate ────────────────────────────────────────────────


@pytest.mark.django_db
def test_flag_requires_maturity(client, player, jurisdiction: Jurisdiction) -> None:
    target = Jurisdiction.objects.create(name="Real One", level="city")
    client.force_login(player)
    body = _datastar_post(
        client,
        f"/polium/jurisdictions/{jurisdiction.sqid}/flag-duplicate/",
        {"flag_target_sqid": target.sqid},
    )
    assert not JurisdictionDuplicateFlag.objects.filter(
        flagged_jurisdiction=jurisdiction
    ).exists()
    assert b"7 days" in body


@pytest.mark.django_db
def test_flag_creates_flag_and_returns_sse(
    client, mature_player, jurisdiction: Jurisdiction
) -> None:
    target = Jurisdiction.objects.create(name="Real One", level="city")
    client.force_login(mature_player)
    body = _datastar_post(
        client,
        f"/polium/jurisdictions/{jurisdiction.sqid}/flag-duplicate/",
        {"flag_target_sqid": target.sqid},
    )
    assert JurisdictionDuplicateFlag.objects.filter(
        flagging_player=mature_player,
        flagged_jurisdiction=jurisdiction,
        points_to=target,
    ).exists()
    assert b"flag-section" in body
    assert b"Real One" in body


@pytest.mark.django_db
def test_flag_prevents_self_flagging(
    client, mature_player, jurisdiction: Jurisdiction
) -> None:
    client.force_login(mature_player)
    body = _datastar_post(
        client,
        f"/polium/jurisdictions/{jurisdiction.sqid}/flag-duplicate/",
        {"flag_target_sqid": jurisdiction.sqid},
    )
    assert not JurisdictionDuplicateFlag.objects.filter(
        flagged_jurisdiction=jurisdiction
    ).exists()
    assert b"itself" in body


@pytest.mark.django_db
def test_flag_blocks_second_flag(
    client, mature_player, jurisdiction: Jurisdiction
) -> None:
    target = Jurisdiction.objects.create(name="Real One", level="city")
    JurisdictionDuplicateFlag.objects.create(
        flagging_player=mature_player,
        flagged_jurisdiction=jurisdiction,
        points_to=target,
    )
    target2 = Jurisdiction.objects.create(name="Other Real", level="city")
    client.force_login(mature_player)
    _datastar_post(
        client,
        f"/polium/jurisdictions/{jurisdiction.sqid}/flag-duplicate/",
        {"flag_target_sqid": target2.sqid},
    )
    assert (
        JurisdictionDuplicateFlag.objects.filter(
            flagged_jurisdiction=jurisdiction
        ).count()
        == 1
    )


# ── jurisdiction_search_flag ──────────────────────────────────────────────────


def _flag_search(client, signals: dict, exclude_sqid: str = "") -> bytes:
    data: dict[str, str] = {"datastar": json.dumps(signals)}
    if exclude_sqid:
        data["exclude"] = exclude_sqid
    resp = client.get(
        "/polium/jurisdictions/flag-search/",
        data=data,
        headers={"Datastar-Request": "true"},
    )
    assert resp.status_code == 200
    return b"".join(resp.streaming_content)


@pytest.mark.django_db
def test_flag_search_returns_results(client, jurisdiction: Jurisdiction) -> None:
    other = Jurisdiction.objects.create(name="Other Place", level="city")
    body = _flag_search(client, {"flag_q": "Other"}, exclude_sqid=jurisdiction.sqid)
    assert other.name.encode() in body


@pytest.mark.django_db
def test_flag_search_excludes_current_jurisdiction(
    client, jurisdiction: Jurisdiction
) -> None:
    body = _flag_search(client, {"flag_q": "Test"}, exclude_sqid=jurisdiction.sqid)
    assert jurisdiction.name.encode() not in body


@pytest.mark.django_db
def test_flag_search_short_query_returns_empty(client) -> None:
    body = _flag_search(client, {"flag_q": "T"})
    assert b"button" not in body


# ── election_detail ───────────────────────────────────────────────────────────


@pytest.fixture
def election(db: None, jurisdiction: Jurisdiction, player: Player) -> Election:
    return Election.objects.create(
        name="Test Election",
        jurisdiction=jurisdiction,
        election_date=date.today() + timedelta(days=30),
        created_by=player,
    )


@pytest.fixture
def verified_player(db: None) -> Player:
    p = Player.objects.create_user(
        username=generate_username(), email="verified@example.com", password=None
    )
    Player.objects.filter(pk=p.pk).update(email_verified=True)
    p.refresh_from_db()
    return p


@pytest.mark.django_db
def test_election_detail_renders(client, election: Election) -> None:
    resp = client.get(f"/polium/elections/{election.sqid}/")
    assert resp.status_code == 200
    assert election.name.encode() in resp.content


@pytest.mark.django_db
def test_election_detail_404_for_unknown(client) -> None:
    resp = client.get("/polium/elections/unknownsqid/")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_election_detail_shows_linked_candidate(
    client, election: Election, jurisdiction: Jurisdiction
) -> None:
    c = Candidate.objects.create(
        name="Linked Candidate",
        jurisdiction=jurisdiction,
        office="MP",
        election=election,
    )
    resp = client.get(f"/polium/elections/{election.sqid}/")
    assert c.name.encode() in resp.content


@pytest.mark.django_db
def test_election_detail_hides_unlinked_candidate(
    client, election: Election, jurisdiction: Jurisdiction
) -> None:
    Candidate.objects.create(
        name="Unlinked Candidate", jurisdiction=jurisdiction, office="MP"
    )
    resp = client.get(f"/polium/elections/{election.sqid}/")
    assert b"Unlinked Candidate" not in resp.content


@pytest.mark.django_db
def test_election_detail_upcoming_badge(
    client, jurisdiction: Jurisdiction, player: Player
) -> None:
    e = Election.objects.create(
        name="Future Election",
        jurisdiction=jurisdiction,
        election_date=date.today() + timedelta(days=10),
        created_by=player,
    )
    resp = client.get(f"/polium/elections/{e.sqid}/")
    assert b"Upcoming" in resp.content


@pytest.mark.django_db
def test_election_detail_past_badge(
    client, jurisdiction: Jurisdiction, player: Player
) -> None:
    e = Election.objects.create(
        name="Past Election",
        jurisdiction=jurisdiction,
        election_date=date.today() - timedelta(days=1),
        created_by=player,
    )
    resp = client.get(f"/polium/elections/{e.sqid}/")
    assert b"Past" in resp.content


# ── vote declaration fixtures ─────────────────────────────────────────────────


@pytest.fixture
def decl_criterion(db: None, polium_category: Category) -> Criterion:
    return Criterion.objects.create(
        category=polium_category,
        question="Decl test criterion?",
        weight=Decimal("100.00"),
    )


@pytest.fixture
def decl_config(db: None) -> SurveyConfig:
    return SurveyConfig.objects.create(pk=1, cooldown_days=30, min_survey_threshold=1)


def _add_survey(
    player: Player, candidate: Candidate, criterion: Criterion, answer: bool = True
) -> None:
    from django.contrib.contenttypes.models import ContentType

    ct = ContentType.objects.get_for_model(Candidate)
    sr = SurveyResponse.objects.create(
        player=player, content_type=ct, object_id=candidate.pk
    )
    CriterionAnswer.objects.create(
        survey_response=sr, criterion=criterion, answer=answer
    )


# ── vote declaration — service ────────────────────────────────────────────────


@pytest.mark.django_db
def test_declare_creates_record(
    election: Election, jurisdiction: Jurisdiction, verified_player: Player
) -> None:
    from polium import service

    c = Candidate.objects.create(
        name="Alice",
        jurisdiction=jurisdiction,
        office="MP",
        election=election,
        current_rating=Decimal("0.50"),
    )
    service.declare_vote(verified_player, c, election)
    assert VoteDeclaration.objects.filter(
        player=verified_player, election=election, candidate=c
    ).exists()


@pytest.mark.django_db
def test_declare_awards_points(
    election: Election,
    jurisdiction: Jurisdiction,
    verified_player: Player,
    decl_criterion: Criterion,
    decl_config: SurveyConfig,
) -> None:
    from polium import service

    c = Candidate.objects.create(
        name="Alice",
        jurisdiction=jurisdiction,
        office="MP",
        election=election,
        current_rating=Decimal("0.50"),
    )
    _add_survey(verified_player, c, decl_criterion, answer=True)
    pts = service.declare_vote(verified_player, c, election)
    assert pts == Decimal("100.00")
    verified_player.refresh_from_db()
    assert verified_player.total_points == Decimal("100.00")


@pytest.mark.django_db
def test_declare_change_no_extra_points(
    election: Election, jurisdiction: Jurisdiction, verified_player: Player
) -> None:
    from polium import service

    c1 = Candidate.objects.create(
        name="Alice",
        jurisdiction=jurisdiction,
        office="MP",
        election=election,
        current_rating=Decimal("0.50"),
    )
    c2 = Candidate.objects.create(
        name="Bob",
        jurisdiction=jurisdiction,
        office="MP",
        election=election,
        current_rating=Decimal("0.60"),
    )
    service.declare_vote(verified_player, c1, election)
    verified_player.refresh_from_db()
    pts_after_first = verified_player.total_points

    extra = service.declare_vote(verified_player, c2, election)
    assert extra == Decimal("0")
    verified_player.refresh_from_db()
    assert verified_player.total_points == pts_after_first

    decl = VoteDeclaration.objects.get(player=verified_player, election=election)
    assert decl.candidate == c2


@pytest.mark.django_db
def test_declare_same_candidate_idempotent(
    election: Election, jurisdiction: Jurisdiction, verified_player: Player
) -> None:
    from polium import service

    c = Candidate.objects.create(
        name="Alice",
        jurisdiction=jurisdiction,
        office="MP",
        election=election,
        current_rating=Decimal("0.50"),
    )
    service.declare_vote(verified_player, c, election)
    verified_player.refresh_from_db()
    pts_after_first = verified_player.total_points

    extra = service.declare_vote(verified_player, c, election)
    assert extra == Decimal("0")
    verified_player.refresh_from_db()
    assert verified_player.total_points == pts_after_first
    assert (
        VoteDeclaration.objects.filter(
            player=verified_player, election=election
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_declare_endorsed_2x(
    election: Election,
    jurisdiction: Jurisdiction,
    verified_player: Player,
    decl_criterion: Criterion,
    decl_config: SurveyConfig,
) -> None:
    from django.conf import settings
    from polium import service

    c = Candidate.objects.create(
        name="Alice",
        jurisdiction=jurisdiction,
        office="MP",
        election=election,
        current_rating=Decimal("0.50"),
    )
    Candidate.objects.filter(pk=c.pk).update(is_endorsed=True)
    c.refresh_from_db()
    _add_survey(verified_player, c, decl_criterion, answer=True)
    # base = 100 (weight × probability=1.0), endorsed multiplier = 2.0 → 200
    endorsed_pts = service.declare_vote(verified_player, c, election)
    expected = (
        Decimal("100") * Decimal(str(settings.POLIUM["ENDORSED_MULTIPLIER"]))
    ).quantize(Decimal("0.01"))
    assert endorsed_pts == expected


@pytest.mark.django_db
def test_declare_blacklisted_025x(
    election: Election,
    jurisdiction: Jurisdiction,
    verified_player: Player,
    decl_criterion: Criterion,
    decl_config: SurveyConfig,
) -> None:
    from django.conf import settings
    from polium import service

    c = Candidate.objects.create(
        name="Alice",
        jurisdiction=jurisdiction,
        office="MP",
        election=election,
        current_rating=Decimal("0.50"),
    )
    Candidate.objects.filter(pk=c.pk).update(is_blacklisted=True)
    c.refresh_from_db()
    _add_survey(verified_player, c, decl_criterion, answer=True)
    # base = 100, blacklisted multiplier = 0.25 → 25
    pts = service.declare_vote(verified_player, c, election)
    expected = (
        Decimal("100") * Decimal(str(settings.POLIUM["BLACKLIST_MULTIPLIER"]))
    ).quantize(Decimal("0.01"))
    assert pts == expected


# ── vote declaration — view ───────────────────────────────────────────────────


@pytest.mark.django_db
def test_election_declare_view_creates_record(
    client, election: Election, jurisdiction: Jurisdiction, verified_player: Player
) -> None:
    c = Candidate.objects.create(
        name="Alice",
        jurisdiction=jurisdiction,
        office="MP",
        election=election,
        current_rating=Decimal("50.00"),
    )
    client.force_login(verified_player)
    body = _datastar_post(
        client,
        f"/polium/elections/{election.sqid}/declare/",
        {"candidate_sqid": c.sqid},
    )
    assert b"election-declare-section" in body
    assert VoteDeclaration.objects.filter(
        player=verified_player, election=election, candidate=c
    ).exists()


@pytest.mark.django_db
def test_election_declare_view_rejects_wrong_election(
    client, election: Election, jurisdiction: Jurisdiction, verified_player: Player
) -> None:
    other_election = Election.objects.create(
        name="Other",
        jurisdiction=jurisdiction,
        election_date=date.today() + timedelta(days=5),
    )
    c = Candidate.objects.create(
        name="Alice",
        jurisdiction=jurisdiction,
        office="MP",
        election=other_election,
        current_rating=Decimal("50.00"),
    )
    client.force_login(verified_player)
    body = _datastar_post(
        client,
        f"/polium/elections/{election.sqid}/declare/",
        {"candidate_sqid": c.sqid},
    )
    assert b"election-declare-section" in body
    assert not VoteDeclaration.objects.filter(
        player=verified_player, election=election
    ).exists()


@pytest.mark.django_db
def test_election_declare_view_requires_login(client, election: Election) -> None:
    resp = client.post(
        f"/polium/elections/{election.sqid}/declare/",
        data=json.dumps({"candidate_sqid": "x"}),
        content_type="application/json",
        headers={"Datastar-Request": "true"},
    )
    assert resp.status_code == 302
    assert "login" in resp["Location"]


# ── Survey view ───────────────────────────────────────────────────────────────


@pytest.fixture
def survey_player(db: None) -> Player:
    p = Player.objects.create_user(
        username=generate_username(), email="survey@example.com", password=None
    )
    Player.objects.filter(pk=p.pk).update(email_verified=True)
    p.refresh_from_db()
    return p


@pytest.fixture
def polium_category(db: None) -> Category:
    return Category.objects.create(name="Climate", description="", game="polium")


@pytest.fixture
def survey_criterion(db: None, polium_category: Category) -> Criterion:
    return Criterion.objects.create(
        category=polium_category,
        question="Has the candidate acted on climate?",
        weight=1.0,
    )


@pytest.fixture
def survey_config_obj(db: None) -> SurveyConfig:
    return SurveyConfig.objects.create(pk=1, cooldown_days=30)


@pytest.mark.django_db
def test_survey_view_requires_login(client: object, candidate: Candidate) -> None:
    resp = client.post(
        f"/polium/candidates/{candidate.sqid}/survey/", {"criterion_1": "yes"}
    )
    assert resp.status_code == 302
    assert "login" in resp["Location"]


@pytest.mark.django_db
def test_survey_empty_answers_returns_error(
    client: object,
    survey_player: Player,
    candidate: Candidate,
    survey_config_obj: SurveyConfig,
    survey_criterion: Criterion,
) -> None:
    client.force_login(survey_player)
    resp = client.post(f"/polium/candidates/{candidate.sqid}/survey/", {})
    assert resp.status_code == 200
    content = b"".join(resp.streaming_content)
    assert b"answer at least one question" in content
    assert SurveyResponse.objects.filter(player=survey_player).count() == 0


@pytest.mark.django_db
def test_survey_creates_response_and_awards_points(
    client: object,
    survey_player: Player,
    candidate: Candidate,
    survey_config_obj: SurveyConfig,
    survey_criterion: Criterion,
) -> None:
    client.force_login(survey_player)
    resp = client.post(
        f"/polium/candidates/{candidate.sqid}/survey/",
        {f"criterion_{survey_criterion.pk}": "yes"},
    )
    assert resp.status_code == 200
    assert SurveyResponse.objects.filter(player=survey_player).count() == 1
    survey_player.refresh_from_db()
    assert survey_player.total_points == Decimal("100")


@pytest.mark.django_db
def test_survey_submitted_state_in_response(
    client: object,
    survey_player: Player,
    candidate: Candidate,
    survey_config_obj: SurveyConfig,
    survey_criterion: Criterion,
) -> None:
    client.force_login(survey_player)
    resp = client.post(
        f"/polium/candidates/{candidate.sqid}/survey/",
        {f"criterion_{survey_criterion.pk}": "yes"},
    )
    content = b"".join(resp.streaming_content)
    assert b"Survey submitted" in content
    assert b"100" in content


@pytest.mark.django_db
def test_survey_cooldown_blocks_resubmit(
    client: object,
    survey_player: Player,
    candidate: Candidate,
    survey_config_obj: SurveyConfig,
    survey_criterion: Criterion,
) -> None:
    client.force_login(survey_player)
    url = f"/polium/candidates/{candidate.sqid}/survey/"
    client.post(url, {f"criterion_{survey_criterion.pk}": "yes"})
    resp = client.post(url, {f"criterion_{survey_criterion.pk}": "no"})
    assert resp.status_code == 200
    assert SurveyResponse.objects.filter(player=survey_player).count() == 1


@pytest.mark.django_db
def test_survey_updates_candidate_rating(
    client: object,
    survey_player: Player,
    candidate: Candidate,
    survey_config_obj: SurveyConfig,
    survey_criterion: Criterion,
) -> None:
    client.force_login(survey_player)
    client.post(
        f"/polium/candidates/{candidate.sqid}/survey/",
        {f"criterion_{survey_criterion.pk}": "yes"},
    )
    candidate.refresh_from_db()
    assert candidate.current_rating == Decimal("1.00")


@pytest.mark.django_db
def test_survey_no_points_for_unverified_player(
    client: object,
    candidate: Candidate,
    survey_config_obj: SurveyConfig,
    survey_criterion: Criterion,
) -> None:
    unverified = Player.objects.create_user(
        username=generate_username(), email="unverified2@example.com", password=None
    )
    client.force_login(unverified)
    resp = client.post(
        f"/polium/candidates/{candidate.sqid}/survey/",
        {f"criterion_{survey_criterion.pk}": "yes"},
    )
    assert resp.status_code == 200
    unverified.refresh_from_db()
    assert unverified.total_points == Decimal("0")
