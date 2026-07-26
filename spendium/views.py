from collections.abc import Generator

from datastar_py import ServerSentEventGenerator as SSE
from datastar_py.django import (
    DatastarResponse,
    ServerSentEventGenerator,
    datastar_response,
    read_signals,
)
from datastar_py.sse import DatastarEvent
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import disambiguation, service
from .models import Product, Purchase, PurchaseLineItem


def privacy(request: HttpRequest) -> HttpResponse:
    return render(request, "spendium/privacy.html")


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


def _is_member(player: object) -> bool:
    """Receipt scanning is a members-only feature.

    Not a cost constraint — extraction is cheap per receipt. It is a deliberate
    product decision to make membership tangibly worth something.
    """
    try:
        membership = player.membership
    except ObjectDoesNotExist:
        return False
    return membership.is_active and membership.expires_at > timezone.now()


def _get_purchase(request: HttpRequest, pk: int) -> Purchase:
    """Scoped to the requesting player.

    Purchases carry no SQID because they are never linked publicly — a purchase
    belongs to one player for thirty days and then ceases to exist. Filtering on
    the player is what makes the sequential primary key safe: another player's
    id resolves to a 404 rather than to their basket.
    """
    return get_object_or_404(Purchase, pk=pk, player=request.user)


def _get_line(request: HttpRequest, pk: int) -> PurchaseLineItem:
    return get_object_or_404(PurchaseLineItem, pk=pk, purchase__player=request.user)


def _prompts_ctx(purchase: Purchase, error: str = "") -> dict[str, object]:
    return {
        "purchase": purchase,
        "prompts": disambiguation.prompt_queue(purchase),
        "error": error,
    }


def _prompts_response(
    purchase: Purchase, request: HttpRequest, error: str = ""
) -> DatastarResponse:
    html = render_to_string(
        "spendium/partials/disambiguation_section.html",
        _prompts_ctx(purchase, error),
        request=request,
    )
    return DatastarResponse(
        [
            ServerSentEventGenerator.patch_elements(
                html, selector="#disambiguation-section"
            ),
            ServerSentEventGenerator.patch_signals(
                {"free_text": "", "chosen_product_id": ""}
            ),
        ]
    )


@login_required
def purchase_detail(request: HttpRequest, pk: int) -> HttpResponse:
    purchase = _get_purchase(request, pk)
    return render(
        request,
        "spendium/purchase_detail.html",
        {
            "line_items": purchase.line_items.select_related("product").order_by("pk"),
            **_prompts_ctx(purchase),
        },
    )


@login_required
def disambiguation_section(request: HttpRequest, pk: int) -> DatastarResponse:
    """Re-render the prompt list. Also serves as Cancel."""
    return _prompts_response(_get_purchase(request, pk), request)


@login_required
@require_POST
def confirm_line(request: HttpRequest, pk: int) -> DatastarResponse:
    line = _get_line(request, pk)
    try:
        disambiguation.confirm(line)
    except (disambiguation.WindowClosedError, ValueError) as exc:
        return _prompts_response(line.purchase, request, error=str(exc))
    return _prompts_response(line.purchase, request)


@login_required
@require_POST
def choose_line_product(request: HttpRequest, pk: int) -> DatastarResponse:
    line = _get_line(request, pk)
    signals = read_signals(request) or {}
    raw_id = str(signals.get("chosen_product_id", "")).strip()

    if not raw_id:
        return _prompts_response(
            line.purchase, request, error="No product was selected."
        )
    try:
        product = Product.objects.get(pk=int(raw_id))
    except ValueError, Product.DoesNotExist:
        return _prompts_response(
            line.purchase, request, error="That product no longer exists."
        )

    try:
        disambiguation.choose(line, product)
    except disambiguation.WindowClosedError as exc:
        return _prompts_response(line.purchase, request, error=str(exc))
    return _prompts_response(line.purchase, request)


@login_required
@require_POST
def submit_line_free_text(request: HttpRequest, pk: int) -> DatastarResponse:
    line = _get_line(request, pk)
    signals = read_signals(request) or {}
    text = str(signals.get("free_text", "")).strip()

    try:
        disambiguation.submit_free_text(line, text)
    except (disambiguation.WindowClosedError, ValueError) as exc:
        return _prompts_response(line.purchase, request, error=str(exc))
    return _prompts_response(line.purchase, request)


# ── Receipt capture and history ───────────────────────────────────────────────


@login_required
def purchase_list(request: HttpRequest) -> HttpResponse:
    """The player's receipts.

    The privacy policy promises purchase history is visible and deletable, so
    this page is an obligation rather than a convenience.
    """
    purchases = (
        Purchase.objects.filter(player=request.user)
        .select_related("store")
        .annotate(
            unresolved=Count(
                "line_items",
                filter=Q(
                    line_items__disambiguation_state=PurchaseLineItem.STATE_PENDING
                ),
            )
        )
        .order_by("-purchased_at")
    )
    return render(
        request,
        "spendium/purchase_list.html",
        {"purchases": purchases, "is_member": _is_member(request.user)},
    )


@login_required
def receipt_upload(request: HttpRequest) -> HttpResponse:
    if not _is_member(request.user):
        return render(request, "spendium/upload_members_only.html", status=403)

    error = ""
    if request.method == "POST":
        upload = request.FILES.get("receipt")
        if upload is None:
            error = "Choose a photo of your receipt."
        else:
            try:
                purchase = service.accept_upload(
                    request.user,
                    upload.read(),
                    filename=upload.name,
                    content_type=upload.content_type or "",
                )
            except (
                service.DuplicateReceiptError,
                service.UnsupportedImageError,
            ) as exc:
                error = str(exc)
            else:
                return redirect("spendium:purchase_detail", pk=purchase.pk)

    return render(
        request,
        "spendium/receipt_upload.html",
        {
            "error": error,
            "max_mb": settings.SPENDIUM["MAX_UPLOAD_BYTES"] // (1024 * 1024),
        },
    )


@login_required
@require_POST
def purchase_delete(request: HttpRequest, pk: int) -> HttpResponse:
    service.delete_purchase(_get_purchase(request, pk))
    return redirect("spendium:purchase_list")


@login_required
@require_POST
def purchase_history_delete(request: HttpRequest) -> HttpResponse:
    service.delete_purchase_history(request.user)
    return redirect("spendium:purchase_list")


@login_required
def purchase_history_export(request: HttpRequest) -> JsonResponse:
    """A copy of everything held about this player's purchases."""
    response = JsonResponse(
        service.export_purchase_history(request.user),
        safe=False,
        json_dumps_params={"indent": 2},
    )
    response["Content-Disposition"] = (
        'attachment; filename="spendium-purchase-history.json"'
    )
    return response
