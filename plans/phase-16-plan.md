# Phase 16 — Authentication

Replaces Django's built-in username/password login with the four auth methods described in the
design: **magic link**, **Google One-tap**, **Sign in with Apple**, and **Passkey (WebAuthn)**.
No password is ever created or required.

Covers: Player model additions, magic link via `django-sesame`, email verification (points gate),
Google/Apple via `django-allauth`, passkey via `py_webauthn`, signup form, rate limiting, and URL
clean-up.

---

## Dependencies to add

```
django-sesame                   # Magic link — stateless signed tokens, single-use, TTL
django-allauth[socialaccount]   # Google + Apple OAuth2
py_webauthn                     # Passkey / WebAuthn registration + authentication
```

`anymail` is already installed and configured. Redis is already configured (rate limit cache).

---

## §16.1 — Player model additions

### New fields on `Player`

```python
# accounts/models.py

class Player(SqidMixin, AbstractUser):
    email = models.EmailField(unique=True)           # override AbstractUser — unique required for magic link
    display_name = models.CharField(max_length=100, blank=True)
    email_verified = models.BooleanField(default=False)
    email_verified_at = models.DateTimeField(null=True, blank=True)
    jurisdiction_country = models.CharField(max_length=2, blank=True)   # ISO 3166-1 alpha-2
    jurisdiction_region = models.CharField(max_length=10, blank=True)   # ISO 3166-2 subdivision
    total_points = models.DecimalField(max_digits=12, decimal_places=2, default=0)
```

`username` is kept as the internal Django identifier and auto-generated at account creation (never
shown to users). `display_name` is what appears in the UI.

### Auto-generating usernames

Since no username is collected at signup, generate one at account creation time:

```python
# accounts/utils.py
import uuid

def generate_username() -> str:
    return uuid.uuid4().hex[:20]
```

All account creation paths (magic link signup, social signup) call `generate_username()` and set
`username` before calling `Player.objects.create_user()`. The `display_name` is what the player
sees and edits.

---

## §16.2 — New models

`MagicLinkToken` is **not needed** — `django-sesame` uses stateless signed tokens (Blake2 keyed
hash derived from `SECRET_KEY`). No database row is created or consumed on each magic link.

### `EmailVerification`

Email verification uses the same hash pattern as the old custom magic link — raw token sent in the
email link, only its SHA-256 hash stored. Sesame is not used here because email verification is a
separate concern from authentication.

```python
# accounts/models.py

class EmailVerification(models.Model):
    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name="email_verifications")
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["token_hash"])]
```

### `PasskeyCredential`

One row per registered authenticator. A player may have multiple passkeys (phone, laptop, YubiKey).

```python
class PasskeyCredential(models.Model):
    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name="passkeys")
    credential_id = models.BinaryField(unique=True)
    public_key = models.BinaryField()
    sign_count = models.PositiveIntegerField(default=0)
    aaguid = models.CharField(max_length=36, blank=True)
    device_name = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
```

---

## §16.3 — Settings changes

### `hf/settings/base.py`

