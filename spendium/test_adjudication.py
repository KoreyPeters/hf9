"""Tier 2 adjudication: the second, targeted model call.

Fixture responses throughout — no network. What is being tested is our handling
of the model's answers, particularly the ways a wrong or malformed answer must
be prevented from reaching the catalogue.
"""

import json
from decimal import Decimal

import pytest

from accounts.models import Player
from spendium import adjudication, metrics
from spendium.models import (
    MatchConfig,
    MatchTier,
    Product,
    ProductAlias,
    PurchaseLineItem,
    Store,
)
from spendium.conftest import FakeClient
from spendium.test_extraction import (
    receipt_payload,
    upload_and_process,
)


@pytest.fixture
def shopper(db: None) -> Player:
    return Player.objects.create_user(username="shopper", email="shopper@example.com")


@pytest.fixture(autouse=True)
def isolated_media(settings, tmp_path) -> None:
    settings.MEDIA_ROOT = tmp_path


def item(index: int = 0, candidates: list[tuple[int, str]] | None = None):
    return adjudication.AdjudicationItem(
        index=index,
        raw_text="PC BLK LBL COFF 300G",
        interpreted_name="President's Choice Black Label Ground Coffee 300g",
        candidates=candidates
        if candidates is not None
        else [(1, "PC Black Label Coffee")],
    )


def decisions_payload(*decisions: dict) -> str:
    return json.dumps({"decisions": list(decisions)})


# ── Prompt and schema ─────────────────────────────────────────────────────────


def test_schema_makes_none_of_these_a_first_class_answer() -> None:
    """Forcing a choice would guarantee wrong ones on genuinely novel products."""
    properties = ADJ_ITEM_PROPS()
    assert "none_of_these" in properties
    assert "none_of_these" in ADJ_REQUIRED()
    assert "confident" in ADJ_REQUIRED()


def ADJ_ITEM_PROPS() -> dict:
    return adjudication.ADJUDICATION_SCHEMA["properties"]["decisions"]["items"][
        "properties"
    ]


def ADJ_REQUIRED() -> list[str]:
    return adjudication.ADJUDICATION_SCHEMA["properties"]["decisions"]["items"][
        "required"
    ]


def test_prompt_states_that_declining_is_acceptable() -> None:
    """A model that fears saying 'no' will invent matches."""
    instruction = adjudication.SYSTEM_INSTRUCTION.lower()
    assert "none_of_these is a good answer" in instruction


def test_prompt_carries_the_size_collapsing_rule() -> None:
    """Catalogue granularity is the product line, so size must not block a match."""
    assert "size" in adjudication.SYSTEM_INSTRUCTION.lower()


def test_prompt_includes_every_item_and_candidate() -> None:
    prompt = adjudication.build_prompt(
        [item(0, [(7, "Alpha Coffee")]), item(1, [(9, "Beta Coffee")])]
    )
    assert "Item 0" in prompt and "Item 1" in prompt
    assert "id 7: Alpha Coffee" in prompt
    assert "id 9: Beta Coffee" in prompt
    assert "PC BLK LBL COFF 300G" in prompt


# ── Parsing and guarding the response ─────────────────────────────────────────


def test_confident_choice_is_resolved() -> None:
    parsed = adjudication.parse_response(
        decisions_payload(
            {"index": 0, "product_id": 1, "none_of_these": False, "confident": True}
        )
    )
    assert parsed[0].resolved is True
    assert parsed[0].product_id == 1


def test_none_of_these_clears_the_product() -> None:
    parsed = adjudication.parse_response(
        decisions_payload(
            {"index": 0, "product_id": 1, "none_of_these": True, "confident": True}
        )
    )
    assert parsed[0].product_id is None
    assert parsed[0].resolved is False


def test_unconfident_choice_is_not_resolved() -> None:
    """An unconfident decision is discarded, so overstating certainty gains nothing."""
    parsed = adjudication.parse_response(
        decisions_payload(
            {"index": 0, "product_id": 1, "none_of_these": False, "confident": False}
        )
    )
    assert parsed[0].resolved is False


def test_decision_without_an_index_is_dropped() -> None:
    parsed = adjudication.parse_response(
        decisions_payload({"product_id": 1, "none_of_these": False, "confident": True})
    )
    assert parsed == []


def test_malformed_response_yields_no_decisions() -> None:
    assert adjudication.parse_response(json.dumps([1, 2, 3])) == []


def test_decision_for_an_unknown_item_is_ignored() -> None:
    client = FakeClient(
        decisions_payload(
            {"index": 99, "product_id": 1, "none_of_these": False, "confident": True}
        )
    )
    assert adjudication.adjudicate([item(0)], client=client) == {}


