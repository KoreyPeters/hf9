import json
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
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
        return render(
            request, "accounts/magic_link_request.html", {"error": "Email required."}
        )

    try:
        player = Player.objects.get(email=email)
    except Player.DoesNotExist:
        return redirect(f"/accounts/signup/?email={email}")

    from .magic import send_magic_link

    send_magic_link(request, player)
    return render(request, "accounts/magic_link_sent.html", {"email": email})


def _signup_page(request: HttpRequest, **context: object) -> HttpResponse:
    """Render the signup form, always carrying the Turnstile site key.

    Every error path re-renders this form, and a render that forgot the key
    would drop the widget — leaving a form that looks fine, submits without a
    token, and is refused. Centralised so that cannot happen one branch at a
    time.
    """
    return render(
        request,
        "accounts/signup.html",
        {"turnstile_site_key": settings.TURNSTILE_SITE_KEY, **context},
    )


def signup(request: HttpRequest) -> HttpResponse:
    from .ratelimit import check_rate_limit

    if request.method == "GET":
        email = request.GET.get("email", "")
        return _signup_page(request, prefill_email=email)

    if not check_rate_limit(request, "signup", limit=5):
        return _signup_page(request, error="Too many requests. Try again later.")

    email = request.POST.get("email", "").strip().lower()
    display_name = request.POST.get("display_name", "").strip()
    country = request.POST.get("jurisdiction_country", "").strip()
    region = request.POST.get("jurisdiction_region", "").strip()

    # Checked before the account is created, and so before anything is mailed.
    # That ordering is the entire point: a signup sends a verification email to
    # an address nobody has proved they own, so an unchallenged bot does not
    # merely make a junk account — it makes us mail a stranger, repeatedly.
    from . import turnstile

    if not turnstile.verify(
        request.POST.get("cf-turnstile-response", ""),
        remote_ip=request.META.get("REMOTE_ADDR", ""),
    ):
        return _signup_page(
            request,
            error="We could not confirm you are a person. Please try again.",
            prefill_email=email,
        )

    if not email or not display_name:
        return _signup_page(
            request,
            error="Name and email are required.",
            prefill_email=email,
        )

    if Player.objects.filter(email=email).exists():
        return _signup_page(
            request,
            error="An account with that email already exists.",
            prefill_email=email,
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

    from datetime import timedelta
    from django.utils import timezone
    from core.tasks import enqueue

    enqueue(
        "verify-email-reminder",
        {"player_id": player.pk},
        schedule_time=timezone.now() + timedelta(days=7),
    )

    login(request, player, backend="django.contrib.auth.backends.ModelBackend")
    return redirect("accounts:welcome")


@login_required
def welcome(request: HttpRequest) -> HttpResponse:
    return render(request, "accounts/welcome.html")


@login_required
@require_POST
def resend_verification(request: HttpRequest) -> HttpResponse:
    from .ratelimit import check_rate_limit

    if not check_rate_limit(request, "resend_verification", limit=3):
        return render(
            request,
            "accounts/verify_email_resent.html",
            {
                "error": "Too many requests. Try again later.",
                "email": request.user.email,
            },
        )
    if request.user.email_verified:
        return redirect(settings.LOGIN_REDIRECT_URL)
    from .email_verification import send_verification_email

    send_verification_email(request, request.user)
    return render(
        request, "accounts/verify_email_resent.html", {"email": request.user.email}
    )


def verify_email(request: HttpRequest, token: str) -> HttpResponse:
    from .email_verification import VerificationError, verify_email_token

    try:
        verify_email_token(token)
    except VerificationError as e:
        return render(request, "accounts/verify_email_error.html", {"error": str(e)})
    return render(request, "accounts/verify_email_success.html")


def player_profile(request: HttpRequest, sqid: str) -> HttpResponse:
    from django.contrib.contenttypes.models import ContentType
    from points.models import PointTransaction
    from polium.models import Candidate, VoteDeclaration
    from surveys.models import SurveyResponse

    profile_player = get_object_or_404(Player, sqid=sqid)
    is_own_profile = (
        request.user.is_authenticated and request.user.pk == profile_player.pk
    )

    ctx: dict[str, object] = {
        "profile_player": profile_player,
        "is_own_profile": is_own_profile,
    }

    if not is_own_profile:
        return render(request, "accounts/profile.html", ctx)

    candidate_ct = ContentType.objects.get_for_model(Candidate)
    survey_response_ct = ContentType.objects.get_for_model(SurveyResponse)
    vote_declaration_ct = ContentType.objects.get_for_model(VoteDeclaration)

    # Candidate surveys
    survey_responses = list(
        SurveyResponse.objects.filter(
            player=profile_player, content_type=candidate_ct
        ).order_by("-submitted_at")
    )
    sr_ids = [sr.pk for sr in survey_responses]
    candidate_ids = [sr.object_id for sr in survey_responses]

    candidates_by_id: dict[int, Candidate] = {
        c.pk: c
        for c in Candidate.objects.filter(pk__in=candidate_ids).only(
            "pk", "name", "office", "sqid"
        )
    }
    sr_points: dict[int, Decimal] = {
        row["object_id"]: row["total"]
        for row in PointTransaction.objects.filter(
            player=profile_player,
            content_type=survey_response_ct,
            object_id__in=sr_ids,
        )
        .values("object_id")
        .annotate(total=Sum("amount"))
    }

    survey_rows: list[dict[str, object]] = []
    for sr in survey_responses:
        candidate = candidates_by_id.get(sr.object_id)
        if candidate is None:
            continue
        survey_rows.append(
            {
                "candidate": candidate,
                "submitted_at": sr.submitted_at,
                "submit_count": sr.submit_count,
                "points_total": sr_points.get(sr.pk),
            }
        )

    # Vote declarations
    declarations = list(
        VoteDeclaration.objects.filter(player=profile_player)
        .select_related("candidate", "election")
        .order_by("-declared_at")
    )
    decl_ids = [d.pk for d in declarations]
    decl_points: dict[int, Decimal] = {
        pt.object_id: pt.amount
        for pt in PointTransaction.objects.filter(
            player=profile_player,
            content_type=vote_declaration_ct,
            object_id__in=decl_ids,
        )
    }
    declaration_rows: list[dict[str, object]] = [
        {"declaration": d, "points": decl_points.get(d.pk)} for d in declarations
    ]

    # Points history — last 50, with source URL resolved
    transactions = list(
        PointTransaction.objects.filter(player=profile_player).order_by("-created_at")[
            :50
        ]
    )

    sr_tx_ids = [
        t.object_id
        for t in transactions
        if t.content_type_id == survey_response_ct.pk and t.object_id is not None
    ]
    sr_to_candidate_sqid: dict[int, str] = {}
    if sr_tx_ids:
        srs = list(
            SurveyResponse.objects.filter(pk__in=sr_tx_ids, content_type=candidate_ct)
        )
        cand_sqids = {
            c.pk: c.sqid
            for c in Candidate.objects.filter(pk__in=[s.object_id for s in srs]).only(
                "pk", "sqid"
            )
        }
        for sr in srs:
            sqid_val = cand_sqids.get(sr.object_id)
            if sqid_val:
                sr_to_candidate_sqid[sr.pk] = sqid_val

    decl_tx_ids = [
        t.object_id
        for t in transactions
        if t.content_type_id == vote_declaration_ct.pk and t.object_id is not None
    ]
    decl_to_election_sqid: dict[int, str] = {}
    if decl_tx_ids:
        for d in VoteDeclaration.objects.filter(pk__in=decl_tx_ids).select_related(
            "election"
        ):
            if d.election:
                decl_to_election_sqid[d.pk] = d.election.sqid

    reason_labels: dict[str, str] = {
        "survey": "Candidate survey",
        "vote_declaration": "Vote declaration",
    }

    tx_rows: list[dict[str, object]] = []
    for t in transactions:
        source_url: str | None = None
        if t.content_type_id == survey_response_ct.pk and t.object_id is not None:
            cand_sqid_val = sr_to_candidate_sqid.get(t.object_id)
            if cand_sqid_val:
                source_url = reverse("polium:candidate_detail", args=[cand_sqid_val])
        elif t.content_type_id == vote_declaration_ct.pk and t.object_id is not None:
            election_sqid_val = decl_to_election_sqid.get(t.object_id)
            if election_sqid_val:
                source_url = reverse("polium:election_detail", args=[election_sqid_val])
        tx_rows.append(
            {
                "label": reason_labels.get(t.reason, t.reason),
                "amount": t.amount,
                "created_at": t.created_at,
                "source_url": source_url,
            }
        )

    ctx.update(
        {
            "survey_rows": survey_rows,
            "declaration_rows": declaration_rows,
            "tx_rows": tx_rows,
        }
    )
    return render(request, "accounts/profile.html", ctx)


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