```python
INSTALLED_APPS = [
    ...
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "allauth.socialaccount.providers.apple",
]

MIDDLEWARE = [
    ...
    "allauth.account.middleware.AccountMiddleware",   # after SessionMiddleware
]

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "sesame.backends.ModelBackend",                  # magic link token validation
    "allauth.account.auth_backends.AuthenticationBackend",
]

# django-sesame — magic link tokens
SESAME_MAX_AGE = 900              # 15 minutes in seconds
SESAME_ONE_TIME = True            # invalidate token after first use
SESAME_INVALIDATE_ON_EMAIL_CHANGE = True

# allauth — headless mode (no allauth templates; we own the UX)
HEADLESS_ONLY = True
ACCOUNT_EMAIL_REQUIRED = True
ACCOUNT_EMAIL_VERIFICATION = "none"      # we handle verification ourselves
ACCOUNT_USERNAME_REQUIRED = False
ACCOUNT_USER_MODEL_USERNAME_FIELD = None  # we generate usernames programmatically
SOCIALACCOUNT_AUTO_SIGNUP = True
SOCIALACCOUNT_EMAIL_REQUIRED = True

SOCIALACCOUNT_PROVIDERS = {
    "google": {
        "APP": {
            "client_id": config("GOOGLE_CLIENT_ID", default=""),
            "secret": config("GOOGLE_CLIENT_SECRET", default=""),
        },
        "SCOPE": ["profile", "email"],
        "AUTH_PARAMS": {"access_type": "online"},
    },
    "apple": {
        "APP": {
            "client_id": config("APPLE_CLIENT_ID", default=""),     # Services ID
            "secret": config("APPLE_CLIENT_SECRET", default=""),    # generated JWT
            "key": config("APPLE_KEY_ID", default=""),
            "certificate_key": config("APPLE_PRIVATE_KEY", default=""),
        },
    },
}

ACCOUNT_ADAPTER = "accounts.adapters.AccountAdapter"
SOCIALACCOUNT_ADAPTER = "accounts.adapters.SocialAccountAdapter"

# Email verification
EMAIL_VERIFICATION_TTL_HOURS = 48

# Passkey (WebAuthn) settings
WEBAUTHN_RP_ID = config("WEBAUTHN_RP_ID", default="localhost")
WEBAUTHN_RP_NAME = "Human Flourishing"
WEBAUTHN_ORIGIN = config("WEBAUTHN_ORIGIN", default="http://localhost:8000")
```

---

## §16.4 — Magic link via django-sesame

### How sesame works

`django-sesame` generates a cryptographically signed token containing the user's PK and a
timestamp, signed with Blake2 in keyed mode using a key derived from `SECRET_KEY`. No database
row is created. Single-use is enforced by tying the token to the user's `last_login` value — once
the player logs in, all prior tokens are automatically invalid.

Token generation:
```python
from sesame.utils import get_query_string
query = get_query_string(player)   # returns "?sesame=<token>"
link = request.build_absolute_uri("/accounts/login/magic/") + query
# e.g. https://hf.example.com/accounts/login/magic/?sesame=AbCdEf...
```

Token redemption is handled entirely by `sesame.views.LoginView` — mount it at the magic link
URL and it does the rest: validates the token, calls `login()`, redirects to `LOGIN_REDIRECT_URL`.

### `accounts/magic.py` — thin email sender

All cryptographic complexity is gone. This file is now only responsible for composing and sending
the email:

```python
# accounts/magic.py
from django.core.mail import send_mail
from django.http import HttpRequest

from sesame.utils import get_query_string

from .models import Player


def send_magic_link(request: HttpRequest, player: Player) -> None:
    link = request.build_absolute_uri("/accounts/login/magic/") + get_query_string(player)
    send_mail(
        subject="Your Human Flourishing login link",
        message=f"Click to log in (link expires in 15 minutes):\n\n{link}",
        from_email="noreply@humanflourishing.org",
        recipient_list=[player.email],
    )
```

---

## §16.5 — Email verification service

Email verification uses a custom token (not sesame) because it is a one-time action that must be
DB-tracked and is not an authentication event.

### `accounts/email_verification.py`

