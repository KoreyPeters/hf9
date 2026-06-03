from datetime import date
from decimal import Decimal

from datastar_py.django import DatastarResponse, ServerSentEventGenerator, read_signals
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.db.models import F
from django.db.models.functions import Greatest
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.views.decorators.http import require_POST

from core.maturity import account_is_mature
from evidence.models import Evidence, EvidenceFlag
from evidence.service import AlreadyFlaggedError, NotMatureError, flag_evidence, submit_evidence, vote_usefulness
from surveys.models import Criterion

from . import service
from .models import Candidate, Election, Jurisdiction, JurisdictionDuplicateFlag, JurisdictionFollow, VoteDeclaration

_VALID_LEVELS = {"country", "province", "region", "city", "other"}

LEVEL_LABELS: dict[str, str] = {
    "country": "Country",
    "province": "State / Province / Territory",
    "region": "County / District / Region",
    "city": "City / Municipality",
    "other": "Other",
}


def _elections_ctx(jurisdiction: Jurisdiction, show_form: bool = False, error: str = "") -> dict[str, object]:
    today = date.today()
    return {
        "jurisdiction": jurisdiction,
        "upcoming_elections": list(jurisdiction.elections.filter(election_date__gte=today).order_by("election_date")[:10]),
        "past_elections": list(jurisdiction.elections.filter(election_date__lt=today).order_by("-election_date")[:5]),
        "elections_for_form": list(jurisdiction.elections.order_by("-election_date")),
        "is_active": jurisdiction.status == Jurisdiction.STATUS_ACTIVE,
        "show_form": show_form,
        "error": error,
    }


def _candidates_ctx(jurisdiction: Jurisdiction, show_form: bool = False, error: str = "") -> dict[str, object]:
    return {
        "jurisdiction": jurisdiction,
        "candidates": list(jurisdiction.candidates.order_by("is_blacklisted", "-current_rating")),
        "elections_for_form": list(jurisdiction.elections.order_by("-election_date")),
        "is_active": jurisdiction.status == Jurisdiction.STATUS_ACTIVE,
        "show_form": show_form,
        "error": error,
    }


def _get_descendant_ids(jurisdiction_id: int) -> set[int]:
    all_ids: set[int] = {jurisdiction_id}
    queue = [jurisdiction_id]
    while queue:
        parent_id = queue.pop()
        child_ids = list(
            Jurisdiction.objects.filter(parent_id=parent_id).values_list("id", flat=True)
        )
        new_ids = [cid for cid in child_ids if cid not in all_ids]
        all_ids.update(new_ids)
        queue.extend(new_ids)
    return all_ids


def polium_home(request: HttpRequest) -> HttpResponse:
    today = date.today()

    if not request.user.is_authenticated:
        upcoming = (
            Election.objects.filter(election_date__gte=today)
            .select_related("jurisdiction")
            .order_by("election_date")[:20]
        )
        return render(request, "polium/home.html", {
            "upcoming_elections": upcoming,
            "state": "anonymous",
        })

    follows = list(
        request.user.followed_jurisdictions.select_related("jurisdiction").all()
    )

    if not follows:
        return render(request, "polium/home.html", {"state": "no_follows"})

    jurisdiction_ids: set[int] = set()
    for follow in follows:
        if follow.depth == JurisdictionFollow.DEPTH_ALL:
            jurisdiction_ids.update(_get_descendant_ids(follow.jurisdiction_id))
        else:
            jurisdiction_ids.add(follow.jurisdiction_id)

    upcoming = (
        Election.objects.filter(election_date__gte=today, jurisdiction_id__in=jurisdiction_ids)
        .select_related("jurisdiction")
        .order_by("election_date")[:20]
    )

    state = "populated" if upcoming.exists() else "no_elections"
    return render(request, "polium/home.html", {
        "upcoming_elections": upcoming,
        "followed_jurisdictions": [f.jurisdiction for f in follows],
        "state": state,
    })


