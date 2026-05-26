from datetime import timedelta

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import Player
from evidence.models import Evidence, EvidenceFlag, EvidenceUsefulness
from evidence.service import AlreadyFlaggedError, NotMatureError, flag_evidence, submit_evidence, vote_usefulness
from polium.models import Candidate, Jurisdiction


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_candidate(db) -> Candidate:
    j = Jurisdiction.objects.create(name="Test State", level="state", created_by=None)
    c = Candidate.objects.create(name="Alice Smith", office="Governor", jurisdiction=j, created_by=None)
    c.sqid  # trigger sqid generation via post-save signal / property
    return c


def _make_evidence(player: Player, candidate: Candidate) -> Evidence:
    ct = ContentType.objects.get_for_model(Candidate)
    return Evidence.objects.create(
        content_type=ct,
        object_id=candidate.pk,
        submitted_by=player,
        url="https://example.com/evidence",
        note="A useful note.",
        status=Evidence.STATUS_VISIBLE,
    )


# ---------------------------------------------------------------------------
# Service — submit_evidence
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_submit_evidence_creates_record(player):
    candidate = _make_candidate(None)
    ev = submit_evidence(player, candidate, "https://example.com", "Test note")
    assert ev.pk is not None
    ct = ContentType.objects.get_for_model(Candidate)
    assert ev.content_type == ct
    assert ev.object_id == candidate.pk
    assert ev.submitted_by == player
    assert ev.url == "https://example.com"
    assert ev.note == "Test note"


@pytest.mark.django_db
def test_submit_evidence_no_criterion(player):
    candidate = _make_candidate(None)
    ev = submit_evidence(player, candidate, "https://example.com", "Note", criterion=None)
    assert ev.criterion is None


@pytest.mark.django_db
def test_submit_evidence_with_criterion(player):
    from surveys.models import Category, Criterion
    candidate = _make_candidate(None)
    cat = Category.objects.create(name="Ethics", description="", game="polium")
    cr = Criterion.objects.create(category=cat, question="Is the candidate honest?", is_active=True)
    ev = submit_evidence(player, candidate, "https://example.com", "Note", criterion=cr)
    assert ev.criterion == cr


# ---------------------------------------------------------------------------
# Service — vote_usefulness
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_vote_useful_creates_vote(player):
    candidate = _make_candidate(None)
    ev = _make_evidence(player, candidate)
    vote = vote_usefulness(player, ev, is_useful=True)
    assert vote.is_useful is True
    ev.refresh_from_db()
    assert ev.net_usefulness_score == 1


@pytest.mark.django_db
def test_vote_not_useful_creates_vote(player):
    candidate = _make_candidate(None)
    ev = _make_evidence(player, candidate)
    vote = vote_usefulness(player, ev, is_useful=False)
    assert vote.is_useful is False
    ev.refresh_from_db()
    assert ev.net_usefulness_score == -1


@pytest.mark.django_db
def test_vote_changes_existing_vote(player):
    candidate = _make_candidate(None)
    ev = _make_evidence(player, candidate)
    vote_usefulness(player, ev, is_useful=True)
    vote_usefulness(player, ev, is_useful=False)
    assert EvidenceUsefulness.objects.filter(player=player, evidence=ev).count() == 1
    ev.refresh_from_db()
    assert ev.net_usefulness_score == -1


@pytest.mark.django_db
def test_recompute_score_reflects_votes(player, db):
    candidate = _make_candidate(None)
    ev = _make_evidence(player, candidate)
    p2 = Player.objects.create_user(username="p2", email="p2@example.com", password="x")
    p3 = Player.objects.create_user(username="p3", email="p3@example.com", password="x")
    vote_usefulness(player, ev, is_useful=True)
    vote_usefulness(p2, ev, is_useful=True)
    vote_usefulness(p3, ev, is_useful=False)
    ev.refresh_from_db()
    assert ev.net_usefulness_score == 1  # 2 useful - 1 not_useful


# ---------------------------------------------------------------------------
# Service — flag_evidence
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_flag_evidence_creates_flag(mature_player):
    candidate = _make_candidate(None)
    ev = _make_evidence(mature_player, candidate)
    flag = flag_evidence(mature_player, ev, EvidenceFlag.REASON_IRRELEVANT)
    assert flag.pk is not None
    assert flag.flagging_player == mature_player


@pytest.mark.django_db
def test_flag_evidence_not_mature_age(player):
    candidate = _make_candidate(None)
    ev = _make_evidence(player, candidate)
    # player is newly created (age=0 days) — never mature by age
    from surveys.models import SurveyResponse
    ct = ContentType.objects.get_for_model(Player)
    for _ in range(3):
        SurveyResponse.objects.create(player=player, content_type=ct, object_id=player.pk)
    with pytest.raises(NotMatureError):
        flag_evidence(player, ev, EvidenceFlag.REASON_IRRELEVANT)


@pytest.mark.django_db
def test_flag_evidence_not_mature_surveys(player):
    candidate = _make_candidate(None)
    ev = _make_evidence(player, candidate)
    # age is old enough but no survey responses
    Player.objects.filter(pk=player.pk).update(date_joined=timezone.now() - timedelta(days=8))
    player.refresh_from_db()
    with pytest.raises(NotMatureError):
        flag_evidence(player, ev, EvidenceFlag.REASON_IRRELEVANT)


@pytest.mark.django_db
def test_flag_evidence_already_flagged(mature_player):
    candidate = _make_candidate(None)
    ev = _make_evidence(mature_player, candidate)
    flag_evidence(mature_player, ev, EvidenceFlag.REASON_IRRELEVANT)
    with pytest.raises(AlreadyFlaggedError):
        flag_evidence(mature_player, ev, EvidenceFlag.REASON_IRRELEVANT)