```python
# accounts/email_verification.py
import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .models import EmailVerification, Player


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def send_verification_email(request, player: Player) -> None:
    from django.core.mail import send_mail
    EmailVerification.objects.filter(player=player, verified_at__isnull=True).delete()
    raw = secrets.token_urlsafe(32)
    EmailVerification.objects.create(
        player=player,
        token_hash=_hash(raw),
        expires_at=timezone.now() + timedelta(hours=settings.EMAIL_VERIFICATION_TTL_HOURS),
    )
    link = request.build_absolute_uri(f"/accounts/verify-email/{raw}/")
    send_mail(
        subject="Verify your Human Flourishing email",
        message=f"Click to verify your email address:\n\n{link}",
        from_email="noreply@humanflourishing.org",
        recipient_list=[player.email],
    )


class VerificationError(Exception):
    pass


def verify_email_token(raw: str) -> Player:
    try:
        record = EmailVerification.objects.select_related("player").get(token_hash=_hash(raw))
    except EmailVerification.DoesNotExist:
        raise VerificationError("Invalid token.")
    if record.verified_at is not None:
        raise VerificationError("Already verified.")
    if record.expires_at < timezone.now():
        raise VerificationError("Token expired.")
    record.verified_at = timezone.now()
    record.save(update_fields=["verified_at"])
    player = record.player
    player.email_verified = True
    player.email_verified_at = timezone.now()
    player.save(update_fields=["email_verified", "email_verified_at"])
    return player
```

**Points gate integration:** add a guard inside `award_points()`:

```python
# points/service.py — inside award_points()
if not player.email_verified:
    return  # silently skip; points accrue after verification
```

---

## §16.6 — Rate limiting

### `accounts/ratelimit.py`

```python
# accounts/ratelimit.py
from django.core.cache import cache
from django.http import HttpRequest


def check_rate_limit(request: HttpRequest, action: str, limit: int = 10, window: int = 3600) -> bool:
    """Return True if the request is within limits, False if rate-limited."""
    ip = request.META.get("HTTP_X_FORWARDED_FOR", request.META.get("REMOTE_ADDR", "unknown"))
    ip = ip.split(",")[0].strip()
    key = f"rl:{action}:{ip}"
    count = cache.get(key, 0)
    if count >= limit:
        return False
    cache.set(key, count + 1, window)
    return True
```

Applied to the magic link request view (10 per IP per hour) and signup view (5 per IP per hour).

---

## §16.7 — Views

### Login method picker: `GET /accounts/login/`

```python
# accounts/views.py

def login_page(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect(settings.LOGIN_REDIRECT_URL)
    return render(request, "accounts/login.html")
```

### Magic link request: `GET/POST /accounts/login/magic/request/`

Collects the email, looks up the player, and sends the sesame link. If the email is not found,
redirects to signup with the email pre-filled.

```python
def magic_link_request(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        return render(request, "accounts/magic_link_request.html")

    from .ratelimit import check_rate_limit
    if not check_rate_limit(request, "magic_link", limit=10):
        return render(request, "accounts/magic_link_request.html",
                      {"error": "Too many requests. Try again later."})

    email = request.POST.get("email", "").strip().lower()
    if not email:
        return render(request, "accounts/magic_link_request.html", {"error": "Email required."})

    try:
        player = Player.objects.get(email=email)
    except Player.DoesNotExist:
        return redirect(f"/accounts/signup/?email={email}")

    from .magic import send_magic_link
    send_magic_link(request, player)
    return render(request, "accounts/magic_link_sent.html", {"email": email})
```

### Magic link verify: `GET /accounts/login/magic/`

**This view is provided by `sesame.views.LoginView` — no custom view needed.**

Mount it directly in `urls.py`. It reads the `?sesame=` query parameter, validates the token,
calls `login()`, and redirects to `LOGIN_REDIRECT_URL`. On failure it returns a 403 by default;
override `get_invalid_link_url()` or provide a custom template at
`sesame/invalid_token.html` to render a friendly error page.

### Signup: `GET/POST /accounts/signup/`