def jurisdiction_search(request: HttpRequest) -> DatastarResponse:
    signals = read_signals(request) or {}
    q = signals.get("q", "").strip()
    results: list[dict[str, str]] = []
    if len(q) >= 2:
        results = list(
            Jurisdiction.objects.filter(
                name__icontains=q,
                status=Jurisdiction.STATUS_ACTIVE,
            ).values("sqid", "name", "level")[:10]
        )
    html = render_to_string(
        "polium/partials/search_results.html",
        {"results": results, "q": q},
        request=request,
    )
    return DatastarResponse(ServerSentEventGenerator.patch_elements(html, selector="#search-results"))


def jurisdiction_create_form(request: HttpRequest) -> DatastarResponse:
    signals = read_signals(request) or {}
    name = signals.get("q", "").strip()
    html = render_to_string(
        "polium/partials/create_form.html",
        {"name": name},
        request=request,
    )
    return DatastarResponse(ServerSentEventGenerator.patch_elements(html, selector="#search-results"))


def jurisdiction_search_parent(request: HttpRequest) -> DatastarResponse:
    signals = read_signals(request) or {}
    q = signals.get("parent_q", "").strip()
    results: list[dict[str, str]] = []
    if len(q) >= 2:
        results = list(
            Jurisdiction.objects.filter(
                name__icontains=q,
                status=Jurisdiction.STATUS_ACTIVE,
            ).values("sqid", "name", "level")[:10]
        )
    html = render_to_string(
        "polium/partials/parent_results.html",
        {"results": results},
        request=request,
    )
    return DatastarResponse(ServerSentEventGenerator.patch_elements(html, selector="#parent-results"))


@login_required
@require_POST
def create_jurisdiction(request: HttpRequest) -> HttpResponse:
    name = request.POST.get("name", "").strip()
    level = request.POST.get("level", "").strip()
    parent_sqid = request.POST.get("parent_sqid", "").strip()

    if not name or level not in _VALID_LEVELS:
        messages.error(request, "Please provide a name and select a level.")
        return redirect("polium:home")

    parent = None
    if parent_sqid:
        parent = Jurisdiction.objects.filter(sqid=parent_sqid, status=Jurisdiction.STATUS_ACTIVE).first()

    jurisdiction = Jurisdiction.objects.create(
        name=name,
        level=level,
        parent=parent,
        created_by=request.user,
        active_engagement=1,
    )
    JurisdictionFollow.objects.create(
        player=request.user,
        jurisdiction=jurisdiction,
        depth=JurisdictionFollow.DEPTH_ALL,
    )
    return redirect("polium:home")


@login_required
@require_POST
def follow_jurisdiction(request: HttpRequest) -> HttpResponse:
    sqid = request.POST.get("jurisdiction_sqid", "")
    depth = request.POST.get("depth", JurisdictionFollow.DEPTH_ALL)
    if depth not in (JurisdictionFollow.DEPTH_THIS, JurisdictionFollow.DEPTH_ALL):
        depth = JurisdictionFollow.DEPTH_ALL
    jurisdiction = get_object_or_404(Jurisdiction, sqid=sqid)
    _, created = JurisdictionFollow.objects.get_or_create(
        player=request.user,
        jurisdiction=jurisdiction,
        defaults={"depth": depth},
    )
    if created:
        Jurisdiction.objects.filter(pk=jurisdiction.pk).update(
            active_engagement=F("active_engagement") + 1
        )
    return redirect("polium:home")


@login_required
@require_POST
def unfollow_jurisdiction(request: HttpRequest) -> HttpResponse:
    sqid = request.POST.get("jurisdiction_sqid", "")
    jurisdiction = get_object_or_404(Jurisdiction, sqid=sqid)
    deleted, _ = JurisdictionFollow.objects.filter(
        player=request.user,
        jurisdiction=jurisdiction,
    ).delete()
    if deleted:
        Jurisdiction.objects.filter(pk=jurisdiction.pk).update(
            active_engagement=Greatest(F("active_engagement") - 1, 0)
        )
    return redirect("polium:home")


