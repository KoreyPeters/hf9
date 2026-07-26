"""Shared fixtures for the Spendium tests.

The important one is `no_real_model_calls`. Uploading a receipt enqueues its
extraction, and `enqueue` runs inline under DEBUG, so a test that merely calls
`accept_upload` will reach straight through to Vertex AI unless something stops
it. Failing loudly on a real client is much better than a test that quietly
makes a network call, times out, and records the receipt as unreadable — which
looks like a bug in the pipeline rather than a gap in the test.
"""

from collections.abc import Callable

import pytest

from spendium import adjudication, extraction


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeModels:
    def __init__(self, payloads: list[str]) -> None:
        self._payloads = list(payloads)
        self.calls: list[dict] = []

    def generate_content(
        self, *, model: str, contents: list, config: object
    ) -> _FakeResponse:
        self.calls.append({"model": model, "config": config, "contents": contents})
        payload = (
            self._payloads.pop(0) if len(self._payloads) > 1 else self._payloads[0]
        )
        return _FakeResponse(payload)


class FakeClient:
    """Stands in for genai.Client, returning each recorded payload in turn."""

    def __init__(self, *payloads: str) -> None:
        self.models = _FakeModels(list(payloads))


@pytest.fixture
def voter(db: None):
    """Someone to attribute an alias vote to.

    Votes are per player, so tests cannot record one without saying who cast
    it — which is the point: two confirmations has to mean two people.
    """
    from accounts.models import Player

    return Player.objects.create_user(username="voter", email="voter@example.com")


@pytest.fixture
def other_voter(db: None):
    from accounts.models import Player

    return Player.objects.create_user(
        username="other-voter", email="other-voter@example.com"
    )


@pytest.fixture
def third_voter(db: None):
    from accounts.models import Player

    return Player.objects.create_user(
        username="third-voter", email="third-voter@example.com"
    )


@pytest.fixture(autouse=True)
def no_real_model_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse() -> None:
        raise AssertionError(
            "A test tried to construct a real Gemini client. Use the "
            "`fake_model` fixture, or pass client= explicitly."
        )

    monkeypatch.setattr(extraction, "_client", refuse)
    monkeypatch.setattr(adjudication, "_client", refuse)


@pytest.fixture
def fake_model(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FakeClient]:
    """Install a fake model for code paths that build their own client.

    Needed wherever the call happens behind a task rather than through a
    parameter — `accept_upload` being the main one.
    """

    def install(*payloads: str) -> FakeClient:
        client = FakeClient(*payloads)
        monkeypatch.setattr(extraction, "_client", lambda: client)
        monkeypatch.setattr(adjudication, "_client", lambda: client)
        return client

    return install
