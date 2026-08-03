"""Tier 3: the player prompt — budgeting, ranking, and resolution."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from accounts.models import Player
from spendium import disambiguation
from spendium.models import (
    MatchConfig,
    MatchTier,
    Product,
    ProductAlias,
    Purchase,
    PurchaseLineItem,
    Store,
)


@pytest.fixture
def shopper(db: None) -> Player:
    return Player.objects.create_user(username="shopper", email="shopper@example.com")


@pytest.fixture
def other_shopper(db: None) -> Player:
    return Player.objects.create_user(username="other", email="other@example.com")


@pytest.fixture
def store(db: None) -> Store:
    return Store.objects.create(name="Shoppers Drug Mart")


def make_purchase(player: Player, store: Store | None = None) -> Purchase:
    return Purchase.objects.create(
        player=player,
        store=store,
        purchased_at=timezone.now(),
        total=Decimal("10.00"),
    )


def make_line(
    purchase: Purchase,
    raw_text: str,
    product: Product | None = None,
    confidence: str | None = None,
    state: str = PurchaseLineItem.STATE_PENDING,
) -> PurchaseLineItem:
    return PurchaseLineItem.objects.create(
        purchase=purchase,
        raw_text=raw_text,
        interpreted_name=raw_text.title(),
        product=product,
        match_confidence=Decimal(confidence) if confidence else None,
        match_tier=MatchTier.FUZZY if product else MatchTier.UNMATCHED,
        line_total=Decimal("1.00"),
        disambiguation_state=state,
    )


# ── Prompt budget ─────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_budget_caps_the_number_of_prompts(shopper: Player) -> None:
    """A player shown fifteen icons learns to ignore all of them."""
    purchase = make_purchase(shopper)
    for i in range(15):
        make_line(purchase, f"ITEM {i}")

    config = MatchConfig.get()
    config.prompt_budget = 5
    config.save()

    assert len(disambiguation.prompt_queue(purchase)) == 5


@pytest.mark.django_db
def test_budget_of_zero_disables_prompting(shopper: Player) -> None:
    purchase = make_purchase(shopper)
    make_line(purchase, "ITEM")
    config = MatchConfig.get()
    config.prompt_budget = 0
    config.save()
    assert disambiguation.prompt_queue(purchase) == []


@pytest.mark.django_db
def test_a_limit_overrides_the_budget(shopper: Player) -> None:
    purchase = make_purchase(shopper)
    for i in range(12):
        make_line(purchase, f"ITEM {i}")

    assert len(disambiguation.prompt_queue(purchase)) == 5
    assert len(disambiguation.prompt_queue(purchase, limit=12)) == 12


@pytest.mark.django_db
def test_a_limit_cannot_reach_past_a_disabled_budget(shopper: Player) -> None:
    """Zero means "do not ask". An override that got past it would turn that
    into "do not ask unless the player insists", which is a different setting
    and not the one the admin chose."""
    purchase = make_purchase(shopper)
    for i in range(3):
        make_line(purchase, f"ITEM {i}")
    config = MatchConfig.get()
    config.prompt_budget = 0
    config.save()

    assert disambiguation.prompt_queue(purchase, limit=3) == []


@pytest.mark.django_db
def test_resolved_lines_are_not_prompted(shopper: Player) -> None:
    purchase = make_purchase(shopper)
    make_line(purchase, "DONE", state=PurchaseLineItem.STATE_RESOLVED)
    make_line(purchase, "FINE", state=PurchaseLineItem.STATE_NOT_NEEDED)
    assert disambiguation.prompt_queue(purchase) == []


# ── Ranking ───────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_strings_blocking_more_receipts_rank_first(
    shopper: Player, other_shopper: Player
) -> None:
    """Global value, not private uncertainty, is the sort key.

    A string pending on many receipts is worth resolving many times over; the
    source plan's "lowest confidence first" would bury it under a one-off.
    """
    purchase = make_purchase(shopper)
    make_line(purchase, "RARE ITEM")
    make_line(purchase, "COMMON ITEM")

    # Four other players are blocked on the same string.
    for _ in range(4):
        make_line(make_purchase(other_shopper), "COMMON ITEM")

    config = MatchConfig.get()
    config.prompt_budget = 2
    config.save()

    prompts = disambiguation.prompt_queue(purchase)
    assert prompts[0].line.raw_text == "COMMON ITEM"
    assert prompts[0].blocked_elsewhere == 5


@pytest.mark.django_db
def test_least_confident_first_within_equal_reach(shopper: Player) -> None:
    purchase = make_purchase(shopper)
    product = Product.objects.create(canonical_name="Something")
    make_line(purchase, "SURE ITEM", product=product, confidence="0.890")
    make_line(purchase, "SHAKY ITEM", product=product, confidence="0.730")

    prompts = disambiguation.prompt_queue(purchase)
    assert prompts[0].line.raw_text == "SHAKY ITEM"


@pytest.mark.django_db
def test_unmatched_lines_outrank_weak_matches(shopper: Player) -> None:
    """No match at all is more uncertain than a shaky one."""
    purchase = make_purchase(shopper)
    product = Product.objects.create(canonical_name="Something")
    make_line(purchase, "WEAK ITEM", product=product, confidence="0.750")
    make_line(purchase, "NO MATCH ITEM")

    prompts = disambiguation.prompt_queue(purchase)
    assert prompts[0].line.raw_text == "NO MATCH ITEM"


@pytest.mark.django_db
def test_ordering_is_stable_between_page_loads(shopper: Player) -> None:
    purchase = make_purchase(shopper)
    for i in range(6):
        make_line(purchase, f"ITEM {i}")
    first = [p.line.pk for p in disambiguation.prompt_queue(purchase)]
    second = [p.line.pk for p in disambiguation.prompt_queue(purchase)]
    assert first == second


@pytest.mark.django_db
def test_candidates_are_recomputed_against_the_current_catalogue(
    shopper: Player,
) -> None:
    """The right answer may have been added since the receipt was read."""
    purchase = make_purchase(shopper)
    make_line(purchase, "HEINZ KETCHUP 750ML")
    assert disambiguation.prompt_queue(purchase)[0].candidates == []

    Product.objects.create(canonical_name="Heinz Ketchup")
    assert disambiguation.prompt_queue(purchase)[0].candidates


# ── Confirming and choosing ───────────────────────────────────────────────────


@pytest.mark.django_db
def test_confirming_resolves_the_line(shopper: Player, store: Store) -> None:
    purchase = make_purchase(shopper, store)
    product = Product.objects.create(canonical_name="Heinz Ketchup")
    line = make_line(purchase, "HEINZ 750", product=product, confidence="0.750")

    disambiguation.confirm(line)
    line.refresh_from_db()
    assert line.disambiguation_state == PurchaseLineItem.STATE_RESOLVED
    assert line.match_tier == MatchTier.PLAYER


@pytest.mark.django_db
def test_confirming_writes_an_alias(shopper: Player, store: Store) -> None:
    """So the next receipt with this string resolves at Tier 0."""
    purchase = make_purchase(shopper, store)
    product = Product.objects.create(canonical_name="Heinz Ketchup")
    line = make_line(purchase, "HEINZ 750", product=product)

    disambiguation.confirm(line)
    alias = ProductAlias.objects.get(raw_text_normalised="heinz 750")
    assert alias.product == product
    assert alias.source == ProductAlias.SOURCE_PLAYER
    assert alias.confirmation_count == 1


@pytest.mark.django_db
def test_two_players_confirming_makes_an_alias_authoritative(
    shopper: Player, other_shopper: Player, store: Store
) -> None:
    """Independent agreement is what stops the prompting, not one tap."""
    product = Product.objects.create(canonical_name="Heinz Ketchup")
    for player in (shopper, other_shopper):
        line = make_line(make_purchase(player, store), "HEINZ 750", product=product)
        disambiguation.confirm(line)

    alias = ProductAlias.objects.get(raw_text_normalised="heinz 750")
    assert alias.status == ProductAlias.STATUS_AUTHORITATIVE


@pytest.mark.django_db
def test_confirming_a_line_with_no_match_is_refused(shopper: Player) -> None:
    line = make_line(make_purchase(shopper), "MYSTERY")
    with pytest.raises(ValueError):
        disambiguation.confirm(line)


@pytest.mark.django_db
def test_choosing_a_different_product_resolves_the_line(
    shopper: Player, store: Store
) -> None:
    purchase = make_purchase(shopper, store)
    guessed = Product.objects.create(canonical_name="Heinz Ketchup")
    actual = Product.objects.create(canonical_name="Heinz Mustard")
    line = make_line(purchase, "HEINZ 750", product=guessed, confidence="0.720")

    disambiguation.choose(line, actual)
    line.refresh_from_db()
    assert line.product == actual


@pytest.mark.django_db
def test_choosing_follows_a_merge(shopper: Player, store: Store) -> None:
    purchase = make_purchase(shopper, store)
    retired = Product.objects.create(canonical_name="Old Record")
    survivor = Product.objects.create(canonical_name="Surviving Record")
    retired.merged_into = survivor
    retired.status = Product.STATUS_RETIRED
    retired.save()

    line = make_line(purchase, "SOMETHING")
    disambiguation.choose(line, retired)
    line.refresh_from_db()
    assert line.product == survivor


# ── Disagreeing with an existing alias ────────────────────────────────────────


@pytest.mark.django_db
def test_one_dissenter_does_not_overturn_two_confirmations(
    shopper: Player, store: Store, voter, other_voter
) -> None:
    """The alias falls back to provisional and resumes prompting — no more."""
    established = Product.objects.create(canonical_name="Established Product")
    alias = ProductAlias.objects.create(
        product=established, store=store, raw_text="HEINZ 750"
    )
    alias.confirm(voter)
    alias.confirm(other_voter)

    other = Product.objects.create(canonical_name="Other Product")
    line = make_line(make_purchase(shopper, store), "HEINZ 750")
    disambiguation.choose(line, other)

    alias.refresh_from_db()
    assert alias.status == ProductAlias.STATUS_PROVISIONAL
    assert alias.product == established
    # The dissenter's own line still reflects their choice.
    line.refresh_from_db()
    assert line.product == other


@pytest.mark.django_db
def test_a_fully_demoted_alias_is_reassigned(shopper: Player, store: Store) -> None:
    """Once the evidence is exhausted the string is free to mean something else."""
    wrong = Product.objects.create(canonical_name="Wrong Product")
    alias = ProductAlias.objects.create(
        product=wrong, store=store, raw_text="HEINZ 750"
    )

    right = Product.objects.create(canonical_name="Right Product")
    line = make_line(make_purchase(shopper, store), "HEINZ 750")
    disambiguation.choose(line, right)

    alias.refresh_from_db()
    assert alias.product == right
    assert alias.status == ProductAlias.STATUS_PROVISIONAL
    assert ProductAlias.objects.filter(raw_text_normalised="heinz 750").count() == 1


# ── Free text ─────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_free_text_resolves_to_an_existing_record(
    shopper: Player, store: Store
) -> None:
    """Player wording must not create a near-duplicate of what already exists."""
    existing = Product.objects.create(canonical_name="Heinz Tomato Ketchup")
    line = make_line(make_purchase(shopper, store), "HNZ TMT KTCP")

    disambiguation.submit_free_text(line, "Heinz Tomato Ketchup")
    line.refresh_from_db()
    assert line.product == existing
    assert Product.objects.count() == 1


@pytest.mark.django_db
def test_free_text_creates_a_player_supplied_record_when_nothing_matches(
    shopper: Player, store: Store
) -> None:
    line = make_line(make_purchase(shopper, store), "MYSTERY ITEM")
    disambiguation.submit_free_text(line, "Bulk Red Lentils")

    product = Product.objects.get(canonical_name="Bulk Red Lentils")
    assert product.status == Product.STATUS_UNVERIFIED
    assert product.confidence_source == Product.SOURCE_PLAYER
    line.refresh_from_db()
    assert line.product == product


@pytest.mark.django_db
def test_free_text_never_writes_the_catalogue_directly(
    shopper: Player, store: Store
) -> None:
    """It goes through the cascade like any other input.

    A description close enough to an existing record resolves to it rather than
    forking the catalogue, which is what keeps the admin merge queue small.
    """
    Product.objects.create(canonical_name="Heinz Tomato Ketchup")
    line = make_line(make_purchase(shopper, store), "HNZ TMT KTCP")
    disambiguation.submit_free_text(line, "heinz tomato ketchup")
    assert Product.objects.count() == 1


@pytest.mark.django_db
def test_empty_free_text_is_refused(shopper: Player) -> None:
    line = make_line(make_purchase(shopper), "MYSTERY")
    with pytest.raises(ValueError):
        disambiguation.submit_free_text(line, "   ")


# ── The window ────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_resolution_is_refused_once_the_window_closes(
    shopper: Player, store: Store
) -> None:
    """Past the window the purchase is no longer the player's to edit."""
    purchase = make_purchase(shopper, store)
    Purchase.objects.filter(pk=purchase.pk).update(
        anonymise_after=timezone.now() - timedelta(days=1)
    )
    purchase.refresh_from_db()

    product = Product.objects.create(canonical_name="Anything")
    line = make_line(purchase, "SOMETHING", product=product)
    line.purchase = purchase

    with pytest.raises(disambiguation.WindowClosedError):
        disambiguation.confirm(line)