def candidate_detail(request: HttpRequest, sqid: str) -> HttpResponse:
    candidate = get_object_or_404(Candidate, sqid=sqid)
    ct = ContentType.objects.get_for_model(Candidate)
    evidence_qs = (
        Evidence.objects.filter(
            content_type=ct,
            object_id=candidate.pk,
            status=Evidence.STATUS_VISIBLE,
        )
        .select_related("submitted_by", "criterion")
        .order_by("-net_usefulness_score", "-submitted_at")
    )
    blacklist_record = (
        candidate.blacklist_history.order_by("-blacklisted_at").first()
        if candidate.is_blacklisted else None
    )
    criteria = Criterion.objects.filter(is_active=True).order_by("category__name", "question")
    elections_for_form: list[Election] = []
    if candidate.jurisdiction_id:
        elections_for_form = list(
            Election.objects.filter(jurisdiction=candidate.jurisdiction).order_by("-election_date")
        )
    return render(request, "polium/candidate_profile.html", {
        "candidate": candidate,
        "evidence_list": evidence_qs,
        "blacklist_record": blacklist_record,
        "criteria": criteria,
        "flag_reasons": EvidenceFlag.REASON_CHOICES,
        "elections_for_form": elections_for_form,
    })


@login_required
@require_POST
def evidence_submit(request: HttpRequest, sqid: str) -> HttpResponse:
    candidate = get_object_or_404(Candidate, sqid=sqid)
    url = request.POST.get("url", "").strip()
    note = request.POST.get("note", "").strip()
    criterion_id = request.POST.get("criterion_id") or None
    criterion = get_object_or_404(Criterion, pk=criterion_id) if criterion_id else None
    if url and note:
        submit_evidence(request.user, candidate, url, note, criterion)
    return redirect("polium:candidate_detail", sqid=sqid)


@login_required
@require_POST
def evidence_vote(request: HttpRequest, pk: int) -> HttpResponse:
    evidence = get_object_or_404(Evidence, pk=pk)
    is_useful = request.POST.get("is_useful") == "true"
    vote_usefulness(request.user, evidence, is_useful)
    return redirect(request.META.get("HTTP_REFERER", "/"))


@login_required
@require_POST
def evidence_flag(request: HttpRequest, pk: int) -> HttpResponse:
    evidence = get_object_or_404(Evidence, pk=pk)
    reason = request.POST.get("reason", EvidenceFlag.REASON_IRRELEVANT)
    try:
        flag_evidence(request.user, evidence, reason)
    except NotMatureError:
        messages.error(request, "Your account must be at least 7 days old with 3 surveys submitted to flag evidence.")
    except AlreadyFlaggedError:
        messages.error(request, "You have already flagged this evidence.")
    return redirect(request.META.get("HTTP_REFERER", "/"))


def _candidate_election_ctx(candidate: Candidate, show_form: bool, error: str = "") -> dict[str, object]:
    elections_for_form: list[Election] = []
    if candidate.jurisdiction_id:
        elections_for_form = list(
            Election.objects.filter(jurisdiction=candidate.jurisdiction).order_by("-election_date")
        )
    return {
        "candidate": candidate,
        "elections_for_form": elections_for_form,
        "show_form": show_form,
        "error": error,
    }


def candidate_election_section(request: HttpRequest, sqid: str) -> DatastarResponse:
    candidate = get_object_or_404(Candidate, sqid=sqid)
    html = render_to_string(
        "polium/partials/candidate_election_section.html",
        _candidate_election_ctx(candidate, show_form=False),
        request=request,
    )
    return DatastarResponse(ServerSentEventGenerator.patch_elements(html, selector="#candidate-election-section"))


@login_required
def candidate_link_election_form(request: HttpRequest, sqid: str) -> DatastarResponse:
    candidate = get_object_or_404(Candidate, sqid=sqid)
    html = render_to_string(
        "polium/partials/candidate_election_section.html",
        _candidate_election_ctx(candidate, show_form=True),
        request=request,
    )
    return DatastarResponse(ServerSentEventGenerator.patch_elements(html, selector="#candidate-election-section"))


