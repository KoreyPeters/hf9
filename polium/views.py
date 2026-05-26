from datetime import date

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .models import Election, Jurisdiction, JurisdictionFollow


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
    return HttpResponse("TODO")


def election_detail(request: HttpRequest, sqid: str) -> HttpResponse:
    return HttpResponse("TODO")


def jurisdiction_detail(request: HttpRequest, sqid: str) -> HttpResponse:
    return HttpResponse("TODO")


def submit_survey(request: HttpRequest, sqid: str) -> HttpResponse:
    return HttpResponse("TODO")


def declare_vote(request: HttpRequest, sqid: str) -> HttpResponse:
    return HttpResponse("TODO")
