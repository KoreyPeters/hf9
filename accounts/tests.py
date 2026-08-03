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
def test_a_forged_forwarded_header_cannot_reset_the_bucket(rf):
    """The bypass, reproduced.

    Observed against production on 2026-08-02: six signup posts carrying
    `X-Forwarded-For: 203.0.113.77` filled the bucket at five, and changing the
    value to `203.0.113.88` was let straight through. Proxies append on the
    right, so the leftmost entry is whatever the caller typed — one header per
    request bought unlimited signups.

    Here the attacker controls the first entry and Cloud Run appends the real
    address. Varying their half must change nothing.
    """
    from django.core.cache import cache

    from accounts.ratelimit import check_rate_limit

    cache.clear()
    for forged in ("203.0.113.77", "203.0.113.88", "198.51.100.5"):
        request = rf.get("/", HTTP_X_FORWARDED_FOR=f"{forged}, 70.72.174.10")
        assert check_rate_limit(request, "signup", limit=3) is True

    request = rf.get("/", HTTP_X_FORWARDED_FOR="203.0.113.99, 70.72.174.10")
    assert check_rate_limit(request, "signup", limit=3) is False, (
        "a fourth forged value got a fresh bucket — the rate limit is keyed on "
        "client-supplied data and can be defeated with one header"
    )


@pytest.mark.django_db
def test_separate_real_clients_get_separate_buckets(rf):
    """The other half. Keying on something the client cannot influence must not
    collapse everybody into one shared limit."""
    from django.core.cache import cache

    from accounts.ratelimit import check_rate_limit

    cache.clear()
    for _ in range(3):
        first = rf.get("/", HTTP_X_FORWARDED_FOR="203.0.113.1, 70.72.174.10")
        assert check_rate_limit(first, "signup", limit=3) is True

    second = rf.get("/", HTTP_X_FORWARDED_FOR="203.0.113.1, 198.51.100.200")
    assert check_rate_limit(second, "signup", limit=3) is True


@pytest.mark.django_db
def test_the_hop_count_follows_the_infrastructure(rf, settings):
    """`TRUSTED_PROXY_HOPS` exists because the right index is not a constant.

    Cloud Run's front end appends one entry. A Google load balancer produces
    `<existing>,<client-ip>,<load-balancer-ip>` and moves the real client to
    second from the end — so enabling `load_balancer.tf.disabled` without
    changing this would bucket the entire site under the load balancer.
    """
    from accounts.ratelimit import client_ip

    header = "203.0.113.77, 70.72.174.10, 35.190.1.1"

    settings.TRUSTED_PROXY_HOPS = 1
    assert client_ip(rf.get("/", HTTP_X_FORWARDED_FOR=header)) == "35.190.1.1"

    settings.TRUSTED_PROXY_HOPS = 2
    assert client_ip(rf.get("/", HTTP_X_FORWARDED_FOR=header)) == "70.72.174.10"


@pytest.mark.django_db
def test_a_short_header_does_not_blow_up(rf, settings):
    """Fewer entries than configured hops falls back to the leftmost rather than
    raising. That value is spoofable, but such a request did not arrive through
    the infrastructure this setting describes, and not rate limiting at all
    would be worse."""
    from accounts.ratelimit import client_ip

    settings.TRUSTED_PROXY_HOPS = 3
    assert client_ip(rf.get("/", HTTP_X_FORWARDED_FOR="203.0.113.5")) == "203.0.113.5"


@pytest.mark.django_db
def test_no_forwarded_header_falls_back_to_remote_addr(rf):
    """Development, where nothing is in front of the app."""
    from accounts.ratelimit import client_ip

    assert client_ip(rf.get("/", REMOTE_ADDR="1.2.3.4")) == "1.2.3.4"


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
    resp = client.post(
        "/accounts/signup/",
        {
            "email": "new@example.com",
            "display_name": "New Player",
            "jurisdiction_country": "US",
            "jurisdiction_region": "CA",
        },
    )
    assert resp.status_code == 302
    p = Player.objects.get(email="new@example.com")
    assert p.display_name == "New Player"
    assert not p.has_usable_password()
    assert p.email_verified is False