@login_required
@require_POST
def candidate_link_election(request: HttpRequest, sqid: str) -> DatastarResponse:
    candidate = get_object_or_404(Candidate, sqid=sqid)
    signals = read_signals(request) or {}
    election_id_str = str(signals.get("candidate_election_id", "")).strip()

    error = ""
    election: Election | None = None

    if election_id_str:
        try:
            eid = int(election_id_str)
            election = Election.objects.filter(pk=eid, jurisdiction=candidate.jurisdiction).first()
            if election is None:
                error = "Selected election does not belong to this jurisdiction."
        except ValueError:
            error = "Invalid election selection."

    if error:
        html = render_to_string(
            "polium/partials/candidate_election_section.html",
            _candidate_election_ctx(candidate, show_form=True, error=error),
            request=request,
        )
        return DatastarResponse(ServerSentEventGenerator.patch_elements(html, selector="#candidate-election-section"))

    candidate.election = election
    candidate.save(update_fields=["election"])

    html = render_to_string(
        "polium/partials/candidate_election_section.html",
        _candidate_election_ctx(candidate, show_form=False),
        request=request,
    )
    return DatastarResponse([
        ServerSentEventGenerator.patch_elements(html, selector="#candidate-election-section"),
        ServerSentEventGenerator.patch_signals({"candidate_election_id": ""}),
    ])


def _points_preview(candidate: Candidate) -> str:
    from django.conf import settings
    base = Decimal(settings.POLIUM["VOTE_DECLARATION_BASE"])
    if candidate.is_endorsed:
        mult = Decimal(str(settings.POLIUM["ENDORSED_MULTIPLIER"]))
    elif candidate.is_blacklisted:
        mult = Decimal(str(settings.POLIUM["BLACKLIST_MULTIPLIER"]))
    else:
        mult = Decimal("1")
    pts = (base * (candidate.current_rating / 100) * mult).quantize(Decimal("1"))
    return str(pts)


def _declare_ctx(
    election: Election,
    candidates: list[Candidate],
    declaration: VoteDeclaration | None,
    points_awarded: str | None = None,
    error: str = "",
) -> dict[str, object]:
    candidates_with_preview = [(c, _points_preview(c)) for c in candidates]
    return {
        "election": election,
        "candidates_with_preview": candidates_with_preview,
        "declaration": declaration,
        "points_awarded": points_awarded,
        "error": error,
    }


def election_detail(request: HttpRequest, sqid: str) -> HttpResponse:
    election = get_object_or_404(Election, sqid=sqid)
    candidates = list(
        election.candidates.select_related("jurisdiction").order_by("is_blacklisted", "-current_rating")
    )
    declaration: VoteDeclaration | None = None
    if request.user.is_authenticated:
        declaration = (
            VoteDeclaration.objects.filter(player=request.user, election=election)
            .select_related("candidate")
            .first()
        )
    today = date.today()
    return render(request, "polium/election_detail.html", {
        "election": election,
        "candidates_with_preview": [(c, _points_preview(c)) for c in candidates],
        "declaration": declaration,
        "today": today,
    })


@login_required
@require_POST
def election_declare(request: HttpRequest, sqid: str) -> DatastarResponse:
    election = get_object_or_404(Election, sqid=sqid)
    signals = read_signals(request) or {}
    candidate_sqid = str(signals.get("candidate_sqid", "")).strip()

    candidates = list(
        election.candidates.select_related("jurisdiction").order_by("is_blacklisted", "-current_rating")
    )

    if not candidate_sqid:
        declaration = (
            VoteDeclaration.objects.filter(player=request.user, election=election)
            .select_related("candidate")
            .first()
        )
        html = render_to_string(
            "polium/partials/election_declare_section.html",
            _declare_ctx(election, candidates, declaration, error="Please select a candidate."),
            request=request,
        )
        return DatastarResponse(ServerSentEventGenerator.patch_elements(html, selector="#election-declare-section"))

    candidate = Candidate.objects.filter(sqid=candidate_sqid, election=election).first()
    if candidate is None:
        declaration = (
            VoteDeclaration.objects.filter(player=request.user, election=election)
            .select_related("candidate")
            .first()
        )
        html = render_to_string(
            "polium/partials/election_declare_section.html",
            _declare_ctx(election, candidates, declaration, error="Invalid candidate selection."),
            request=request,
        )
        return DatastarResponse(ServerSentEventGenerator.patch_elements(html, selector="#election-declare-section"))

    points_awarded: Decimal = service.declare_vote(request.user, candidate, election)

    declaration = (
        VoteDeclaration.objects.filter(player=request.user, election=election)
        .select_related("candidate")
        .first()
    )
    awarded_str = str(points_awarded.quantize(Decimal("1"))) if points_awarded > 0 else None

    html = render_to_string(
        "polium/partials/election_declare_section.html",
        _declare_ctx(election, candidates, declaration, points_awarded=awarded_str),
        request=request,
    )
    return DatastarResponse(ServerSentEventGenerator.patch_elements(html, selector="#election-declare-section"))