# ── Views ─────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_purchase_detail_requires_login(client, shopper: Player) -> None:
    purchase = make_purchase(shopper)
    response = client.get(reverse("spendium:purchase_detail", args=[purchase.pk]))
    assert response.status_code == 302


@pytest.mark.django_db
def test_a_player_cannot_open_another_players_receipt(
    client, shopper: Player, other_shopper: Player
) -> None:
    """The sequential primary key is only safe because of this filter."""
    purchase = make_purchase(other_shopper)
    client.force_login(shopper)
    response = client.get(reverse("spendium:purchase_detail", args=[purchase.pk]))
    assert response.status_code == 404


@pytest.mark.django_db
def test_a_player_cannot_resolve_another_players_line(
    client, shopper: Player, other_shopper: Player
) -> None:
    product = Product.objects.create(canonical_name="Anything")
    line = make_line(make_purchase(other_shopper), "SOMETHING", product=product)
    client.force_login(shopper)
    response = client.post(reverse("spendium:confirm_line", args=[line.pk]))
    assert response.status_code == 404


@pytest.mark.django_db
def test_purchase_detail_renders(client, shopper: Player, store: Store) -> None:
    purchase = make_purchase(shopper, store)
    make_line(purchase, "HEINZ KETCHUP 750ML")
    client.force_login(shopper)
    response = client.get(reverse("spendium:purchase_detail", args=[purchase.pk]))
    assert response.status_code == 200
    assert b"HEINZ KETCHUP 750ML" in response.content


