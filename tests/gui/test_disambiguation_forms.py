"""The disambiguation prompts, driven through a real browser.

Both behaviours here are invisible to `django.test.Client`: one is a keypress,
and the other is what happens to *other* inputs on the page when you type into
one. Neither has a server-side symptom, so neither can be caught anywhere else.
"""

from decimal import Decimal

import pytest
from django.utils import timezone
from playwright.sync_api import Page, expect

from accounts.models import Player
from accounts.utils import generate_username
from spendium.models import MatchTier, Purchase, PurchaseLineItem, Store


def _shopper() -> Player:
    player = Player.objects.create_user(
        username=generate_username(), email="shopper@example.com", password=None
    )
    Player.objects.filter(pk=player.pk).update(email_verified=True)
    player.refresh_from_db()
    return player


def _purchase_with_prompts(player: Player, count: int) -> Purchase:
    purchase = Purchase.objects.create(
        player=player,
        store=Store.objects.create(name="Shoppers Drug Mart"),
        purchased_at=timezone.now(),
        total=Decimal("10.00"),
        processing_status=Purchase.STATUS_PROCESSED,
    )
    for i in range(count):
        PurchaseLineItem.objects.create(
            purchase=purchase,
            raw_text=f"MYSTERY {i}",
            interpreted_name=f"Mystery {i}",
            line_total=Decimal("1.00"),
            match_tier=MatchTier.UNMATCHED,
            disambiguation_state=PurchaseLineItem.STATE_PENDING,
        )
    return purchase


@pytest.mark.django_db(transaction=True)
def test_enter_submits_a_suggestion(live_server, make_logged_in_page):
    """The reported bug: Enter did nothing and you had to reach for the mouse.

    Nothing server-side can observe this. The fix is a real `<form>`, so what is
    actually being tested is that the browser's own implicit submission has
    something to submit.
    """
    player = _shopper()
    purchase = _purchase_with_prompts(player, 1)

    page: Page = make_logged_in_page(player)
    page.goto(f"{live_server.url}/spendium/purchases/{purchase.pk}/")

    box = page.get_by_placeholder("Or describe it yourself")
    expect(box).to_be_visible()
    box.fill("Bulk Red Lentils")
    box.press("Enter")

    # The prompt is replaced by the "nothing left to check" state once the only
    # pending line resolves.
    expect(page.locator("#disambiguation-section")).to_contain_text(
        "Nothing to check on this receipt."
    )

    line = purchase.line_items.get()
    assert line.disambiguation_state == PurchaseLineItem.STATE_RESOLVED
    assert line.product is not None
    assert line.product.canonical_name == "Bulk Red Lentils"


@pytest.mark.django_db(transaction=True)
def test_typing_in_one_prompt_leaves_the_others_alone(live_server, make_logged_in_page):
    """The bug the form conversion actually fixes.

    Every prompt used to bind to one `free_text` signal declared on the
    container, so typing into any box filled all of them. The data stayed
    correct — each button posted to its own line — but the receipt showed one
    answer smeared across every question, and "show the rest" made it worse by
    lifting the cap of five.
    """
    player = _shopper()
    purchase = _purchase_with_prompts(player, 3)

    page: Page = make_logged_in_page(player)
    page.goto(f"{live_server.url}/spendium/purchases/{purchase.pk}/")

    boxes = page.get_by_placeholder("Or describe it yourself")
    expect(boxes).to_have_count(3)

    boxes.nth(1).fill("Bulk Red Lentils")

    expect(boxes.nth(0)).to_have_value("")
    expect(boxes.nth(2)).to_have_value("")


@pytest.mark.django_db(transaction=True)
def test_saving_by_click_still_works(live_server, make_logged_in_page):
    """The form rewrite is exactly where the original affordance would quietly
    break, so it is worth asserting rather than assuming."""
    player = _shopper()
    purchase = _purchase_with_prompts(player, 1)

    page: Page = make_logged_in_page(player)
    page.goto(f"{live_server.url}/spendium/purchases/{purchase.pk}/")

    page.get_by_placeholder("Or describe it yourself").fill("Bulk Red Lentils")
    page.get_by_role("button", name="Save").click()

    expect(page.locator("#disambiguation-section")).to_contain_text(
        "Nothing to check on this receipt."
    )
    assert purchase.line_items.get().disambiguation_state == (
        PurchaseLineItem.STATE_RESOLVED
    )


@pytest.mark.django_db(transaction=True)
def test_an_empty_suggestion_is_refused_by_the_browser(
    live_server, make_logged_in_page
):
    """`contentType: 'form'` runs `checkValidity()` before sending anything, so
    `required` is enforced without a round trip. Previously an empty box posted
    and came back with a server-rendered error."""
    player = _shopper()
    purchase = _purchase_with_prompts(player, 1)

    page: Page = make_logged_in_page(player)
    page.goto(f"{live_server.url}/spendium/purchases/{purchase.pk}/")

    page.get_by_placeholder("Or describe it yourself").press("Enter")

    expect(page.get_by_placeholder("Or describe it yourself")).to_be_visible()
    assert purchase.line_items.get().disambiguation_state == (
        PurchaseLineItem.STATE_PENDING
    )