def jurisdiction_detail(request: HttpRequest, sqid: str) -> HttpResponse:
    jurisdiction = get_object_or_404(Jurisdiction, sqid=sqid)

    ancestors: list[Jurisdiction] = []
    node = jurisdiction.parent
    while node:
        ancestors.insert(0, node)
        node = node.parent

    today = date.today()
    children = list(jurisdiction.children.filter(status=Jurisdiction.STATUS_ACTIVE).order_by("name"))
    upcoming = list(jurisdiction.elections.filter(election_date__gte=today).order_by("election_date")[:10])
    past = list(jurisdiction.elections.filter(election_date__lt=today).order_by("-election_date")[:5])
    candidates = list(jurisdiction.candidates.order_by("is_blacklisted", "-current_rating"))
    elections_for_form = list(jurisdiction.elections.order_by("-election_date"))
    is_active = jurisdiction.status == Jurisdiction.STATUS_ACTIVE

    ctx: dict[str, object] = {
        "jurisdiction": jurisdiction,
        "ancestors": ancestors,
        "children": children,
        "upcoming_elections": upcoming,
        "past_elections": past,
        "candidates": candidates,
        "elections_for_form": elections_for_form,
        "is_active": is_active,
        "level_label": LEVEL_LABELS.get(jurisdiction.level, jurisdiction.level),
        "is_following": False,
        "follow_depth": None,
        "is_mature": False,
        "already_flagged": False,
        "flagged_target": None,
    }

    if request.user.is_authenticated:
        follow = JurisdictionFollow.objects.filter(
            player=request.user, jurisdiction=jurisdiction
        ).first()
        ctx["is_following"] = follow is not None
        ctx["follow_depth"] = follow.depth if follow else None
        ctx["is_mature"] = account_is_mature(request.user)
        flag = JurisdictionDuplicateFlag.objects.filter(
            flagging_player=request.user, flagged_jurisdiction=jurisdiction
        ).first()
        ctx["already_flagged"] = flag is not None
        ctx["flagged_target"] = flag.points_to if flag else None

    return render(request, "polium/jurisdiction_detail.html", ctx)


@login_required
@require_POST
def jurisdiction_follow_detail(request: HttpRequest, sqid: str) -> DatastarResponse:
    signals = read_signals(request) or {}
    depth = signals.get("follow_depth", JurisdictionFollow.DEPTH_ALL)
    if depth not in (JurisdictionFollow.DEPTH_THIS, JurisdictionFollow.DEPTH_ALL):
        depth = JurisdictionFollow.DEPTH_ALL
    jurisdiction = get_object_or_404(Jurisdiction, sqid=sqid)
    _, created = JurisdictionFollow.objects.get_or_create(
        player=request.user,
        jurisdiction=jurisdiction,
        defaults={"depth": depth},
    )
    if created:
        Jurisdiction.objects.filter(pk=jurisdiction.pk).update(
            active_engagement=F("active_engagement") + 1
        )
    follow = JurisdictionFollow.objects.filter(player=request.user, jurisdiction=jurisdiction).first()
    html = render_to_string(
        "polium/partials/follow_section.html",
        {
            "jurisdiction": jurisdiction,
            "is_following": True,
            "follow_depth": follow.depth if follow else depth,
            "is_active": jurisdiction.status == Jurisdiction.STATUS_ACTIVE,
        },
        request=request,
    )
    return DatastarResponse(ServerSentEventGenerator.patch_elements(html, selector="#follow-section"))


@login_required
@require_POST
def jurisdiction_unfollow_detail(request: HttpRequest, sqid: str) -> DatastarResponse:
    jurisdiction = get_object_or_404(Jurisdiction, sqid=sqid)
    deleted, _ = JurisdictionFollow.objects.filter(
        player=request.user,
        jurisdiction=jurisdiction,
    ).delete()
    if deleted:
        Jurisdiction.objects.filter(pk=jurisdiction.pk).update(
            active_engagement=Greatest(F("active_engagement") - 1, 0)
        )
    html = render_to_string(
        "polium/partials/follow_section.html",
        {
            "jurisdiction": jurisdiction,
            "is_following": False,
            "follow_depth": None,
            "is_active": jurisdiction.status == Jurisdiction.STATUS_ACTIVE,
        },
        request=request,
    )
    return DatastarResponse(ServerSentEventGenerator.patch_elements(html, selector="#follow-section"))


