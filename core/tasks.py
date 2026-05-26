import json
import logging
from collections.abc import Callable
from datetime import datetime
from functools import wraps
from typing import Any

from django.conf import settings
from django.http import HttpRequest, HttpResponseForbidden, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

logger = logging.getLogger(__name__)

_registry: dict[str, Callable[..., Any]] = {}


def task(url_path: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        _registry[url_path] = fn

        @wraps(fn)
        @csrf_exempt
        @require_POST
        def view(request: HttpRequest) -> JsonResponse | HttpResponseForbidden:
            if not settings.DEBUG and not _verify_oidc(request):
                return HttpResponseForbidden()
            payload: dict[str, Any] = json.loads(request.body or b"{}")
            fn(**payload)
            return JsonResponse({"status": "ok"})

        return view

    return decorator


def enqueue(
    url_path: str,
    payload: dict[str, Any] | None = None,
    schedule_time: datetime | None = None,
) -> None:
    if payload is None:
        payload = {}

    if settings.DEBUG:
        if schedule_time is not None:
            return
        fn = _registry.get(url_path)
        if fn is None:
            raise ValueError(
                f"No task registered for '{url_path}'. "
                "Ensure the module containing the @task handler is imported at startup."
            )
        fn(**payload)
        return

    from google.cloud import tasks_v2

    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(
        settings.GCP_PROJECT,
        settings.GCP_REGION,
        settings.CLOUD_TASKS_QUEUE,
    )
    task_body: dict[str, Any] = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": f"{settings.TASK_BASE_URL}/tasks/{url_path}/",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode(),
            "oidc_token": {
                "service_account_email": settings.TASK_SERVICE_ACCOUNT,
            },
        }
    }
    if schedule_time is not None:
        from google.protobuf import timestamp_pb2
        ts = timestamp_pb2.Timestamp()
        ts.FromDatetime(schedule_time)
        task_body["schedule_time"] = ts
    client.create_task(request={"parent": parent, "task": task_body})


def _verify_oidc(request: HttpRequest) -> bool:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    token = auth[7:]
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token

        id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            audience=settings.TASK_BASE_URL,
        )
        return True
    except Exception:
        logger.warning("Task endpoint received invalid or missing OIDC token.")
        return False