@pytest.mark.django_db
def test_signup_redirects_to_welcome(client):
    from django.core.cache import cache

    cache.clear()
    resp = client.post(
        "/accounts/signup/",
        {
            "email": "welcome@example.com",
            "display_name": "Welcome Player",
        },
    )
    assert resp.status_code == 302
    assert resp["Location"] == "/accounts/welcome/"


# ── The bot check on signup ───────────────────────────────────────────────────
#
# Added after twenty automated signups against a harvested email list, each of
# which made us mail a real stranger — see plans/bot-signups.md. The tests that
# matter are the ones about what happens when the check is misconfigured or
# unreachable, because those are the states that quietly turn it off.


@pytest.mark.django_db
def test_a_signup_without_a_turnstile_token_is_refused(client, settings, monkeypatch):
    """And crucially, refused *before* anything is created or mailed."""
    from django.core import mail
    from django.core.cache import cache

    from accounts import turnstile
    from accounts.models import Player

    cache.clear()
    settings.TURNSTILE_SECRET_KEY = "configured"
    monkeypatch.setattr(
        turnstile,
        "_siteverify",
        lambda payload: pytest.fail("no token should mean no network call"),
    )

    resp = client.post(
        "/accounts/signup/",
        {"email": "bot@example.com", "display_name": "Bot"},
    )

    assert resp.status_code == 200
    assert not Player.objects.filter(email="bot@example.com").exists()
    assert mail.outbox == [], "we mailed the address before proving anyone owns it"


@pytest.mark.django_db
def test_a_rejected_challenge_creates_no_account(client, settings, monkeypatch):
    from django.core import mail
    from django.core.cache import cache

    from accounts import turnstile
    from accounts.models import Player

    cache.clear()
    settings.TURNSTILE_SECRET_KEY = "configured"
    monkeypatch.setattr(turnstile, "_siteverify", lambda payload: {"success": False})

    resp = client.post(
        "/accounts/signup/",
        {
            "email": "bot@example.com",
            "display_name": "Bot",
            "cf-turnstile-response": "wrong",
        },
    )

    assert resp.status_code == 200
    assert not Player.objects.filter(email="bot@example.com").exists()
    assert mail.outbox == []


@pytest.mark.django_db
def test_a_passing_challenge_lets_a_real_person_through(client, settings, monkeypatch):
    from django.core.cache import cache

    from accounts import turnstile
    from accounts.models import Player

    cache.clear()
    settings.TURNSTILE_SECRET_KEY = "configured"
    monkeypatch.setattr(turnstile, "_siteverify", lambda payload: {"success": True})

    resp = client.post(
        "/accounts/signup/",
        {
            "email": "real@example.com",
            "display_name": "Real Person",
            "cf-turnstile-response": "ok",
        },
    )

    assert resp.status_code == 302
    assert Player.objects.filter(email="real@example.com").exists()


def test_an_unreachable_cloudflare_fails_closed(settings, monkeypatch):
    """Treating a network failure as a pass would hand over the bypass.

    Making a third party unreachable from our container is easier than solving
    the challenge, so "Cloudflare timed out" must not mean "come in".
    """
    import urllib.error

    from accounts import turnstile

    settings.TURNSTILE_SECRET_KEY = "configured"

    def explode(payload):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(turnstile, "_siteverify", explode)
    assert turnstile.verify("token") is False


def test_a_missing_secret_fails_closed_in_production(settings, monkeypatch):
    """The misconfiguration that would otherwise disable the check silently.

    Waving signups through when the secret is absent is the failure nobody
    notices until they read the user table — which is exactly how this got
    found the first time.
    """
    from accounts import turnstile

    settings.DEBUG = False
    settings.TURNSTILE_SECRET_KEY = ""
    monkeypatch.setattr(
        turnstile,
        "_siteverify",
        lambda payload: pytest.fail("no secret should mean no network call"),
    )
    assert turnstile.verify("anything") is False


def test_the_check_is_skipped_in_development(settings):
    """So the suite and a local runserver need no Cloudflare account. Keyed on
    DEBUG, so it cannot follow a missing env var into production."""
    from accounts import turnstile

    settings.DEBUG = True
    settings.TURNSTILE_SECRET_KEY = ""
    assert turnstile.verify("") is True