def elections_section(request: HttpRequest, sqid: str) -> DatastarResponse:
    jurisdiction = get_object_or_404(Jurisdiction, sqid=sqid)
    html = render_to_string(
        "polium/partials/elections_section.html",
        _elections_ctx(jurisdiction, show_form=False),
        request=request,
    )
    return DatastarResponse(ServerSentEventGenerator.patch_elements(html, selector="#elections-section"))


@login_required
def add_election_form(request: HttpRequest, sqid: str) -> DatastarResponse:
    jurisdiction = get_object_or_404(Jurisdiction, sqid=sqid)
    html = render_to_string(
        "polium/partials/elections_section.html",
        _elections_ctx(jurisdiction, show_form=True),
        request=request,
    )
    return DatastarResponse(ServerSentEventGenerator.patch_elements(html, selector="#elections-section"))


@login_required
@require_POST
def add_election(request: HttpRequest, sqid: str) -> DatastarResponse:
    jurisdiction = get_object_or_404(Jurisdiction, sqid=sqid)
    signals = read_signals(request) or {}
    name = str(signals.get("election_name", "")).strip()
    election_date_str = str(signals.get("election_date", "")).strip()
    external_reference = str(signals.get("election_external_reference", "")).strip()

    error = ""
    election_date: date | None = None
    if not name:
        error = "Election name is required."
    elif not election_date_str:
        error = "Election date is required."
    else:
        try:
            election_date = date.fromisoformat(election_date_str)
        except ValueError:
            error = "Invalid date. Use YYYY-MM-DD format."

    if error:
        html = render_to_string(
            "polium/partials/elections_section.html",
            _elections_ctx(jurisdiction, show_form=True, error=error),
            request=request,
        )
        return DatastarResponse(ServerSentEventGenerator.patch_elements(html, selector="#elections-section"))

    Election.objects.create(
        name=name,
        jurisdiction=jurisdiction,
        election_date=election_date,
        external_reference=external_reference,
        created_by=request.user,
    )
    html = render_to_string(
        "polium/partials/elections_section.html",
        _elections_ctx(jurisdiction, show_form=False),
        request=request,
    )
    return DatastarResponse([
        ServerSentEventGenerator.patch_elements(html, selector="#elections-section"),
        ServerSentEventGenerator.patch_signals({"election_name": "", "election_date": "", "election_external_reference": ""}),
    ])


def candidates_section(request: HttpRequest, sqid: str) -> DatastarResponse:
    jurisdiction = get_object_or_404(Jurisdiction, sqid=sqid)
    html = render_to_string(
        "polium/partials/candidates_section.html",
        _candidates_ctx(jurisdiction, show_form=False),
        request=request,
    )
    return DatastarResponse(ServerSentEventGenerator.patch_elements(html, selector="#candidates-section"))


@login_required
def add_candidate_form(request: HttpRequest, sqid: str) -> DatastarResponse:
    jurisdiction = get_object_or_404(Jurisdiction, sqid=sqid)
    html = render_to_string(
        "polium/partials/candidates_section.html",
        _candidates_ctx(jurisdiction, show_form=True),
        request=request,
    )
    return DatastarResponse(ServerSentEventGenerator.patch_elements(html, selector="#candidates-section"))


