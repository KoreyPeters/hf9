import json

from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from sesame.views import LoginView as SesameLoginView

from .models import Player


class MagicLinkVerifyView(SesameLoginView):
    def login_failed(self) -> HttpResponse:
        return render(
            self.request,
            "accounts/magic_link_error.html",
            {"error": "This login link is invalid or has already been used."},
        )


def login_page(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect(settings.LOGIN_REDIRECT_URL)
    return render(request, "accounts/login.html")


def magic_link_request(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        return render(request, "accounts/magic_link_request.html")

    from .ratelimit import check_rate_limit
    if not check_rate_limit(request, "magic_link", limit=10):
        return render(
            request,
            "accounts/magic_link_request.html",
            {"error": "Too many requests. Try again later."},
        )

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


def signup(request: HttpRequest) -> HttpResponse:
    from .ratelimit import check_rate_limit

    if request.method == "GET":
        email = request.GET.get("email", "")
        return render(request, "accounts/signup.html", {"prefill_email": email})

    if not check_rate_limit(request, "signup", limit=5):
        return render(
            request,
            "accounts/signup.html",
            {"error": "Too many requests. Try again later."},
        )

    email = request.POST.get("email", "").strip().lower()
    display_name = request.POST.get("display_name", "").strip()
    country = request.POST.get("jurisdiction_country", "").strip()
    region = request.POST.get("jurisdiction_region", "").strip()

    if not email or not display_name:
        return render(
            request,
            "accounts/signup.html",
            {"error": "Name and email are required.", "prefill_email": email},
        )

    if Player.objects.filter(email=email).exists():
        return render(
            request,
            "accounts/signup.html",
            {"error": "An account with that email already exists.", "prefill_email": email},
        )

    from .utils import generate_username
    player = Player.objects.create_user(
        username=generate_username(),
        email=email,
        password=None,
        display_name=display_name,
        jurisdiction_country=country,
        jurisdiction_region=region,
    )

    from .email_verification import send_verification_email
    send_verification_email(request, player)

    login(request, player, backend="django.contrib.auth.backends.ModelBackend")
    return redirect(settings.LOGIN_REDIRECT_URL)


def verify_email(request: HttpRequest, token: str) -> HttpResponse:
    from .email_verification import VerificationError, verify_email_token
    try:
        verify_email_token(token)
    except VerificationError as e:
        return render(request, "accounts/verify_email_error.html", {"error": str(e)})
    return render(request, "accounts/verify_email_success.html")


@csrf_exempt
@require_POST
@login_required
def passkey_register_options(request: HttpRequest) -> JsonResponse:
    from .passkey import registration_options
    return JsonResponse(registration_options(request.user))


@csrf_exempt
@require_POST
@login_required
def passkey_register_verify(request: HttpRequest) -> JsonResponse:
    from .passkey import verify_registration
    body = json.loads(request.body)
    try:
        verify_registration(request.user, json.dumps(body), body.get("deviceName", ""))
        return JsonResponse({"ok": True})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)


@csrf_exempt
@require_POST
def passkey_auth_options(request: HttpRequest) -> JsonResponse:
    from .passkey import authentication_options
    email = json.loads(request.body).get("email", "")
    try:
        return JsonResponse(authentication_options(email))
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)


@csrf_exempt
@require_POST
def passkey_auth_verify(request: HttpRequest) -> JsonResponse:
    from .passkey import verify_authentication
    try:
        player = verify_authentication(request.body.decode())
        login(request, player, backend="django.contrib.auth.backends.ModelBackend")
        return JsonResponse({"ok": True, "redirect": settings.LOGIN_REDIRECT_URL})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)
