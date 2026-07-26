from datetime import timedelta

import pytest
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from accounts.models import Player
from core.maturity import account_is_mature
from core.tasks import enqueue
from polium.models import Jurisdiction
from surveys.models import SurveyResponse


# ── Task dispatch ─────────────────────────────────────────────────────────────
#
# enqueue() takes the Cloud Tasks path only when DEBUG is off, so none of it ran
# under test until this. The audience bug below survived precisely there: every
# task 403'd on arrival and the queue retried it five times, invisible for as
# long as a separate IAM fault stopped tasks being created at all.


@pytest.fixture
def captured_task(monkeypatch, settings):
    """Run enqueue() down its production path and hand back the task it built."""
    settings.DEBUG = False
    settings.TASK_BASE_URL = "https://example.com"
    settings.TASK_SERVICE_ACCOUNT = "tasks@example.com"
    settings.GCP_PROJECT = "proj"
    settings.GCP_REGION = "region"
    settings.CLOUD_TASKS_QUEUE = "queue"

    sent = {}

    class FakeClient:
        def queue_path(self, project, region, queue):
            return f"projects/{project}/locations/{region}/queues/{queue}"

        def create_task(self, request):
            sent.update(request)

    monkeypatch.setattr(
        "google.cloud.tasks_v2.CloudTasksClient", lambda *a, **kw: FakeClient()
    )
    enqueue("process-receipt", {"purchase_id": 1})
    return sent


def test_the_task_token_names_the_audience_the_endpoint_checks(
    captured_task, settings
) -> None:
    """The pairing that matters. `_verify_oidc` verifies against TASK_BASE_URL,
    so the token has to be minted for TASK_BASE_URL. Omit it and Cloud Tasks
    defaults the audience to the full target URL, which never matches."""
    oidc = captured_task["task"]["http_request"]["oidc_token"]
    assert oidc["audience"] == settings.TASK_BASE_URL


def test_the_task_targets_the_registered_url(captured_task) -> None:
    request = captured_task["task"]["http_request"]
    assert request["url"] == "https://example.com/tasks/process-receipt/"


@pytest.mark.django_db
def test_sqid_generated_on_save(player: Player) -> None:
    assert player.sqid is not None
    assert player.sqid != ""


@pytest.mark.django_db
def test_sqid_is_deterministic(db: None) -> None:
    p1 = Player.objects.create_user(
        username="player1", email="p1@example.com", password="pass"
    )
    p2 = Player.objects.create_user(
        username="player2", email="p2@example.com", password="pass"
    )
    assert p1.sqid != p2.sqid
    fresh = Player.objects.get(pk=p1.pk)
    assert fresh.sqid == p1.sqid


@pytest.mark.django_db
def test_sqid_not_overwritten_on_resave(player: Player) -> None:
    original = player.sqid
    player.save()
    player.refresh_from_db()
    assert player.sqid == original


@pytest.mark.django_db
def test_different_models_produce_different_sqids_for_same_pk(db: None) -> None:
    player = Player.objects.create_user(
        username="sqidtest", email="sqidtest@example.com", password="pass"
    )
    jurisdiction = Jurisdiction.objects.create(name="J", level="federal")
    if player.pk != jurisdiction.pk:
        pytest.skip("PKs differ; can't test same-PK collision guard")
    assert player.sqid != jurisdiction.sqid


@pytest.mark.django_db
def test_immature_new_player(player: Player) -> None:
    assert account_is_mature(player) is False


@pytest.mark.django_db
def test_immature_old_player_insufficient_surveys(player: Player) -> None:
    Player.objects.filter(pk=player.pk).update(
        date_joined=timezone.now() - timedelta(days=8)
    )
    ct = ContentType.objects.get_for_model(Player)
    for _ in range(2):
        SurveyResponse.objects.create(
            player=player, content_type=ct, object_id=player.pk
        )
    player.refresh_from_db()
    assert account_is_mature(player) is False


@pytest.mark.django_db
def test_immature_player_enough_surveys_too_young(player: Player) -> None:
    ct = ContentType.objects.get_for_model(Player)
    for _ in range(3):
        SurveyResponse.objects.create(
            player=player, content_type=ct, object_id=player.pk
        )
    assert account_is_mature(player) is False


@pytest.mark.django_db
def test_mature_player(mature_player: Player) -> None:
    assert account_is_mature(mature_player) is True