```python
def signup(request: HttpRequest) -> HttpResponse:
    from .ratelimit import check_rate_limit
    if request.method == "GET":
        email = request.GET.get("email", "")
        return render(request, "accounts/signup.html", {"prefill_email": email})

    if not check_rate_limit(request, "signup", limit=5):
        return render(request, "accounts/signup.html",
                      {"error": "Too many requests. Try again later."})

    email = request.POST.get("email", "").strip().lower()
    display_name = request.POST.get("display_name", "").strip()
    country = request.POST.get("jurisdiction_country", "").strip()
    region = request.POST.get("jurisdiction_region", "").strip()

    if not email or not display_name:
        return render(request, "accounts/signup.html", {"error": "Name and email are required."})

    if Player.objects.filter(email=email).exists():
        return render(request, "accounts/signup.html",
                      {"error": "An account with that email already exists."})

    from .utils import generate_username
    player = Player.objects.create_user(
        username=generate_username(),
        email=email,
        password=None,
        display_name=display_name,
        jurisdiction_country=country,
        jurisdiction_region=region,
    )
    player.set_unusable_password()
    player.save()

    from .email_verification import send_verification_email
    send_verification_email(request, player)

    login(request, player, backend="django.contrib.auth.backends.ModelBackend")
    return redirect(settings.LOGIN_REDIRECT_URL)
```

### Email verification: `GET /accounts/verify-email/<token>/`

```python
def verify_email(request: HttpRequest, token: str) -> HttpResponse:
    from .email_verification import VerificationError, verify_email_token
    try:
        verify_email_token(token)
    except VerificationError as e:
        return render(request, "accounts/verify_email_error.html", {"error": str(e)})
    return render(request, "accounts/verify_email_success.html")
```

---

## §16.8 — Passkey (WebAuthn)

### `accounts/passkey.py`

The `py_webauthn` library handles the cryptographic heavy lifting. The browser side uses the
standard `navigator.credentials.create()` and `navigator.credentials.get()` APIs — no JS library
needed beyond a thin adapter.

```python
# accounts/passkey.py
import json

import webauthn
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from .models import PasskeyCredential, Player


def registration_options(player: Player) -> dict:
    existing = [
        webauthn.helpers.structs.PublicKeyCredentialDescriptor(
            id=bytes(c.credential_id)
        )
        for c in player.passkeys.all()
    ]
    options = webauthn.generate_registration_options(
        rp_id=settings.WEBAUTHN_RP_ID,
        rp_name=settings.WEBAUTHN_RP_NAME,
        user_id=str(player.pk).encode(),
        user_name=player.email,
        user_display_name=player.display_name or player.email,
        exclude_credentials=existing,
    )
    cache.set(f"webauthn_reg:{player.pk}", webauthn.options_to_json(options), 300)
    return json.loads(webauthn.options_to_json(options))


def verify_registration(player: Player, credential_json: str, device_name: str = "") -> PasskeyCredential:
    stored = cache.get(f"webauthn_reg:{player.pk}")
    if not stored:
        raise ValueError("Registration session expired.")
    expected = webauthn.options_from_json(stored)
    verification = webauthn.verify_registration_response(
        credential=webauthn.helpers.parse_cbor_authenticator_data(credential_json),
        expected_challenge=expected.challenge,
        expected_rp_id=settings.WEBAUTHN_RP_ID,
        expected_origin=settings.WEBAUTHN_ORIGIN,
    )
    return PasskeyCredential.objects.create(
        player=player,
        credential_id=verification.credential_id,
        public_key=verification.credential_public_key,
        sign_count=verification.sign_count,
        aaguid=str(verification.aaguid) if verification.aaguid else "",
        device_name=device_name,
    )


def authentication_options(email: str) -> dict:
    try:
        player = Player.objects.get(email=email)
    except Player.DoesNotExist:
        raise ValueError("No account found.")
    credentials = [
        webauthn.helpers.structs.PublicKeyCredentialDescriptor(id=bytes(c.credential_id))
        for c in player.passkeys.all()
    ]
    options = webauthn.generate_authentication_options(
        rp_id=settings.WEBAUTHN_RP_ID,
        allow_credentials=credentials,
    )
    cache.set(f"webauthn_auth:{player.pk}", webauthn.options_to_json(options), 300)
    cache.set(f"webauthn_auth_player:{options.challenge.hex()}", player.pk, 300)
    return json.loads(webauthn.options_to_json(options))


def verify_authentication(credential_json: str) -> Player:
    data = json.loads(credential_json)
    challenge_hex = data.get("challenge", "")
    player_pk = cache.get(f"webauthn_auth_player:{challenge_hex}")
    if not player_pk:
        raise ValueError("Authentication session expired.")
    player = Player.objects.get(pk=player_pk)
    stored = cache.get(f"webauthn_auth:{player.pk}")
    expected = webauthn.options_from_json(stored)
    credential_id = bytes.fromhex(data["rawId"])
    passkey = PasskeyCredential.objects.get(credential_id=credential_id, player=player)
    verification = webauthn.verify_authentication_response(
        credential=webauthn.helpers.parse_authentication_credential_json(credential_json),
        expected_challenge=expected.challenge,
        expected_rp_id=settings.WEBAUTHN_RP_ID,
        expected_origin=settings.WEBAUTHN_ORIGIN,
        credential_public_key=bytes(passkey.public_key),
        credential_current_sign_count=passkey.sign_count,
    )
    passkey.sign_count = verification.new_sign_count
    passkey.last_used_at = timezone.now()
    passkey.save(update_fields=["sign_count", "last_used_at"])
    return player
```