@login_required
@require_POST
def add_candidate(request: HttpRequest, sqid: str) -> DatastarResponse:
    jurisdiction = get_object_or_404(Jurisdiction, sqid=sqid)
    signals = read_signals(request) or {}
    name = str(signals.get("candidate_name", "")).strip()
    office = str(signals.get("candidate_office", "")).strip()
    election_id_str = str(signals.get("candidate_election_id", "")).strip()
    external_reference = str(signals.get("candidate_external_reference", "")).strip()
    bio = str(signals.get("candidate_bio", "")).strip()

    error = ""
    if not name:
        error = "Candidate name is required."
    elif not office:
        error = "Office is required."

    election: Election | None = None
    if not error and election_id_str:
        try:
            eid = int(election_id_str)
            election = Election.objects.filter(pk=eid, jurisdiction=jurisdiction).first()
            if election is None:
                error = "Selected election does not belong to this jurisdiction."
        except ValueError:
            error = "Invalid election."

    if error:
        html = render_to_string(
            "polium/partials/candidates_section.html",
            _candidates_ctx(jurisdiction, show_form=True, error=error),
            request=request,
        )
        return DatastarResponse(ServerSentEventGenerator.patch_elements(html, selector="#candidates-section"))

    Candidate.objects.create(
        name=name,
        jurisdiction=jurisdiction,
        office=office,
        election=election,
        external_reference=external_reference,
        bio=bio,
        created_by=request.user,
    )
    html = render_to_string(
        "polium/partials/candidates_section.html",
        _candidates_ctx(jurisdiction, show_form=False),
        request=request,
    )
    return DatastarResponse([
        ServerSentEventGenerator.patch_elements(html, selector="#candidates-section"),
        ServerSentEventGenerator.patch_signals({"candidate_name": "", "candidate_office": "", "candidate_election_id": "", "candidate_external_reference": "", "candidate_bio": ""}),
    ])


@login_required
@require_POST
def flag_jurisdiction_duplicate(request: HttpRequest, sqid: str) -> DatastarResponse:
    jurisdiction = get_object_or_404(Jurisdiction, sqid=sqid)
    is_active = jurisdiction.status == Jurisdiction.STATUS_ACTIVE
    is_mature = account_is_mature(request.user)

    def _render(already_flagged: bool, flagged_target: Jurisdiction | None = None, error: str = "") -> DatastarResponse:
        html = render_to_string(
            "polium/partials/flag_section.html",
            {
                "jurisdiction": jurisdiction,
                "already_flagged": already_flagged,
                "flagged_target": flagged_target,
                "is_mature": is_mature,
                "is_active": is_active,
                "error": error,
            },
            request=request,
        )
        return DatastarResponse(ServerSentEventGenerator.patch_elements(html, selector="#flag-section"))

    if not is_mature:
        return _render(already_flagged=False, error="Your account must be at least 7 days old with 3 surveys submitted to flag.")

    signals = read_signals(request) or {}
    target_sqid = str(signals.get("flag_target_sqid", "")).strip()

    if not target_sqid:
        return _render(already_flagged=False, error="Select a jurisdiction to flag as duplicate of.")

    target = get_object_or_404(Jurisdiction, sqid=target_sqid)

    if target.pk == jurisdiction.pk:
        return _render(already_flagged=False, error="Cannot flag a jurisdiction as a duplicate of itself.")

    try:
        with transaction.atomic():
            JurisdictionDuplicateFlag.objects.create(
                flagging_player=request.user,
                flagged_jurisdiction=jurisdiction,
                points_to=target,
            )
    except IntegrityError:
        existing = JurisdictionDuplicateFlag.objects.get(
            flagging_player=request.user, flagged_jurisdiction=jurisdiction
        )
        return _render(already_flagged=True, flagged_target=existing.points_to)

    return _render(already_flagged=True, flagged_target=target)


def jurisdiction_search_flag(request: HttpRequest) -> DatastarResponse:
    signals = read_signals(request) or {}
    q = str(signals.get("flag_q", "")).strip()
    exclude_sqid = request.GET.get("exclude", "")
    results: list[dict[str, str]] = []
    if len(q) >= 2:
        qs = Jurisdiction.objects.filter(
            name__icontains=q,
            status=Jurisdiction.STATUS_ACTIVE,
        )
        if exclude_sqid:
            qs = qs.exclude(sqid=exclude_sqid)
        results = list(qs.values("sqid", "name", "level")[:10])
    html = render_to_string(
        "polium/partials/flag_results.html",
        {"results": results},
        request=request,
    )
    return DatastarResponse(ServerSentEventGenerator.patch_elements(html, selector="#flag-results"))


def submit_survey(request: HttpRequest, sqid: str) -> HttpResponse:
    return HttpResponse("TODO")


def declare_vote(request: HttpRequest, sqid: str) -> HttpResponse:
    return HttpResponse("TODO")
