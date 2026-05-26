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