### Passkey views

Four JSON endpoints (consumed by Datastar/JS on the frontend):

```python
# accounts/views.py

@require_POST
@login_required
def passkey_register_options(request: HttpRequest) -> JsonResponse:
    from .passkey import registration_options
    return JsonResponse(registration_options(request.user))


@require_POST
@login_required
@csrf_exempt
def passkey_register_verify(request: HttpRequest) -> JsonResponse:
    from .passkey import verify_registration
    body = json.loads(request.body)
    try:
        verify_registration(request.user, json.dumps(body), body.get("deviceName", ""))
        return JsonResponse({"ok": True})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)


@require_POST
@csrf_exempt
def passkey_auth_options(request: HttpRequest) -> JsonResponse:
    from .passkey import authentication_options
    email = json.loads(request.body).get("email", "")
    try:
        return JsonResponse(authentication_options(email))
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)


@require_POST
@csrf_exempt
def passkey_auth_verify(request: HttpRequest) -> JsonResponse:
    from .passkey import verify_authentication
    try:
        player = verify_authentication(request.body.decode())
        login(request, player, backend="django.contrib.auth.backends.ModelBackend")
        return JsonResponse({"ok": True, "redirect": settings.LOGIN_REDIRECT_URL})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)
```

---

## §16.9 — Google / Apple via allauth

allauth handles the OAuth2 dance. We hook into its `social_account_added` signal to mark the
player as email-verified (Google/Apple already verified their email).

```python
# accounts/signals.py
from allauth.socialaccount.signals import social_account_added
from django.dispatch import receiver
from django.utils import timezone


@receiver(social_account_added)
def mark_social_player_verified(sender, request, sociallogin, **kwargs):
    player = sociallogin.user
    if not player.email_verified:
        player.email_verified = True
        player.email_verified_at = timezone.now()
        player.save(update_fields=["email_verified", "email_verified_at"])
```

```python
# accounts/apps.py
from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "accounts"

    def ready(self) -> None:
        import accounts.signals  # noqa: F401
```

allauth needs an adapter to auto-generate usernames (since `ACCOUNT_USERNAME_REQUIRED = False`):

```python
# accounts/adapters.py
from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter

from .utils import generate_username


class AccountAdapter(DefaultAccountAdapter):
    def populate_username(self, request, user):
        if not user.username:
            user.username = generate_username()


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    def populate_user(self, request, sociallogin, data):
        user = super().populate_user(request, sociallogin, data)
        if not user.username:
            user.username = generate_username()
        if not user.display_name and data.get("name"):
            user.display_name = data["name"]
        return user
```

---

## §16.10 — URL configuration

