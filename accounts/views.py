from django.http import HttpRequest, HttpResponse
from django.shortcuts import render


def signup(request: HttpRequest) -> HttpResponse:
    return render(request, "accounts/signup.html")