@pytest.mark.django_db
def test_confirm_view_resolves_the_line(client, shopper: Player, store: Store) -> None:
    purchase = make_purchase(shopper, store)
    product = Product.objects.create(canonical_name="Heinz Ketchup")
    line = make_line(purchase, "HEINZ 750", product=product)

    client.force_login(shopper)
    response = client.post(reverse("spendium:confirm_line", args=[line.pk]))
    assert response.status_code == 200
    line.refresh_from_db()
    assert line.disambiguation_state == PurchaseLineItem.STATE_RESOLVED


def _stream(resp) -> bytes:
    """Collect an SSE response body.

    Used by the prompt controls, which post ordinary form data rather than
    signals — `contentType: 'form'` — so there is no Datastar header or JSON
    envelope to construct. Expansion rides on the query string instead.
    """
    assert resp.status_code == 200
    return b"".join(resp.streaming_content)


# ── Getting past a queue you cannot answer ────────────────────────────────────


@pytest.mark.django_db
def test_the_receipt_offers_the_rest(client, shopper: Player, store: Store) -> None:
    purchase = make_purchase(shopper, store)
    Purchase.objects.filter(pk=purchase.pk).update(
        processing_status=Purchase.STATUS_PROCESSED
    )
    for i in range(12):
        make_line(purchase, f"ITEM {i}")

    client.force_login(shopper)
    body = client.get(reverse("spendium:purchase_detail", args=[purchase.pk])).content

    assert b"Show the other 7" in body


