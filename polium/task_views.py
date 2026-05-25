from decimal import Decimal

from django.utils import timezone

from core.tasks import task
from surveys.ratings import compute_rating

BLACKLIST_ENTRY = Decimal("0.25")
BLACKLIST_EXIT = Decimal("0.50")


@task("update-candidate-rating")
def update_candidate_rating(candidate_id: int) -> None:
    from .models import BlacklistHistory, Candidate

    candidate = Candidate.objects.select_for_update().get(pk=candidate_id)
    rating = compute_rating(candidate)
    if rating is None:
        return

    new_rating = Decimal(str(round(rating, 2)))
    Candidate.objects.filter(pk=candidate_id).update(current_rating=new_rating)

    if not candidate.is_blacklisted and new_rating < BLACKLIST_ENTRY:
        now = timezone.now()
        Candidate.objects.filter(pk=candidate_id).update(is_blacklisted=True, blacklisted_at=now)
        BlacklistHistory.objects.create(
            candidate=candidate,
            blacklisted_at=now,
            rating_at_blacklist=new_rating,
        )
    elif candidate.is_blacklisted and new_rating >= BLACKLIST_EXIT:
        now = timezone.now()
        Candidate.objects.filter(pk=candidate_id).update(is_blacklisted=False, blacklisted_at=None)
        BlacklistHistory.objects.filter(
            candidate=candidate, lifted_at__isnull=True
        ).update(lifted_at=now, rating_at_lift=new_rating)
