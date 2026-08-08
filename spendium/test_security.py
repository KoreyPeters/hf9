"""Access control across every Spendium endpoint.

Written as an audit rather than as tests for particular features, because the
question "is every view gated correctly" is not answerable by reading views one
at a time — it is exactly the question that gets missed when each phase only
reviews itself.

The table below is the intended audience for each endpoint. Anything added to
`spendium/urls.py` without a row here should fail the coverage test at the
bottom, which is the point: a new view must state who it is for.

Purchases are the sensitive case. They use sequential primary keys, so the
`player=request.user` filter is the *only* thing between a guessed id and
somebody else's basket.
"""

from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from accounts.models import Player
from spendium.models import Product, Purchase, PurchaseLineItem, Store

PUBLIC = "public"
ANY_PLAYER = "any signed-in player"
OWNER_ONLY = "the owning player"
MEMBERS_ONLY = "members"

# Every route in spendium/urls.py, and who it is for.
AUDIENCE = {
    "home": PUBLIC,
    "notify": PUBLIC,
    "privacy": PUBLIC,
    "product_detail": PUBLIC,
    "submit_product_survey": ANY_PLAYER,
    # Public for the same reason product pages are: a rating nobody outside the
    # game can see applies no pressure on the retailer.
    "store_detail": PUBLIC,
    "submit_store_survey": ANY_PLAYER,
    "action_centre": ANY_PLAYER,
    "set_email_preference": ANY_PLAYER,
    "purchase_list": ANY_PLAYER,
    # Any signed-in player until the free trial is spent, members after.
    "receipt_upload": MEMBERS_ONLY,
    "purchase_history_export": OWNER_ONLY,
    "purchase_history_delete": OWNER_ONLY,
    "purchase_detail": OWNER_ONLY,
    "purchase_delete": OWNER_ONLY,
    "disambiguation_section": OWNER_ONLY,
    "confirm_line": OWNER_ONLY,
    "choose_line_product": OWNER_ONLY,
    "accept_line_reading": OWNER_ONLY,
    "submit_line_free_text": OWNER_ONLY,
}


@pytest.fixture
def owner(db: None) -> Player:
    return Player.objects.create_user(username="owner", email="owner@example.com")


@pytest.fixture
def intruder(db: None) -> Player:
    return Player.objects.create_user(username="intruder", email="in@example.com")


@pytest.fixture(autouse=True)
def isolated_media(settings, tmp_path) -> None:
    settings.MEDIA_ROOT = tmp_path


@pytest.fixture
def store(db: None) -> Store:
    return Store.objects.create(name="Shoppers Drug Mart")


@pytest.fixture
def victim_purchase(owner: Player, store: Store) -> Purchase:
    purchase = Purchase.objects.create(
        player=owner,
        store=store,
        purchased_at=timezone.now(),
        total=Decimal("42.00"),
        processing_status=Purchase.STATUS_PROCESSED,
    )
    PurchaseLineItem.objects.create(
        purchase=purchase,
        raw_text="SOMETHING PRIVATE",
        line_total=Decimal("42.00"),
        disambiguation_state=PurchaseLineItem.STATE_PENDING,
    )
    return purchase


# ── Every route declares an audience ──────────────────────────────────────────


def test_every_route_has_a_declared_audience() -> None:
    """A new view must say who it is for.

    This is the check that would have caught a view added without gating, which
    is the failure no amount of reading individual views reliably prevents.
    """
    from spendium import urls

    routed = {pattern.name for pattern in urls.urlpatterns}
    undeclared = routed - set(AUDIENCE)
    assert not undeclared, f"No declared audience for: {sorted(undeclared)}"


# ── Login gating ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [n for n, audience in AUDIENCE.items() if audience != PUBLIC],
)
@pytest.mark.django_db
def test_non_public_routes_require_login(client, name: str) -> None:
    args = ["1"] if _takes_pk(name) or _takes_sqid(name) else []
    url = reverse(f"spendium:{name}", args=args)
    response = client.get(url)
    # Redirect to login, or method-not-allowed on a POST-only route — either
    # way it is not serving content to an anonymous caller.
    assert response.status_code in (302, 405), f"{name} served {response.status_code}"


def _takes_sqid(name: str) -> bool:
    return name in {
        "product_detail",
        "submit_product_survey",
        "store_detail",
        "submit_store_survey",
    }


def _takes_pk(name: str) -> bool:
    return name in {
        "purchase_detail",
        "purchase_delete",
        "disambiguation_section",
        "confirm_line",
        "choose_line_product",
        "accept_line_reading",
        "submit_line_free_text",
    }


# ── Public routes really are public ───────────────────────────────────────────


@pytest.mark.django_db
def test_privacy_is_public(client) -> None:
    assert client.get(reverse("spendium:privacy")).status_code == 200


@pytest.mark.django_db
def test_product_pages_are_public(client) -> None:
    """Deliberate. Ratings are meant to be actionable by people who are not
    players — a rating nobody outside the game can see applies no pressure."""
    product = Product.objects.create(canonical_name="Heinz Ketchup")
    product.refresh_from_db()
    response = client.get(reverse("spendium:product_detail", args=[product.sqid]))
    assert response.status_code == 200


# ── Cross-player access: the sequential primary key problem ───────────────────


@pytest.mark.django_db
def test_another_players_receipt_cannot_be_read(
    client, intruder: Player, victim_purchase: Purchase
) -> None:
    client.force_login(intruder)
    response = client.get(
        reverse("spendium:purchase_detail", args=[victim_purchase.pk])
    )
    assert response.status_code == 404
    assert b"SOMETHING PRIVATE" not in response.content