@pytest.mark.django_db
def test_flag_triggers_hide_when_threshold_met(mature_player):
    candidate = _make_candidate(None)
    ev = _make_evidence(mature_player, candidate)
    # score=0 → threshold=max(1,0)=1, one flag hides it
    flag_evidence(mature_player, ev, EvidenceFlag.REASON_IRRELEVANT)
    ev.refresh_from_db()
    assert ev.status == Evidence.STATUS_HIDDEN


@pytest.mark.django_db
def test_flag_does_not_hide_high_usefulness(mature_player, db):
    candidate = _make_candidate(None)
    ev = _make_evidence(mature_player, candidate)
    # manually set score=20 → threshold=max(1,2)=2; one flag insufficient
    Evidence.objects.filter(pk=ev.pk).update(net_usefulness_score=20)
    ev.refresh_from_db()
    flag_evidence(mature_player, ev, EvidenceFlag.REASON_IRRELEVANT)
    ev.refresh_from_db()
    assert ev.status == Evidence.STATUS_VISIBLE


# ---------------------------------------------------------------------------
# View tests
# ---------------------------------------------------------------------------

@pytest.fixture
def candidate(db):
    j = Jurisdiction.objects.create(name="Test State", level="state", created_by=None)
    c = Candidate.objects.create(name="Bob Jones", office="Senator", jurisdiction=j, created_by=None)
    return c


@pytest.fixture
def visible_evidence(db, player, candidate):
    ct = ContentType.objects.get_for_model(Candidate)
    return Evidence.objects.create(
        content_type=ct,
        object_id=candidate.pk,
        submitted_by=player,
        url="https://visible.example.com",
        note="Visible evidence note.",
        status=Evidence.STATUS_VISIBLE,
    )


@pytest.fixture
def hidden_evidence(db, player, candidate):
    ct = ContentType.objects.get_for_model(Candidate)
    return Evidence.objects.create(
        content_type=ct,
        object_id=candidate.pk,
        submitted_by=player,
        url="https://hidden.example.com",
        note="Hidden evidence note.",
        status=Evidence.STATUS_HIDDEN,
    )


@pytest.mark.django_db
def test_candidate_profile_returns_200(client: Client, candidate):
    url = reverse("polium:candidate_detail", kwargs={"sqid": candidate.sqid})
    response = client.get(url)
    assert response.status_code == 200


@pytest.mark.django_db
def test_candidate_profile_shows_evidence(client: Client, candidate, visible_evidence):
    url = reverse("polium:candidate_detail", kwargs={"sqid": candidate.sqid})
    response = client.get(url)
    assert b"Visible evidence note." in response.content


@pytest.mark.django_db
def test_candidate_profile_hides_non_visible(client: Client, candidate, hidden_evidence):
    url = reverse("polium:candidate_detail", kwargs={"sqid": candidate.sqid})
    response = client.get(url)
    assert b"Hidden evidence note." not in response.content


@pytest.mark.django_db
def test_candidate_profile_blacklist_notice(client: Client, candidate):
    from polium.models import BlacklistHistory
    candidate.is_blacklisted = True
    candidate.save()
    BlacklistHistory.objects.create(
        candidate=candidate,
        blacklisted_at=timezone.now(),
        rating_at_blacklist=30,
        blacklisted_by=None,
        reason="Sustained low rating.",
        forum_discussion_url="https://forum.example.com/1",
    )
    url = reverse("polium:candidate_detail", kwargs={"sqid": candidate.sqid})
    response = client.get(url)
    assert b"blacklisted" in response.content.lower()


@pytest.mark.django_db
def test_evidence_submit_requires_login(client: Client, candidate):
    url = reverse("polium:evidence_submit", kwargs={"sqid": candidate.sqid})
    response = client.post(url, {"url": "https://example.com", "note": "note"})
    assert response.status_code == 302
    assert "/login" in response["Location"] or "accounts" in response["Location"]


@pytest.mark.django_db
def test_evidence_submit_creates_record(client: Client, player, candidate):
    client.force_login(player)
    url = reverse("polium:evidence_submit", kwargs={"sqid": candidate.sqid})
    response = client.post(url, {"url": "https://example.com/new", "note": "My note"})
    assert response.status_code == 302
    assert Evidence.objects.filter(url="https://example.com/new").exists()


@pytest.mark.django_db
def test_evidence_vote_requires_login(client: Client, player, candidate, visible_evidence):
    url = reverse("polium:evidence_vote", kwargs={"pk": visible_evidence.pk})
    response = client.post(url, {"is_useful": "true"})
    assert response.status_code == 302
    assert "/login" in response["Location"] or "accounts" in response["Location"]


@pytest.mark.django_db
def test_evidence_flag_requires_login(client: Client, player, candidate, visible_evidence):
    url = reverse("polium:evidence_flag", kwargs={"pk": visible_evidence.pk})
    response = client.post(url, {"reason": "irrelevant"})
    assert response.status_code == 302
    assert "/login" in response["Location"] or "accounts" in response["Location"]


@pytest.mark.django_db
def test_evidence_flag_not_mature_shows_message(client: Client, player, candidate, visible_evidence):
    client.force_login(player)
    url = reverse("polium:evidence_flag", kwargs={"pk": visible_evidence.pk})
    response = client.post(url, {"reason": "irrelevant"}, HTTP_REFERER=f"/candidates/{candidate.sqid}/")
    assert response.status_code == 302
    follow_response = client.get(response["Location"])
    assert EvidenceFlag.objects.count() == 0
