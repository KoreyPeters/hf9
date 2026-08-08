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
from django.db.models import Count, Q, Sum
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.views.decorators.http import require_POST

from points.models import PointTransaction
from surveys.service import (
    CoolDownError,
    categories_for,
    criteria_for,
    submit_survey,
)

from . import action_centre, disambiguation, ratings, service
from .models import ActionCentreState, Product, Purchase, PurchaseLineItem, Store


# The discovery section stays hidden below this many publishable products. A
# listing of two says "nobody is here" more loudly than no listing at all.
DISCOVERY_FLOOR = 6


def home(request: HttpRequest) -> HttpResponse:
    """Spendium's front door.

    Three states, following the shape of `polium_home`. The difference is what
    they are organised around: Polium has elections, which happen whether or not
    a player turns up, so its home is a calendar. Nothing happens in Spendium
    except what the player did, so this is a workbench — their own receipts
    first, and discovery only once there is anything worth discovering.

    Public, because the front page needs somewhere to send people that explains
    the game before asking them to sign up.
    """
    if not request.user.is_authenticated:
        return render(request, "spendium/home.html", {"state": "anonymous"})

    purchases = Purchase.objects.filter(player=request.user)
    if not purchases.exists():
        return render(
            request,
            "spendium/home.html",
            {
                "state": "no_purchases",
                "trial_left": service.trial_uploads_left(request.user),
                "is_member": service.is_member(request.user),
            },
        )

    top = ratings.top_rated()
    return render(
        request,
        "spendium/home.html",
        {
            "state": "active",
            "purchase_count": purchases.count(),
            # From the ledger, not from the purchases: a purchase is anonymised
            # after thirty days and its points are not.
            "spending_points": PointTransaction.objects.filter(
                player=request.user, reason="purchase"
            ).aggregate(total=Sum("amount"))["total"]
            or 0,
            "waiting": action_centre.new_item_count(request.user),
            "recent": purchases.order_by("-created_at")[:3],
            "trial_left": service.trial_uploads_left(request.user),
            "is_member": service.is_member(request.user),
            "top_rated": top if len(top) >= DISCOVERY_FLOOR else [],
        },
    )


def privacy(request: HttpRequest) -> HttpResponse:
    return render(request, "spendium/privacy.html")


@require_POST
@datastar_response
def notify(request: HttpRequest) -> Generator[DatastarEvent, None, None]:
    """Public waitlist signup.

    Was csrf_exempt, which was covering for the form not sending a token rather
    than for any reason the endpoint needed exempting. The harm was small — the
    worst case is somebody cross-site submitting addresses into a waitlist — but
    an unauthenticated endpoint that writes to the database has no business
    skipping the check when the fix is one line in the template.
    """
    email = request.POST.get("email", "").strip()
    if email:
        from .models import SpendiumWaitlist

        SpendiumWaitlist.objects.get_or_create(email=email)
    fragment = render_to_string("spendium/partials/notify_success.html")
    yield SSE.patch_elements(fragment, selector="#notify-form")


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


def _expanded(request: HttpRequest) -> bool:
    """Whether the player has asked to see past the prompt budget.

    Read from the request on every prompt render, including the re-renders after
    an answer. Reading it once at page load would not survive them: answering a
    question would collapse the list back to five and the player would have to
    expand it again after every single tap, which is worse than not offering
    expansion at all.

    A query parameter rather than a signal. The prompt controls post with
    `contentType: 'form'`, and Datastar sends no signals at all in that mode —
    so a signal would simply stop arriving the moment those became forms. A
    query string is part of the request whatever the body contains, which is why
    every URL that re-renders this section carries `?expanded=`.
    """
    return request.GET.get("expanded") == "1"


def _prompts_ctx(
    purchase: Purchase, request: HttpRequest, error: str = ""
) -> dict[str, object]:
    """Context for the prompt section, expanded or not.

    `total_pending` is counted separately rather than measured off `prompts`,
    because the whole point is to know how many questions exist *beyond* the
    ones being shown.
    """
    total_pending = purchase.line_items.filter(
        disambiguation_state=PurchaseLineItem.STATE_PENDING
    ).count()
    expanded = _expanded(request)
    prompts = disambiguation.prompt_queue(
        purchase, limit=total_pending if expanded else None
    )
    return {
        "purchase": purchase,
        "prompts": prompts,
        "error": error,
        "expanded": expanded,
        # What the toggle offers, and what makes it absent when there is nothing
        # behind the cap. Zero when prompting is disabled outright, since
        # `prompt_queue` returns nothing in that case and there is no "rest" to
        # show.
        "hidden_count": max(0, total_pending - len(prompts)) if prompts else 0,
    }