@pytest.mark.django_db
def test_another_players_prompts_cannot_be_read(
    client, intruder: Player, victim_purchase: Purchase
) -> None:
    client.force_login(intruder)
    response = client.get(
        reverse("spendium:disambiguation_section", args=[victim_purchase.pk])
    )
    assert response.status_code == 404


@pytest.mark.django_db
def test_another_players_receipt_cannot_be_deleted(
    client, intruder: Player, victim_purchase: Purchase
) -> None:
    client.force_login(intruder)
    response = client.post(
        reverse("spendium:purchase_delete", args=[victim_purchase.pk])
    )
    assert response.status_code == 404
    assert Purchase.objects.filter(pk=victim_purchase.pk).exists()


@pytest.mark.parametrize(
    "name", ["confirm_line", "choose_line_product", "submit_line_free_text"]
)
@pytest.mark.django_db
def test_another_players_line_cannot_be_resolved(
    client, intruder: Player, victim_purchase: Purchase, name: str
) -> None:
    line = victim_purchase.line_items.get()
    client.force_login(intruder)
    response = client.post(reverse(f"spendium:{name}", args=[line.pk]))
    assert response.status_code == 404
    line.refresh_from_db()
    assert line.disambiguation_state == PurchaseLineItem.STATE_PENDING


@pytest.mark.django_db
def test_the_receipt_list_shows_only_your_own(
    client, intruder: Player, victim_purchase: Purchase
) -> None:
    client.force_login(intruder)
    response = client.get(reverse("spendium:purchase_list"))
    assert response.status_code == 200
    assert b"Shoppers Drug Mart" not in response.content


@pytest.mark.django_db
def test_export_covers_only_your_own(
    client, intruder: Player, victim_purchase: Purchase
) -> None:
    client.force_login(intruder)
    response = client.get(reverse("spendium:purchase_history_export"))
    assert response.status_code == 200
    assert b"SOMETHING PRIVATE" not in response.content


@pytest.mark.django_db
def test_bulk_delete_touches_only_your_own(
    client, intruder: Player, victim_purchase: Purchase
) -> None:
    """The one endpoint with no id at all, so scoping is its only protection."""
    client.force_login(intruder)
    client.post(reverse("spendium:purchase_history_delete"))
    assert Purchase.objects.filter(pk=victim_purchase.pk).exists()


@pytest.mark.django_db
def test_the_action_centre_shows_only_your_own(
    client, intruder: Player, victim_purchase: Purchase
) -> None:
    client.force_login(intruder)
    response = client.get(reverse("spendium:action_centre"))
    assert response.status_code == 200
    assert b"SOMETHING PRIVATE" not in response.content


# ── The members-only gate ─────────────────────────────────────────────────────


@pytest.mark.django_db
def test_upload_is_refused_once_the_trial_is_spent(
    client, intruder: Player, settings
) -> None:
    """Membership is still the gate; the trial only moves where it sits."""
    settings.SPENDIUM = settings.SPENDIUM | {"FREE_TRIAL_UPLOADS": 0}
    client.force_login(intruder)
    assert client.get(reverse("spendium:receipt_upload")).status_code == 403


# ── Task endpoints ────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_task_endpoints_reject_unauthenticated_calls_in_production(
    client, settings
) -> None:
    """The OIDC guard is skipped under DEBUG, so it has never run in anger.

    Without this, the only assurance that production task endpoints are not
    world-callable is that somebody read the decorator.
    """
    settings.DEBUG = False
    response = client.post(
        reverse("task_snapshot_metrics"), data="{}", content_type="application/json"
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_task_endpoints_reject_a_bogus_bearer_token(client, settings) -> None:
    settings.DEBUG = False
    response = client.post(
        reverse("task_snapshot_metrics"),
        data="{}",
        content_type="application/json",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_every_registered_task_is_reachable() -> None:
    """A task with no URL is silently dead in production.

    `snapshot-metrics` was exactly that: registered by its decorator, enqueued
    by name, scheduled in terraform, and routed nowhere — so the Cloud Scheduler
    job would have 404ed nightly and metrics would never have been recorded,
    with nothing failing loudly enough to notice.
    """
    import hf.task_urls as task_urls
    from core.tasks import _registry

    routed = {p.pattern._route.strip("/") for p in task_urls.urlpatterns}
    unreachable = set(_registry) - routed
    assert not unreachable, f"Registered but unroutable: {sorted(unreachable)}"


# ── CSRF ──────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_the_waitlist_endpoint_enforces_csrf() -> None:
    """It was exempt, covering for a form that sent no token.

    An unauthenticated endpoint that writes to the database has no business
    skipping the check when the fix is one line in the template.
    """
    from django.test import Client

    strict = Client(enforce_csrf_checks=True)
    response = strict.post(reverse("spendium:notify"), {"email": "someone@example.com"})
    assert response.status_code == 403


# ── Cost controls ─────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_disabling_adjudication_stops_the_second_model_call(
    owner: Player, fake_model
) -> None:
    """The only cost control there is, verified end to end.

    A kill switch nobody has pulled is a kill switch nobody knows works. This
    covers Tier 2 only — extraction still runs on every receipt, which is why
    Phase 14 exists.
    """
    from spendium import service
    from spendium.models import MatchConfig
    from spendium.test_extraction import png_bytes, receipt_payload

    config = MatchConfig.get()
    config.adjudication_candidates = 0
    config.weak_match_score = 95  # force residuals that would otherwise adjudicate
    config.noise_floor_score = 10
    config.save()
    Product.objects.create(canonical_name="Colgate Cavity Toothpaste Regular")

    client = fake_model(receipt_payload())
    service.accept_upload(owner, png_bytes(), content_type="image/png")

    # Extraction only. A second call would mean adjudication ran anyway.
    assert len(client.models.calls) == 1
