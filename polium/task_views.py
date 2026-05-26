from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from core.tasks import task
from surveys.ratings import compute_rating


@task("update-candidate-rating")
def update_candidate_rating(candidate_id: int) -> None:
    from .models import Candidate

    candidate = Candidate.objects.select_for_update().get(pk=candidate_id)
    rating = compute_rating(candidate)
    if rating is None:
        return

    new_rating = Decimal(str(round(rating, 2)))
    Candidate.objects.filter(pk=candidate_id).update(current_rating=new_rating)

    if (
        candidate.is_endorsed
        and candidate.election_win_confirmed
        and not candidate.is_blacklisted
        and candidate.pre_election_rating_snapshot is not None
    ):
        threshold = candidate.pre_election_rating_snapshot * Decimal(
            str(settings.BLACKLIST_RATIO)
        )
        if new_rating < threshold and candidate.rating_below_threshold_since is None:
            Candidate.objects.filter(pk=candidate_id).update(
                rating_below_threshold_since=timezone.now()
            )
        elif new_rating >= threshold and candidate.rating_below_threshold_since is not None:
            Candidate.objects.filter(pk=candidate_id).update(
                rating_below_threshold_since=None
            )