@pytest.mark.django_db
def test_expanding_shows_everything_pending(
    client, shopper: Player, store: Store
) -> None:
    purchase = make_purchase(shopper, store)
    for i in range(12):
        make_line(purchase, f"ITEM {i}")

    client.force_login(shopper)
    url = reverse("spendium:disambiguation_section", args=[purchase.pk])

    collapsed = _stream(client.get(url, {"expanded": "0"}))
    expanded = _stream(client.get(url, {"expanded": "1"}))

    assert collapsed.count(b"ITEM ") == 5
    assert expanded.count(b"ITEM ") == 12
    assert b"Show fewer" in expanded
    assert b"Show the other" not in expanded


@pytest.mark.django_db
def test_answering_while_expanded_stays_expanded(
    client, shopper: Player, store: Store
) -> None:
    """The one that makes expansion worth offering at all.

    The section is replaced wholesale after every answer. If expansion did not
    survive that, the list would snap back to five on each tap and the player
    would have to expand again between every question — worse than leaving them
    stuck behind the cap.

    Carried on the query string rather than in a signal, because the prompt
    controls post with `contentType: 'form'` and Datastar sends no signals in
    that mode. A signal would simply stop arriving.
    """
    purchase = make_purchase(shopper, store)
    product = Product.objects.create(canonical_name="Heinz Ketchup")
    line = make_line(purchase, "HEINZ 750", product=product, confidence="0.750")
    for i in range(11):
        make_line(purchase, f"ITEM {i}")

    client.force_login(shopper)
    url = reverse("spendium:confirm_line", args=[line.pk])
    body = _stream(client.post(f"{url}?expanded=1"))

    assert body.count(b"ITEM ") == 11, "the list collapsed back to the budget"
    assert b"expanded=1" in body, (
        "the re-rendered controls carry expanded=0, so the next answer would "
        "collapse the list"
    )