def test_missing_keys_warn_without_stopping_the_boot(settings):
    """A warning, deliberately, and this is the interesting assertion.

    It used to be an `Error`. `migrate` runs all system checks and runs at
    container start, so an Error exited non-zero, the container never listened
    on its port, and Cloud Run crash-looped the revision — a missing signup key
    taking Polium and receipt processing down with it. Observed in production on
    2026-08-02.

    A check whose blast radius exceeds the thing it checks is worse than the
    problem. `verify` still refuses signups; only the boot is spared.
    """
    from django.core.checks import Error

    from accounts.turnstile import check_turnstile_configured

    settings.DEBUG = False
    settings.TURNSTILE_SITE_KEY = ""
    settings.TURNSTILE_SECRET_KEY = ""

    results = check_turnstile_configured(None)
    assert [r.id for r in results] == ["accounts.W001"]
    assert not any(isinstance(r, Error) for r in results), (
        "an Error here aborts `manage.py migrate`, which start.sh runs before "
        "uvicorn — the whole service would fail to boot over a signup key"
    )

    settings.TURNSTILE_SITE_KEY = "site"
    settings.TURNSTILE_SECRET_KEY = "secret"
    assert check_turnstile_configured(None) == []


@pytest.mark.django_db
def test_the_widget_survives_a_form_error(client, settings):
    """Every error path re-renders this form. One that dropped the site key
    would render a form with no widget, which submits with no token and is
    refused — a dead end that looks like a broken site."""
    from django.core.cache import cache

    cache.clear()
    settings.TURNSTILE_SITE_KEY = "site-key-here"

    resp = client.post("/accounts/signup/", {"email": "", "display_name": ""})

    assert resp.status_code == 200
    assert b"site-key-here" in resp.content


# ── Outbound mail identity ────────────────────────────────────────────────────


@pytest.mark.django_db
def test_account_mail_comes_from_the_configured_sender(client, settings):
    """Both send sites used to hardcode noreply@humanflourishing.org — a third
    domain, matching neither DEFAULT_FROM_EMAIL nor MAILGUN_SENDER_DOMAIN, and
    one the project no longer owns. Mail from a domain we cannot sign fails
    alignment at the receiver, and its bounces belong to whoever holds the
    domain now."""
    from django.core import mail
    from django.core.cache import cache

    cache.clear()
    resp = client.post(
        "/accounts/signup/",
        {"email": "sender@example.com", "display_name": "Sender"},
    )

    assert resp.status_code == 302
    assert mail.outbox, "signup sent no verification mail"
    assert mail.outbox[0].from_email == settings.DEFAULT_FROM_EMAIL


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

    p = Player.objects.create_user(
        username=generate_username(), email="ndn@example.com", password=None
    )
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
    assert b"No activity" in resp.content


@pytest.mark.django_db
def test_player_profile_unknown_sqid_returns_404(client):
    resp = client.get("/accounts/profile/xxxx/")
    assert resp.status_code == 404


# ── Pages render at all ───────────────────────────────────────────────────────
#
# Added after /accounts/login/ reached production returning 500: the template
# reversed 'socialaccount_login', which allauth 65 does not define, so the page
# could never have rendered. Every other test here exercises view logic against
# a page it assumes already works — nothing actually rendered the templates, so
# a broken tag was invisible until a person clicked the link.


@pytest.mark.django_db
@pytest.mark.parametrize(
    "path",
    [
        "/accounts/login/",
        "/accounts/signup/",
        "/accounts/login/magic/",
    ],
)
def test_anonymous_pages_render(client, path):
    assert client.get(path).status_code == 200


@pytest.mark.django_db
def test_login_page_offers_both_social_providers(client):
    """The reverse that broke was for these two links specifically. A bare 200
    would still pass if the buttons silently vanished."""
    content = client.get("/accounts/login/").content
    assert b"Continue with Google" in content
    assert b"Sign in with Apple" in content
    assert b"/accounts/google/login/" in content
    assert b"/accounts/apple/login/" in content
