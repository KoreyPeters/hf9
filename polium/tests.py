from decimal import Decimal
from unittest.mock import patch

import polium.task_views
import pytest

from core.tasks import _registry
from polium.models import BlacklistHistory, Candidate, Jurisdiction


@pytest.fixture
def jurisdiction(db: None) -> Jurisdiction:
    return Jurisdiction.objects.create(name="Test Jurisdiction", level="federal")


@pytest.fixture
def candidate(db: None, jurisdiction: Jurisdiction) -> Candidate:
    return Candidate.objects.create(
        name="Test Candidate", jurisdiction=jurisdiction, office="Senator"
    )


@pytest.mark.django_db
def test_blacklist_entry_below_threshold(candidate: Candidate) -> None:
    with patch("polium.task_views.compute_rating", return_value=0.10):
        _registry["update-candidate-rating"](candidate_id=candidate.pk)
    candidate.refresh_from_db()
    assert candidate.is_blacklisted is True
    entry = BlacklistHistory.objects.get(candidate=candidate)
    assert entry.rating_at_blacklist == Decimal("0.1")


@pytest.mark.django_db
def test_blacklist_not_triggered_above_entry(candidate: Candidate) -> None:
    with patch("polium.task_views.compute_rating", return_value=0.30):
        _registry["update-candidate-rating"](candidate_id=candidate.pk)
    candidate.refresh_from_db()
    assert candidate.is_blacklisted is False
    assert BlacklistHistory.objects.filter(candidate=candidate).count() == 0


@pytest.mark.django_db
def test_blacklist_lift_above_exit_threshold(candidate: Candidate) -> None:
    from django.utils import timezone

    now = timezone.now()
    Candidate.objects.filter(pk=candidate.pk).update(is_blacklisted=True, blacklisted_at=now)
    BlacklistHistory.objects.create(
        candidate=candidate, blacklisted_at=now, rating_at_blacklist=Decimal("0.10")
    )
    with patch("polium.task_views.compute_rating", return_value=0.60):
        _registry["update-candidate-rating"](candidate_id=candidate.pk)
    candidate.refresh_from_db()
    assert candidate.is_blacklisted is False
    entry = BlacklistHistory.objects.get(candidate=candidate)
    assert entry.lifted_at is not None


@pytest.mark.django_db
def test_blacklist_not_lifted_between_thresholds(candidate: Candidate) -> None:
    from django.utils import timezone

    now = timezone.now()
    Candidate.objects.filter(pk=candidate.pk).update(is_blacklisted=True, blacklisted_at=now)
    BlacklistHistory.objects.create(
        candidate=candidate, blacklisted_at=now, rating_at_blacklist=Decimal("0.10")
    )
    with patch("polium.task_views.compute_rating", return_value=0.40):
        _registry["update-candidate-rating"](candidate_id=candidate.pk)
    candidate.refresh_from_db()
    assert candidate.is_blacklisted is True
    entry = BlacklistHistory.objects.get(candidate=candidate)
    assert entry.lifted_at is None


@pytest.mark.django_db
def test_no_rating_returns_early(candidate: Candidate) -> None:
    with patch("polium.task_views.compute_rating", return_value=None):
        _registry["update-candidate-rating"](candidate_id=candidate.pk)
    fresh = Candidate.objects.get(pk=candidate.pk)
    assert fresh.current_rating == 0
    assert BlacklistHistory.objects.filter(candidate=candidate).count() == 0


@pytest.mark.django_db
def test_registry_contains_update_candidate_rating() -> None:
    assert "update-candidate-rating" in _registry


@pytest.mark.django_db
def test_task_updates_current_rating(candidate: Candidate) -> None:
    with patch("polium.task_views.compute_rating", return_value=0.75):
        _registry["update-candidate-rating"](candidate_id=candidate.pk)
    candidate.refresh_from_db()
    assert candidate.current_rating == Decimal("0.75")


@pytest.mark.django_db
def test_task_callable_directly_without_http(candidate: Candidate) -> None:
    with patch("polium.task_views.compute_rating", return_value=0.50):
        _registry["update-candidate-rating"](candidate_id=candidate.pk)
