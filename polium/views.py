from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404

from .models import Candidate, Election, Jurisdiction


def get_candidate_by_sqid(sqid: str) -> Candidate:
    return get_object_or_404(Candidate, sqid=sqid)


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
