from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.contenttypes.models import ContentType
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from evidence.models import Evidence, EvidenceFlag
from evidence.service import AlreadyFlaggedError, NotMatureError, flag_evidence, submit_evidence, vote_usefulness
from surveys.models import Criterion

from .models import Candidate, Election, Jurisdiction, JurisdictionFollow


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


def jurisdiction_search(request: HttpRequest) -> JsonResponse:
    q = request.GET.get("q", "").strip()
    if len(q) < 2:
        return JsonResponse({"results": []})
    jurisdictions = list(
        Jurisdiction.objects.filter(
            name__icontains=q,
            status=Jurisdiction.STATUS_ACTIVE,
        )
        .values("sqid", "name", "level")[:10]
    )
    return JsonResponse({"results": jurisdictions})


@login_required
@require_POST
def follow_jurisdiction(request: HttpRequest) -> HttpResponse:
    sqid = request.POST.get("jurisdiction_sqid", "")
    depth = request.POST.get("depth", JurisdictionFollow.DEPTH_ALL)
    if depth not in (JurisdictionFollow.DEPTH_THIS, JurisdictionFollow.DEPTH_ALL):
        depth = JurisdictionFollow.DEPTH_ALL
    jurisdiction = get_object_or_404(Jurisdiction, sqid=sqid)
    JurisdictionFollow.objects.get_or_create(
        player=request.user,
        jurisdiction=jurisdiction,
        defaults={"depth": depth},
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
    return render(request, "polium/candidate_profile.html", {
        "candidate": candidate,
        "evidence_list": evidence_qs,
        "blacklist_record": blacklist_record,
        "criteria": criteria,
        "flag_reasons": EvidenceFlag.REASON_CHOICES,
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


def election_detail(request: HttpRequest, sqid: str) -> HttpResponse:
    return HttpResponse("TODO")


def jurisdiction_detail(request: HttpRequest, sqid: str) -> HttpResponse:
    return HttpResponse("TODO")


def submit_survey(request: HttpRequest, sqid: str) -> HttpResponse:
    return HttpResponse("TODO")


def declare_vote(request: HttpRequest, sqid: str) -> HttpResponse:
    return HttpResponse("TODO")
