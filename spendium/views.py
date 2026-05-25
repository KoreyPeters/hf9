from collections.abc import Generator

from datastar_py import ServerSentEventGenerator as SSE
from datastar_py.django import datastar_response
from datastar_py.sse import DatastarEvent
from django.http import HttpRequest
from django.template.loader import render_to_string
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST


@csrf_exempt
@require_POST
@datastar_response
def notify(request: HttpRequest) -> Generator[DatastarEvent, None, None]:
    email = request.POST.get("email", "").strip()
    if email:
        from .models import SpendiumWaitlist
        SpendiumWaitlist.objects.get_or_create(email=email)
    fragment = render_to_string("spendium/partials/notify_success.html")
    yield SSE.patch_elements(fragment, selector="#notify-form")