@pytest.mark.django_db
def test_no_toggle_when_everything_fits(client, shopper: Player, store: Store) -> None:
    purchase = make_purchase(shopper, store)
    Purchase.objects.filter(pk=purchase.pk).update(
        processing_status=Purchase.STATUS_PROCESSED
    )
    for i in range(3):
        make_line(purchase, f"ITEM {i}")

    client.force_login(shopper)
    body = client.get(reverse("spendium:purchase_detail", args=[purchase.pk])).content

    assert b"Show the other" not in body
    assert b"Show fewer" not in body


@pytest.mark.django_db
def test_no_toggle_when_prompting_is_disabled(
    client, shopper: Player, store: Store
) -> None:
    """A budget of zero means no questions, so there is no "rest" to offer."""
    purchase = make_purchase(shopper, store)
    Purchase.objects.filter(pk=purchase.pk).update(
        processing_status=Purchase.STATUS_PROCESSED
    )
    for i in range(12):
        make_line(purchase, f"ITEM {i}")
    config = MatchConfig.get()
    config.prompt_budget = 0
    config.save()

    client.force_login(shopper)
    body = client.get(reverse("spendium:purchase_detail", args=[purchase.pk])).content

    assert b"Show the other" not in body


@pytest.mark.django_db
def test_choose_view_reads_the_posted_form(
    client, shopper: Player, store: Store
) -> None:
    """Exercises the form plumbing, not just the service call."""
    purchase = make_purchase(shopper, store)
    guessed = Product.objects.create(canonical_name="Heinz Ketchup")
    actual = Product.objects.create(canonical_name="Heinz Mustard")
    line = make_line(purchase, "HEINZ 750", product=guessed, confidence="0.720")

    client.force_login(shopper)
    _stream(
        client.post(
            reverse("spendium:choose_line_product", args=[line.pk]),
            {"chosen_product_id": str(actual.pk)},
        )
    )
    line.refresh_from_db()
    assert line.product == actual


@pytest.mark.django_db
def test_free_text_view_reads_the_posted_form(
    client, shopper: Player, store: Store
) -> None:
    line = make_line(make_purchase(shopper, store), "MYSTERY ITEM")
    client.force_login(shopper)
    _stream(
        client.post(
            reverse("spendium:submit_line_free_text", args=[line.pk]),
            {"free_text": "Bulk Red Lentils"},
        )
    )
    line.refresh_from_db()
    assert line.product is not None
    assert line.product.canonical_name == "Bulk Red Lentils"


@pytest.mark.django_db
def test_choose_view_reports_a_missing_selection(
    client, shopper: Player, store: Store
) -> None:
    product = Product.objects.create(canonical_name="Heinz Ketchup")
    line = make_line(make_purchase(shopper, store), "HEINZ 750", product=product)
    client.force_login(shopper)
    body = _stream(
        client.post(
            reverse("spendium:choose_line_product", args=[line.pk]),
            {"chosen_product_id": ""},
        )
    )
    assert b"No product was selected" in body
    line.refresh_from_db()
    assert line.disambiguation_state == PurchaseLineItem.STATE_PENDING


