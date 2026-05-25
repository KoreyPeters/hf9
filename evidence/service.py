from .models import Evidence


def recompute_evidence_status(evidence: Evidence) -> None:
    flag_count = evidence.flags.count()
    threshold = max(1, evidence.net_usefulness_score / 10)
    if flag_count >= threshold and evidence.status == Evidence.STATUS_VISIBLE:
        Evidence.objects.filter(pk=evidence.pk).update(status=Evidence.STATUS_HIDDEN)


def recompute_usefulness_score(evidence: Evidence) -> None:
    useful = evidence.usefulness_votes.filter(is_useful=True).count()
    not_useful = evidence.usefulness_votes.filter(is_useful=False).count()
    Evidence.objects.filter(pk=evidence.pk).update(net_usefulness_score=useful - not_useful)