```python
# accounts/urls.py
from django.contrib.auth import views as auth_views
from django.urls import include, path
from sesame.views import LoginView as SesameLoginView

from . import views

app_name = "accounts"

urlpatterns = [
    # Login method picker (replaces Django's LoginView)
    path("login/", views.login_page, name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),

    # Magic link — request (our view) + verify (sesame's LoginView)
    path("login/magic/request/", views.magic_link_request, name="magic_link_request"),
    path("login/magic/", SesameLoginView.as_view(), name="magic_link_verify"),

    # Signup
    path("signup/", views.signup, name="signup"),

    # Email verification
    path("verify-email/<str:token>/", views.verify_email, name="verify_email"),

    # Passkey
    path("passkey/register/options/", views.passkey_register_options, name="passkey_register_options"),
    path("passkey/register/verify/", views.passkey_register_verify, name="passkey_register_verify"),
    path("passkey/auth/options/", views.passkey_auth_options, name="passkey_auth_options"),
    path("passkey/auth/verify/", views.passkey_auth_verify, name="passkey_auth_verify"),

    # Google + Apple (allauth)
    path("", include("allauth.urls")),
]
```

The sesame `LoginView` reads `?sesame=<token>` from the query string — the link sent by
`send_magic_link()` is `https://example.com/accounts/login/magic/?sesame=<token>`.

---

## §16.11 — Templates

| Template | Purpose |
|---|---|
| `accounts/login.html` | Method picker: Google button, Apple button, passkey button, email input |
| `accounts/magic_link_request.html` | Email input form (POSTs to `/accounts/login/magic/request/`) |
| `accounts/magic_link_sent.html` | "Check your inbox" confirmation |
| `sesame/invalid_token.html` | Sesame's invalid/expired token error page (overrides sesame's default 403) |
| `accounts/signup.html` | Three-field form: display name, email, jurisdiction |
| `accounts/verify_email_success.html` | "Email verified" confirmation |
| `accounts/verify_email_error.html` | Verification token error |

Note: `sesame/invalid_token.html` is the template sesame checks for when a token is invalid.
Creating it in our `templates/` directory overrides sesame's default bare 403 response.

### Jurisdiction dropdowns

```html
<!-- accounts/signup.html (jurisdiction block) -->
<select name="jurisdiction_country" id="id_country">
  <option value="">Select country</option>
  <option value="US">United States</option>
  <option value="CA">Canada</option>
  <option value="GB">United Kingdom</option>
  <option value="AU">Australia</option>
  <option value="NZ">New Zealand</option>
</select>
<select name="jurisdiction_region" id="id_region">
  <option value="">Select region (optional)</option>
</select>
<script>
  const lang = navigator.language || '';
  const country = lang.split('-')[1] || '';
  if (country) {
    const sel = document.getElementById('id_country');
    for (const opt of sel.options) {
      if (opt.value === country) { opt.selected = true; break; }
    }
  }
</script>
```

Region data (US states, Canadian provinces, etc.) is a static JS object filtered on country
change. Kept client-side to avoid a round-trip.

---

## §16.12 — Migration

```python
# accounts/migrations/0003_player_auth_fields.py
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0002_alter_player_sqid")]

    operations = [
        migrations.AddField(
            model_name="player",
            name="display_name",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="player",
            name="email_verified",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="player",
            name="email_verified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="player",
            name="jurisdiction_country",
            field=models.CharField(blank=True, max_length=2),
        ),
        migrations.AddField(
            model_name="player",
            name="jurisdiction_region",
            field=models.CharField(blank=True, max_length=10),
        ),
        migrations.AlterField(
            model_name="player",
            name="email",
            field=models.EmailField(max_length=254, unique=True, verbose_name="email address"),
        ),
        migrations.CreateModel(
            name="EmailVerification",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("token_hash", models.CharField(max_length=64, unique=True)),
                ("expires_at", models.DateTimeField()),
                ("verified_at", models.DateTimeField(blank=True, null=True)),
                ("player", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="email_verifications", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.AddIndex(
            model_name="emailverification",
            index=models.Index(fields=["token_hash"], name="accounts_em_token_h_idx"),
        ),
        migrations.CreateModel(
            name="PasskeyCredential",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("credential_id", models.BinaryField(unique=True)),
                ("public_key", models.BinaryField()),
                ("sign_count", models.PositiveIntegerField(default=0)),
                ("aaguid", models.CharField(blank=True, max_length=36)),
                ("device_name", models.CharField(blank=True, max_length=100)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                ("player", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="passkeys", to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]
```