def test_invented_product_id_is_refused() -> None:
    """Honouring an id we never offered would attach the line to a random record."""
    client = FakeClient(
        decisions_payload(
            {"index": 0, "product_id": 4242, "none_of_these": False, "confident": True}
        )
    )
    assert adjudication.adjudicate([item(0)], client=client) == {}


def test_no_items_means_no_call() -> None:
    client = FakeClient(decisions_payload())
    assert adjudication.adjudicate([], client=client) == {}
    assert client.models.calls == []


def test_one_call_covers_the_whole_receipt() -> None:
    """Batching keeps this to a single round trip however long the receipt is."""
    client = FakeClient(
        decisions_payload(
            {"index": 0, "product_id": 1, "none_of_these": False, "confident": True},
            {"index": 1, "product_id": 1, "none_of_these": True, "confident": True},
        )
    )
    adjudication.adjudicate([item(0), item(1)], client=client)
    assert len(client.models.calls) == 1


def test_adjudication_sends_no_image() -> None:
    """Tier 2 is text only — the image may already have been deleted."""
    client = FakeClient(decisions_payload())
    adjudication.adjudicate([item(0)], client=client)
    contents = client.models.calls[0]["contents"]
    assert contents
    assert all(isinstance(part, str) for part in contents)


# ── Integration with recording a receipt ──────────────────────────────────────


@pytest.fixture
def near_miss_catalogue(db: None) -> Product:
    """A product close enough to be a candidate but not to win on similarity."""
    return Product.objects.create(canonical_name="Colgate Cavity Toothpaste Regular")


@pytest.mark.django_db
def test_adjudication_resolves_a_tier1_miss(
    shopper: Player, near_miss_catalogue: Product, fake_model
) -> None:
    config = MatchConfig.get()
    config.weak_match_score = 95  # force the fuzzy match to fall short
    config.noise_floor_score = 10
    config.save()

    client = fake_model(
        receipt_payload(),
        decisions_payload(
            {
                "index": 0,
                "product_id": near_miss_catalogue.pk,
                "none_of_these": False,
                "confident": True,
            }
        ),
    )
    upload_and_process(shopper, client)

    # Two calls: extraction, then adjudication. Guards against this passing
    # because the residual was never sent at all.
    assert len(client.models.calls) == 2
    line = PurchaseLineItem.objects.get(raw_text="TP-COLG-250")
    assert line.product == near_miss_catalogue
    assert line.match_tier == MatchTier.ADJUDICATED


@pytest.mark.django_db
def test_adjudication_writes_a_provisional_alias(
    shopper: Player, near_miss_catalogue: Product, fake_model
) -> None:
    """So the next receipt with this string resolves at Tier 0, for free."""
    config = MatchConfig.get()
    config.weak_match_score = 95
    config.noise_floor_score = 10
    config.save()

    client = fake_model(
        receipt_payload(),
        decisions_payload(
            {
                "index": 0,
                "product_id": near_miss_catalogue.pk,
                "none_of_these": False,
                "confident": True,
            }
        ),
    )
    upload_and_process(shopper, client)

    alias = ProductAlias.objects.get(raw_text_normalised="tp colg 250")
    assert alias.product == near_miss_catalogue
    assert alias.source == ProductAlias.SOURCE_ADJUDICATION
    assert alias.status == ProductAlias.STATUS_PROVISIONAL


@pytest.mark.django_db
def test_adjudicated_line_still_prompts_the_player(
    shopper: Player, near_miss_catalogue: Product, fake_model
) -> None:
    """The model is a strong signal, never a confirming witness."""
    config = MatchConfig.get()
    config.weak_match_score = 95
    config.noise_floor_score = 10
    config.save()

    client = fake_model(
        receipt_payload(),
        decisions_payload(
            {
                "index": 0,
                "product_id": near_miss_catalogue.pk,
                "none_of_these": False,
                "confident": True,
            }
        ),
    )
    upload_and_process(shopper, client)

    line = PurchaseLineItem.objects.get(raw_text="TP-COLG-250")
    assert line.disambiguation_state == PurchaseLineItem.STATE_PENDING


@pytest.mark.django_db
def test_no_candidates_means_no_adjudication_call(shopper: Player, fake_model) -> None:
    """An empty candidate list gives the model nothing to choose from."""
    client = fake_model(receipt_payload())
    upload_and_process(shopper, client)
    assert len(client.models.calls) == 1


@pytest.mark.django_db
def test_matched_lines_are_not_adjudicated(
    shopper: Player, store: Store, fake_model
) -> None:
    """Tier 2 only sees what Tiers 0 and 1 could not resolve."""
    product = Product.objects.create(
        canonical_name="Colgate Toothpaste Bright Whitening"
    )
    Product.objects.create(canonical_name="Heinz Tomato Ketchup")
    ProductAlias.objects.create(product=product, store=store, raw_text="TP-COLG-250")

    client = fake_model(receipt_payload())
    upload_and_process(shopper, client)
    assert len(client.models.calls) == 1