# ── Accepting the reading we already show ─────────────────────────────────────
#
# The interface used to display "We read it as X" and offer no way to agree with
# it. These cover the button that closes that gap, and — more importantly — the
# things it must not quietly become: a second route into the catalogue that
# skips clustering, a way to edit a purchase past its window, or a payout.


@pytest.mark.django_db
def test_accepting_resolves_to_an_existing_product(
    shopper: Player, store: Store
) -> None:
    """The clustering path. Accepting must not mint a near-duplicate beside a
    record that already means the same thing — that is the fragmentation the
    free-text route was built to avoid, and this route would otherwise reopen
    it one tap at a time."""
    existing = Product.objects.create(canonical_name="Mystery Item")
    line = make_line(make_purchase(shopper, store), "MYSTERY ITEM")

    disambiguation.accept_reading(line)

    line.refresh_from_db()
    assert line.product == existing
    assert Product.objects.count() == 1
    assert line.disambiguation_state == PurchaseLineItem.STATE_RESOLVED
    assert line.match_tier == MatchTier.PLAYER


@pytest.mark.django_db
def test_accepting_an_unknown_reading_creates_one_unverified_product(
    shopper: Player, store: Store
) -> None:
    line = make_line(make_purchase(shopper, store), "BULK RED LENTILS")

    disambiguation.accept_reading(line)

    line.refresh_from_db()
    assert Product.objects.count() == 1
    assert line.product.canonical_name == "Bulk Red Lentils"
    assert line.product.status == Product.STATUS_UNVERIFIED


@pytest.mark.django_db
def test_two_players_accepting_the_same_string_confirm_one_alias(
    shopper: Player, other_shopper: Player, store: Store
) -> None:
    """The compounding case, and the reason one tap was judged safe enough.

    A single accept resolves only that player's line. It takes two *distinct*
    players agreeing before the string matches silently for everyone — so a
    careless tap cannot carry the catalogue on its own.
    """
    mine = make_line(make_purchase(shopper, store), "BULK RED LENTILS")
    theirs = make_line(make_purchase(other_shopper, store), "BULK RED LENTILS")

    disambiguation.accept_reading(mine)
    disambiguation.accept_reading(theirs)

    mine.refresh_from_db()
    theirs.refresh_from_db()
    assert mine.product == theirs.product, "the two accepts split into two records"
    assert Product.objects.count() == 1

    alias = ProductAlias.objects.get(
        store=store, raw_text_normalised="bulk red lentils"
    )
    assert alias.product == mine.product
    assert alias.status == ProductAlias.STATUS_AUTHORITATIVE


@pytest.mark.django_db
def test_a_line_with_no_reading_cannot_be_accepted(
    shopper: Player, store: Store
) -> None:
    line = make_line(make_purchase(shopper, store), "???")
    PurchaseLineItem.objects.filter(pk=line.pk).update(interpreted_name="")
    line.refresh_from_db()

    with pytest.raises(ValueError):
        disambiguation.accept_reading(line)

    line.refresh_from_db()
    assert line.disambiguation_state == PurchaseLineItem.STATE_PENDING


@pytest.mark.django_db
def test_the_button_is_not_offered_without_a_reading(
    client, shopper: Player, store: Store
) -> None:
    purchase = make_purchase(shopper, store)
    # The prompts render only once the receipt has been read; a pending purchase
    # shows "We're still reading this" instead, and this would assert nothing.
    Purchase.objects.filter(pk=purchase.pk).update(
        processing_status=Purchase.STATUS_PROCESSED
    )
    line = make_line(purchase, "???")
    PurchaseLineItem.objects.filter(pk=line.pk).update(interpreted_name="")

    client.force_login(shopper)
    response = client.get(reverse("spendium:purchase_detail", args=[purchase.pk]))

    assert reverse("spendium:accept_line_reading", args=[line.pk]).encode() not in (
        response.content
    )
    assert b"We couldn't place this one." in response.content