def _prompts_response(
    purchase: Purchase, request: HttpRequest, error: str = ""
) -> DatastarResponse:
    html = render_to_string(
        "spendium/partials/disambiguation_section.html",
        _prompts_ctx(purchase, request, error),
        request=request,
    )
    # Elements only. There used to be a `patch_signals` here resetting
    # `free_text` and `chosen_product_id`, needed because a signal survives the
    # element being replaced. Form fields do not: this patch swaps the whole
    # section, and the inputs come back empty on their own.
    return DatastarResponse(
        [
            ServerSentEventGenerator.patch_elements(
                html, selector="#disambiguation-section"
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
            **_prompts_ctx(purchase, request),
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
    # Posted as form data, not signals. The prompt controls submit with
    # `contentType: 'form'`, which sends the closest form's fields — scoping the
    # value to one prompt instead of the whole section, which is what the old
    # shared signal could not do.
    raw_id = request.POST.get("chosen_product_id", "").strip()

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
def accept_line_reading(request: HttpRequest, pk: int) -> DatastarResponse:
    """One tap to agree with the name we read off the receipt.

    Takes no signals: the text being accepted is the one already stored on the
    line, not anything the client sends. That is deliberate — a description
    posted from the browser is `submit_line_free_text`'s job, and keeping the two
    apart means this endpoint cannot be used to put arbitrary text into the
    catalogue under the cheaper-looking name.
    """
    line = _get_line(request, pk)
    try:
        disambiguation.accept_reading(line)
    except (disambiguation.WindowClosedError, ValueError) as exc:
        return _prompts_response(line.purchase, request, error=str(exc))
    return _prompts_response(line.purchase, request)


@login_required
@require_POST
def submit_line_free_text(request: HttpRequest, pk: int) -> DatastarResponse:
    line = _get_line(request, pk)
    # Form data, per the note in `choose_line_product`. Scoped to one prompt's
    # form, so typing into the third box no longer fills the other eleven.
    text = request.POST.get("free_text", "").strip()

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
        {"purchases": purchases, "is_member": service.is_member(request.user)},
    )


@login_required
def receipt_upload(request: HttpRequest) -> HttpResponse:
    if not service.may_upload(request.user):
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
                service.UploadsPausedError,
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
            "is_member": service.is_member(request.user),
            "trial_left": service.trial_uploads_left(request.user),
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


# ── Product ratings ───────────────────────────────────────────────────────────


def _rating_ctx(product: Product, player: object) -> dict[str, object]:
    rating = ratings.compute(product)
    criteria = criteria_for(product, "spendium")
    # The banner about provisional criteria is per category; with one category
    # per subject today this is that category, and it degrades to nothing rather
    # than guessing if a subject ever draws on several.
    categories = categories_for(product, "spendium")
    category = categories[0] if len(categories) == 1 else None

    # Anyone signed in may rate. A purchase decides whether the response is
    # anchored to evidence, not whether it is allowed — `design.md:341` says
    # players and non-players alike may submit, and this used to gate on having
    # bought the thing, which contradicted it and made a new product unrateable
    # by everyone except the handful who had already bought it.
    can_rate = player.is_authenticated
    return {
        "product": product,
        "rating": rating,
        "breakdown": ratings.response_breakdown(product),
        "trend": ratings.trend(product),
        "category": category,
        "criteria": criteria,
        "can_rate": can_rate,
        # Said up front, because it is fairer than silently discounting the
        # answer afterwards. This is what `is_verified` has always meant and
        # what the weighting already acts on.
        "will_be_verified": can_rate and ratings.player_has_bought(player, product),
    }


def product_detail(request: HttpRequest, sqid: str) -> HttpResponse:
    """Public. Ratings are meant to be actionable by people who are not players."""
    product = get_object_or_404(Product, sqid=sqid)
    canonical = product.resolve_canonical()
    if canonical.pk != product.pk:
        return redirect("spendium:product_detail", sqid=canonical.sqid)
    return render(
        request, "spendium/product_detail.html", _rating_ctx(product, request.user)
    )


# ── Store ratings ─────────────────────────────────────────────────────────────


def _store_rating_ctx(store: Store, player: object) -> dict[str, object]:
    """Everything the store page needs.

    Leads with points per dollar rather than the percentage. For a retailer that
    is the figure players choose on — "29 points per dollar versus 4" is a
    reason to cross the road, where "68%" is a judgement — and the design asks
    for it prominently.
    """
    categories = categories_for(store, "spendium")
    return {
        "store": store,
        "rating": ratings.compute_store(store),
        "points_per_dollar": ratings.store_points_per_dollar(store),
        "category": categories[0] if len(categories) == 1 else None,
        "criteria": criteria_for(store, "spendium"),
        "can_rate": player.is_authenticated,
        "will_be_verified": player.is_authenticated
        and ratings.player_has_shopped_at(player, store),
    }


def store_detail(request: HttpRequest, sqid: str) -> HttpResponse:
    """Public, for the same reason product pages are: a rating nobody outside
    the game can see applies no pressure."""
    store = get_object_or_404(Store, sqid=sqid)
    return render(
        request, "spendium/store_detail.html", _store_rating_ctx(store, request.user)
    )


@login_required
@require_POST
def submit_store_survey(request: HttpRequest, sqid: str) -> DatastarResponse:
    store = get_object_or_404(Store, sqid=sqid)
    signals = read_signals(request) or {}

    answers: dict[int, bool] = {}
    for key, value in signals.items():
        if not key.startswith("criterion_"):
            continue
        try:
            answers[int(key.removeprefix("criterion_"))] = bool(value)
        except ValueError:
            continue

    error = ""
    if not answers:
        error = "Answer at least one question."
    else:
        try:
            submit_survey(
                request.user,
                store,
                answers,
                is_verified=ratings.player_has_shopped_at(request.user, store),
            )
        except CoolDownError as exc:
            error = f"You rated this recently. Try again in {exc.args[0].days} days."

    context = _store_rating_ctx(store, request.user)
    context["error"] = error
    context["submitted"] = not error
    html = render_to_string(
        "spendium/partials/store_rating_section.html", context, request=request
    )
    return DatastarResponse(
        ServerSentEventGenerator.patch_elements(html, selector="#store-rating-section")
    )


@login_required
@require_POST
def submit_product_survey(request: HttpRequest, sqid: str) -> DatastarResponse:
    product = get_object_or_404(Product, sqid=sqid).resolve_canonical()
    signals = read_signals(request) or {}

    answers: dict[int, bool] = {}
    for key, value in signals.items():
        if not key.startswith("criterion_"):
            continue
        try:
            answers[int(key.removeprefix("criterion_"))] = bool(value)
        except ValueError:
            continue

    error = ""
    if not answers:
        error = "Answer at least one question."
    else:
        try:
            # No `criteria_version` argument any more — each answer records the
            # version of its own criterion's category, which is the only place
            # it is well defined once more than one category can apply.
            submit_survey(
                request.user,
                product,
                answers,
                is_verified=ratings.player_has_bought(request.user, product),
            )
        except CoolDownError as exc:
            error = f"You rated this recently. Try again in {exc.args[0].days} days."

    context = _rating_ctx(product, request.user)
    context["error"] = error
    context["submitted"] = not error
    html = render_to_string(
        "spendium/partials/product_rating_section.html", context, request=request
    )
    return DatastarResponse(
        ServerSentEventGenerator.patch_elements(
            html, selector="#product-rating-section"
        )
    )


# ── Action Centre ─────────────────────────────────────────────────────────────


@login_required
def action_centre_view(request: HttpRequest) -> HttpResponse:
    """Everything the player has outstanding.

    Visiting clears the badge. That is the whole contract: the badge means
    "something new", and looking is what makes it not new any more.
    """
    centre = action_centre.build(request.user)
    action_centre.mark_visited(request.user)
    return render(
        request,
        "spendium/action_centre.html",
        {
            "centre": centre,
            "state": ActionCentreState.get_for(request.user),
        },
    )


@login_required
@require_POST
def set_email_preference(request: HttpRequest) -> HttpResponse:
    """Opt out, or back in. Honoured immediately."""
    state = ActionCentreState.get_for(request.user)
    state.emails_enabled = request.POST.get("emails_enabled") == "on"
    state.save(update_fields=["emails_enabled"])
    return redirect("spendium:action_centre")