@pytest.mark.django_db
def test_adjudication_can_be_disabled(
    shopper: Player, near_miss_catalogue: Product, fake_model
) -> None:
    config = MatchConfig.get()
    config.weak_match_score = 95
    config.noise_floor_score = 10
    config.adjudication_candidates = 0
    config.save()

    client = fake_model(receipt_payload())
    upload_and_process(shopper, client)
    assert len(client.models.calls) == 1


@pytest.mark.django_db
def test_a_demoted_alias_is_not_resurrected_by_the_model(
    shopper: Player, store: Store, near_miss_catalogue: Product, fake_model, voter
) -> None:
    """A string players have contradicted must not be quietly reclaimed.

    Tier 0 skips demoted aliases, so the line becomes a residual and reaches
    adjudication — the one path on which the model can meet a string that
    already has a row. Uniqueness is global per (store, string), so writing
    here would reassign the string for every future receipt, overturning a
    player's correction with a guess.
    """
    incumbent = Product.objects.create(canonical_name="Incumbent Product")
    alias = ProductAlias.objects.create(
        product=incumbent, store=store, raw_text="TP-COLG-250"
    )
    alias.contradict(voter)
    assert alias.status == ProductAlias.STATUS_DEMOTED

    config = MatchConfig.get()
    config.weak_match_score = 95
    config.noise_floor_score = 10
    config.save()

    client = fake_model(
        receipt_payload(),
        decisions_payload(
            {
                "index": 0,
                "product_id": near_miss_catalogue.pk,
                "none_of_these": False,
                "confident": True,
            }
        ),
    )
    upload_and_process(shopper, client)

    # The residual really did reach adjudication, so the guard below is being
    # exercised rather than skipped.
    assert len(client.models.calls) == 2
    line = PurchaseLineItem.objects.get(raw_text="TP-COLG-250")
    assert line.product == near_miss_catalogue

    alias.refresh_from_db()
    assert alias.product == incumbent
    assert alias.status == ProductAlias.STATUS_DEMOTED
    assert ProductAlias.objects.filter(raw_text_normalised="tp colg 250").count() == 1


@pytest.fixture
def store(db: None) -> Store:
    return Store.objects.create(name="Shoppers Drug Mart")


# ── Accuracy tracking ─────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_accuracy_is_none_before_any_player_rules(store: Store) -> None:
    """Silence is not agreement, and reporting zero would be worse than nothing."""
    product = Product.objects.create(canonical_name="Anything")
    ProductAlias.objects.create(
        product=product,
        store=store,
        raw_text="X",
        source=ProductAlias.SOURCE_ADJUDICATION,
    )
    assert metrics.adjudication_accuracy().accuracy is None


@pytest.mark.django_db
def test_accuracy_counts_confirmations_and_contradictions(store: Store, voter) -> None:
    product = Product.objects.create(canonical_name="Anything")
    right = ProductAlias.objects.create(
        product=product,
        store=store,
        raw_text="RIGHT",
        source=ProductAlias.SOURCE_ADJUDICATION,
    )
    wrong = ProductAlias.objects.create(
        product=product,
        store=store,
        raw_text="WRONG",
        source=ProductAlias.SOURCE_ADJUDICATION,
    )
    right.confirm(voter)
    wrong.contradict(voter)

    accuracy = metrics.adjudication_accuracy()
    assert accuracy.confirmed == 1
    assert accuracy.contradicted == 1
    assert accuracy.accuracy == 0.5


@pytest.mark.django_db
def test_accuracy_ignores_player_sourced_aliases(store: Store, voter) -> None:
    """Measuring the model against aliases players wrote would be circular."""
    product = Product.objects.create(canonical_name="Anything")
    alias = ProductAlias.objects.create(
        product=product, store=store, raw_text="P", source=ProductAlias.SOURCE_PLAYER
    )
    alias.confirm(voter)
    assert metrics.adjudication_accuracy().judged == 0


@pytest.mark.django_db
def test_tier_distribution_reports_every_tier(shopper: Player, fake_model) -> None:
    client = fake_model(receipt_payload())
    upload_and_process(shopper, client)
    distribution = metrics.tier_distribution()
    assert set(distribution) == {tier.value for tier in MatchTier}
    assert distribution[MatchTier.UNMATCHED.value] == 2


@pytest.mark.django_db
def test_prompt_rate_is_none_with_no_line_items() -> None:
    assert metrics.prompt_rate() is None


@pytest.mark.django_db
def test_prompt_rate_reflects_pending_disambiguations(
    shopper: Player, fake_model
) -> None:
    client = fake_model(receipt_payload())
    purchase = upload_and_process(shopper, client)
    assert purchase.total == Decimal("9.58")
    assert metrics.prompt_rate() == 1.0