`MagicLinkToken` is absent — sesame is stateless and requires no migration.

---

## §16.13 — Tests

### Magic link (sesame integration)

The cryptographic correctness of sesame tokens is tested by the sesame library itself. Our tests
verify the integration: that `send_magic_link` generates a URL containing a valid sesame token, and
that sesame's `LoginView` accepts it and logs the player in.

```python
# accounts/tests.py

@pytest.mark.django_db
def test_magic_link_url_contains_sesame_token(player, rf):
    from accounts.magic import send_magic_link
    from django.core import mail
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
    client.get(f"/accounts/login/magic/{token_qs}")   # first use — logs in, updates last_login
    client.logout()
    resp = client.get(f"/accounts/login/magic/{token_qs}")  # second use — invalid
    assert resp.status_code in (302, 403)   # sesame redirects or 403s on invalid token
    assert "_auth_user_id" not in client.session
```

### Email verification

```python
@pytest.mark.django_db
def test_email_verification_sets_verified(player):
    import hashlib
    from accounts.email_verification import verify_email_token
    from accounts.models import EmailVerification
    from datetime import timedelta
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
    import hashlib
    from accounts.email_verification import VerificationError, verify_email_token
    from accounts.models import EmailVerification
    from datetime import timedelta
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
    import hashlib
    from accounts.email_verification import VerificationError, verify_email_token
    from accounts.models import EmailVerification
    from datetime import timedelta
    raw = "testtoken3"
    EmailVerification.objects.create(
        player=player,
        token_hash=hashlib.sha256(raw.encode()).hexdigest(),
        expires_at=timezone.now() - timedelta(hours=1),
    )
    with pytest.raises(VerificationError, match="expired"):
        verify_email_token(raw)
```

### Rate limiting

```python
@pytest.mark.django_db
def test_rate_limit_blocks_after_limit(rf):
    from accounts.ratelimit import check_rate_limit
    from django.core.cache import cache
    cache.clear()
    request = rf.get("/", REMOTE_ADDR="1.2.3.4")
    for _ in range(10):
        assert check_rate_limit(request, "test_action", limit=10) is True
    assert check_rate_limit(request, "test_action", limit=10) is False
```

### Signup view

```python
@pytest.mark.django_db
def test_signup_creates_player_with_unusable_password(client):
    from django.core.cache import cache
    cache.clear()
    resp = client.post("/accounts/signup/", {
        "email": "new@example.com",
        "display_name": "New Player",
        "jurisdiction_country": "US",
        "jurisdiction_region": "CA",
    })
    assert resp.status_code == 302
    from accounts.models import Player
    p = Player.objects.get(email="new@example.com")
    assert p.display_name == "New Player"
    assert not p.has_usable_password()
    assert p.email_verified is False  # verification email sent but not yet clicked
```

---

## File summary

