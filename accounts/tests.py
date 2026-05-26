import hashlib
from datetime import timedelta

import pytest
from django.utils import timezone


@pytest.fixture
def player(db):
    from accounts.models import Player
    from accounts.utils import generate_username
    return Player.objects.create_user(
        username=generate_username(),
        email="test@example.com",
        password=None,
        display_name="Test Player",
    )


@pytest.fixture
def unverified_player(db):
    from accounts.models import Player
    from accounts.utils import generate_username
    p = Player.objects.create_user(
        username=generate_username(),
        email="unverified@example.com",
        password=None,
        display_name="Unverified Player",
    )
    return p


# ── Magic link ────────────────────────────────────────────────────────────────

@pytest.mark.django_db
def test_magic_link_url_contains_sesame_token(player, rf):
    from django.core import mail
    from accounts.magic import send_magic_link
    request = rf.get("/")
    request.META["SERVER_NAME"] = "testserver"
    request.META["SERVER_PORT"] = "80"
    send_magic_link(request, player)
    assert len(mail.outbox) == 1
    assert "?sesame=" in mail.outbox[0].body


@pytest.mark.django_db
def test_magic_link_logs_player_in(player, client):
    from sesame.utils import get_query_string
    token_qs = get_query_string(player)
    resp = client.get(f"/accounts/login/magic/{token_qs}")
    assert resp.status_code == 302
    assert client.session.get("_auth_user_id") == str(player.pk)


@pytest.mark.django_db
def test_magic_link_one_time_use(player, client):
    from sesame.utils import get_query_string
    token_qs = get_query_string(player)
    client.get(f"/accounts/login/magic/{token_qs}")
    client.logout()
    resp = client.get(f"/accounts/login/magic/{token_qs}")
    assert resp.status_code in (200, 302, 403)
    assert "_auth_user_id" not in client.session


@pytest.mark.django_db
def test_magic_link_login_sets_email_verified(player, client):
    from sesame.utils import get_query_string
    assert player.email_verified is False
    token_qs = get_query_string(player)
    client.get(f"/accounts/login/magic/{token_qs}")
    player.refresh_from_db()
    assert player.email_verified is True
    assert player.email_verified_at is not None


# ── Email verification ────────────────────────────────────────────────────────

@pytest.mark.django_db
def test_email_verification_sets_verified(player):
    from accounts.email_verification import verify_email_token
    from accounts.models import EmailVerification
    raw = "testtoken"
    EmailVerification.objects.create(
        player=player,
        token_hash=hashlib.sha256(raw.encode()).hexdigest(),
        expires_at=timezone.now() + timedelta(hours=48),
    )
    verify_email_token(raw)
    player.refresh_from_db()
    assert player.email_verified is True
    assert player.email_verified_at is not None


@pytest.mark.django_db
def test_email_verification_already_verified_raises(player):
    from accounts.email_verification import VerificationError, verify_email_token
    from accounts.models import EmailVerification
    raw = "testtoken2"
    EmailVerification.objects.create(
        player=player,
        token_hash=hashlib.sha256(raw.encode()).hexdigest(),
        expires_at=timezone.now() + timedelta(hours=48),
        verified_at=timezone.now(),
    )
    with pytest.raises(VerificationError, match="Already verified"):
        verify_email_token(raw)


@pytest.mark.django_db
def test_email_verification_expired_raises(player):
    from accounts.email_verification import VerificationError, verify_email_token
    from accounts.models import EmailVerification
    raw = "testtoken3"
    EmailVerification.objects.create(
        player=player,
        token_hash=hashlib.sha256(raw.encode()).hexdigest(),
        expires_at=timezone.now() - timedelta(hours=1),
    )
    with pytest.raises(VerificationError, match="expired"):
        verify_email_token(raw)


# ── Verification banner ───────────────────────────────────────────────────────

@pytest.mark.django_db
def test_unverified_player_sees_banner(unverified_player, client):
    client.force_login(unverified_player)
    resp = client.get("/accounts/welcome/")
    assert b"verify-banner" in resp.content


@pytest.mark.django_db
def test_verified_player_no_banner(client, player):
    from accounts.models import Player
    Player.objects.filter(pk=player.pk).update(email_verified=True)
    player.refresh_from_db()
    client.force_login(player)
    resp = client.get("/accounts/welcome/")
    assert b"verify-banner" not in resp.content


# ── Rate limiting ─────────────────────────────────────────────────────────────