@pytest.mark.django_db
def test_accepting_past_the_window_changes_nothing(
    shopper: Player, store: Store
) -> None:
    """A closed window blocks the whole route, end to end.

    Note what this does *not* prove. `accept_reading` calls
    `_require_open_window` itself, but so does `submit_free_text` underneath it,
    so this passes with either guard present — verified by deleting the one in
    `accept_reading` and watching this still go green. The redundant guard is
    covered separately below.
    """
    purchase = make_purchase(shopper, store)
    line = make_line(purchase, "BULK RED LENTILS")
    Purchase.objects.filter(pk=purchase.pk).update(
        anonymise_after=timezone.now() - timedelta(days=1)
    )
    line.refresh_from_db()

    with pytest.raises(disambiguation.WindowClosedError):
        disambiguation.accept_reading(line)

    line.refresh_from_db()
    assert line.product is None
    assert line.disambiguation_state == PurchaseLineItem.STATE_PENDING
    assert not Product.objects.exists()


@pytest.mark.django_db
def test_accept_checks_the_window_itself(
    shopper: Player, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The redundant guard, tested where it can actually be seen.

    `accept_reading` delegates to `submit_free_text`, which checks the window
    too — so the end-to-end test above cannot tell the two apart and passes even
    with this guard deleted. Stubbing the delegate leaves `accept_reading`'s own
    check as the only thing that can raise.

    Kept redundant on purpose. This is a public entry point, and the variant
    discussed in plans/accept-the-suggested-description.md — resolving only into
    existing records — would stop going through `submit_free_text` at all.
    """
    purchase = make_purchase(shopper, store)
    line = make_line(purchase, "BULK RED LENTILS")
    Purchase.objects.filter(pk=purchase.pk).update(
        anonymise_after=timezone.now() - timedelta(days=1)
    )
    line.refresh_from_db()

    monkeypatch.setattr(
        disambiguation,
        "submit_free_text",
        lambda *args, **kwargs: pytest.fail("the window was never checked"),
    )
    with pytest.raises(disambiguation.WindowClosedError):
        disambiguation.accept_reading(line)


@pytest.mark.django_db
def test_accepting_does_not_pay_the_player(shopper: Player, store: Store) -> None:
    """Points settle when the receipt is read and are never revisited.

    Pinned because the prompt is a request for unpaid work on everyone else's
    behalf, and that is only true for as long as it stays true. If resolving a
    line ever started paying, these prompts would become a rewarded action —
    which is a different feature with a different abuse surface.
    """
    purchase = make_purchase(shopper, store)
    Purchase.objects.filter(pk=purchase.pk).update(points_awarded=Decimal("5.00"))
    line = make_line(purchase, "BULK RED LENTILS")
    before = Player.objects.get(pk=shopper.pk).total_points

    disambiguation.accept_reading(line)

    purchase.refresh_from_db()
    assert purchase.points_awarded == Decimal("5.00")
    assert Player.objects.get(pk=shopper.pk).total_points == before


@pytest.mark.django_db
def test_accept_view_resolves_the_line(client, shopper: Player, store: Store) -> None:
    purchase = make_purchase(shopper, store)
    line = make_line(purchase, "BULK RED LENTILS")

    client.force_login(shopper)
    response = client.post(reverse("spendium:accept_line_reading", args=[line.pk]))

    assert response.status_code == 200
    line.refresh_from_db()
    assert line.disambiguation_state == PurchaseLineItem.STATE_RESOLVED
    assert line.product.canonical_name == "Bulk Red Lentils"


@pytest.mark.django_db
def test_accept_view_is_scoped_to_the_owner(
    client, shopper: Player, other_shopper: Player, store: Store
) -> None:
    line = make_line(make_purchase(other_shopper, store), "BULK RED LENTILS")
    client.force_login(shopper)
    response = client.post(reverse("spendium:accept_line_reading", args=[line.pk]))
    assert response.status_code == 404


@pytest.mark.django_db
def test_accept_view_ignores_posted_text(client, shopper: Player, store: Store) -> None:
    """The endpoint accepts what is stored, never what the client sends.

    Otherwise it would be a second, cheaper-looking route for putting arbitrary
    text into the catalogue, sitting behind a button labelled as agreement.
    """
    line = make_line(make_purchase(shopper, store), "BULK RED LENTILS")

    client.force_login(shopper)
    _stream(
        client.post(
            reverse("spendium:accept_line_reading", args=[line.pk]),
            {"free_text": "Something Else Entirely"},
        )
    )

    line.refresh_from_db()
    assert line.product.canonical_name == "Bulk Red Lentils"