| File | Action |
|---|---|
| `accounts/models.py` | Add `display_name`, `email_verified`, `email_verified_at`, `jurisdiction_*` to `Player`; override `email` for uniqueness; add `EmailVerification`, `PasskeyCredential` |
| `accounts/migrations/0003_player_auth_fields.py` | New migration (no `MagicLinkToken` — sesame is stateless) |
| `accounts/utils.py` | New — `generate_username()` |
| `accounts/magic.py` | New — thin `send_magic_link()` wrapper around `sesame.utils.get_query_string` |
| `accounts/email_verification.py` | New — email verification service |
| `accounts/ratelimit.py` | New — cache-based rate limiter |
| `accounts/passkey.py` | New — WebAuthn registration + authentication |
| `accounts/adapters.py` | New — allauth adapters for username generation + display name |
| `accounts/signals.py` | New — mark social login players as email-verified |
| `accounts/apps.py` | Update — import signals in `ready()` |
| `accounts/views.py` | Replace stub — `login_page`, `magic_link_request`, `signup`, `verify_email`, four passkey endpoints |
| `accounts/urls.py` | Replace — wire all new routes; `SesameLoginView` for magic link verify; include allauth |
| `accounts/admin.py` | Register `EmailVerification`, `PasskeyCredential` |
| `accounts/tests.py` | New — magic link/sesame (3), email verification (3), rate limit (1), signup (1) |
| `hf/settings/base.py` | Add allauth + sesame apps, middleware, backends, settings |
| `points/service.py` | Add `email_verified` guard inside `award_points()` |
| `templates/accounts/login.html` | Method picker |
| `templates/accounts/magic_link_request.html` | Email form |
| `templates/accounts/magic_link_sent.html` | "Check your inbox" |
| `templates/sesame/invalid_token.html` | Friendly error page for invalid/expired sesame tokens |
| `templates/accounts/signup.html` | Three-field signup form |
| `templates/accounts/verify_email_success.html` | Verification confirmed |
| `templates/accounts/verify_email_error.html` | Verification error |
| `pyproject.toml` | Add `django-sesame`, `django-allauth[socialaccount]`, `py_webauthn` |

---

## Sequencing notes

Implement in this order to keep the app in a working state at every step:

1. `pyproject.toml` — install all three packages
2. Migration + model additions (Player fields + `EmailVerification` + `PasskeyCredential`)
3. `utils.py`, `ratelimit.py`
4. Settings — sesame + allauth blocks in `base.py`
5. `accounts/magic.py` (thin) + `magic_link_request` view + sesame `LoginView` wired in URLs + templates → magic link login fully functional
6. `email_verification.py` + `verify_email` view + template + points gate guard → email verification fully functional
7. allauth adapters + signals + `apps.py` → Google login functional
8. Apple credentials configured → Apple login functional
9. `passkey.py` + passkey views + URLs → passkey registration + authentication functional
10. Tests

---

## Status: COMPLETE ✓

All items implemented and tested. 48 tests pass (1 skipped).

- [x] §16.1 Player model additions (`display_name`, `email_verified`, `email_verified_at`, `jurisdiction_*`, unique `email`)
- [x] §16.2 `EmailVerification` + `PasskeyCredential` models
- [x] §16.3 Settings (`hf/settings/base.py` — sesame, allauth, sites, passkey)
- [x] §16.4 Magic link via django-sesame (`accounts/magic.py`, `MagicLinkVerifyView`)
- [x] §16.5 Email verification service (`accounts/email_verification.py`, `verify_email` view + templates)
- [x] §16.6 Rate limiting (`accounts/ratelimit.py`)
- [x] §16.7 Views (`login_page`, `magic_link_request`, `signup`, `verify_email`, 4 passkey endpoints)
- [x] §16.8 Passkey/WebAuthn (`accounts/passkey.py`)
- [x] §16.9 allauth adapters + `social_account_added` signal
- [x] §16.10 URL configuration (`accounts/urls.py`, `hf/urls.py`)
- [x] §16.11 Templates (login, magic link request/sent/error, sesame invalid token, signup, verify email success/error)
- [x] §16.12 Migration `0003_player_auth_fields.py` applied
- [x] §16.13 Tests (8 tests in `accounts/tests.py`)
- [x] `accounts/admin.py` updated (EmailVerification, PasskeyCredential registered)
- [x] `points/service.py` email_verified guard added
- [x] `conftest.py` player fixture updated (unique email, `email_verified=True`)
- [x] `core/tests.py` fixed (unique emails in test player creation)
- [x] `django.manage check` — 0 issues
- [x] `ruff check` — clean