@pytest.mark.django_db
def test_rate_limit_blocks_after_limit(rf):
    from django.core.cache import cache
    from accounts.ratelimit import check_rate_limit
    cache.clear()
    request = rf.get("/", REMOTE_ADDR="1.2.3.4")
    for _ in range(10):
        assert check_rate_limit(request, "test_action", limit=10) is True
    assert check_rate_limit(request, "test_action", limit=10) is False


@pytest.mark.django_db
def test_resend_verification_rate_limited(unverified_player, client):
    from django.core.cache import cache
    cache.clear()
    client.force_login(unverified_player)
    for _ in range(3):
        client.post("/accounts/verify-email/resend/")
    resp = client.post("/accounts/verify-email/resend/")
    assert b"Too many requests" in resp.content


# ── Signup ────────────────────────────────────────────────────────────────────

@pytest.mark.django_db
def test_signup_creates_player_with_unusable_password(client):
    from django.core.cache import cache
    from accounts.models import Player
    cache.clear()
    resp = client.post("/accounts/signup/", {
        "email": "new@example.com",
        "display_name": "New Player",
        "jurisdiction_country": "US",
        "jurisdiction_region": "CA",
    })
    assert resp.status_code == 302
    p = Player.objects.get(email="new@example.com")
    assert p.display_name == "New Player"
    assert not p.has_usable_password()
    assert p.email_verified is False


@pytest.mark.django_db
def test_signup_redirects_to_welcome(client):
    from django.core.cache import cache
    cache.clear()
    resp = client.post("/accounts/signup/", {
        "email": "welcome@example.com",
        "display_name": "Welcome Player",
    })
    assert resp.status_code == 302
    assert resp["Location"] == "/accounts/welcome/"


# ── Verification reminder task ────────────────────────────────────────────────

@pytest.mark.django_db
def test_reminder_task_skips_verified_player(player):
    from django.core import mail
    from core.tasks import _registry
    Player = player.__class__
    Player.objects.filter(pk=player.pk).update(email_verified=True)
    _registry["verify-email-reminder"](player_id=player.pk)
    assert len(mail.outbox) == 0


@pytest.mark.django_db
def test_reminder_task_skips_outside_30_day_window(unverified_player):
    from django.core import mail
    from core.tasks import _registry
    from accounts.models import Player
    Player.objects.filter(pk=unverified_player.pk).update(
        date_joined=timezone.now() - timedelta(days=31)
    )
    _registry["verify-email-reminder"](player_id=unverified_player.pk)
    assert len(mail.outbox) == 0


@pytest.mark.django_db
def test_reminder_task_sends_for_unverified_player_within_window(unverified_player):
    from django.core import mail
    from core.tasks import _registry
    _registry["verify-email-reminder"](player_id=unverified_player.pk)
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == [unverified_player.email]


# ── Player display label ───────────────────────────────────────────────────────

@pytest.mark.django_db
def test_display_label_format(player):
    assert player.sqid is not None
    assert player.display_label == f"{player.display_name} #{player.sqid[:4]}"


@pytest.mark.django_db
def test_display_label_fallback_to_username(db):
    from accounts.models import Player
    from accounts.utils import generate_username
    p = Player.objects.create_user(username=generate_username(), email="ndn@example.com", password=None)
    assert p.display_name == ""
    assert p.display_label == f"{p.username} #{p.sqid[:4]}"


def test_display_label_no_sqid():
    from accounts.models import Player
    p = Player(display_name="Jane", username="jane", sqid=None)
    assert p.display_label == "Jane"


# ── Player profile page ────────────────────────────────────────────────────────

@pytest.mark.django_db
def test_player_profile_anonymous_returns_200(client, player):
    resp = client.get(f"/accounts/profile/{player.sqid}/")
    assert resp.status_code == 200


@pytest.mark.django_db
def test_player_profile_contains_display_label(client, player):
    resp = client.get(f"/accounts/profile/{player.sqid}/")
    assert player.display_label.encode() in resp.content


@pytest.mark.django_db
def test_player_profile_authenticated_owner_sees_own_profile_flag(client, player):
    client.force_login(player)
    resp = client.get(f"/accounts/profile/{player.sqid}/")
    assert resp.status_code == 200
    assert b"your profile" in resp.content


@pytest.mark.django_db
def test_player_profile_unknown_sqid_returns_404(client):
    resp = client.get("/accounts/profile/xxxx/")
    assert resp.status_code == 404
