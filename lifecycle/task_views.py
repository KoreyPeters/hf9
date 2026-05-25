from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from core.tasks import task
from lifecycle.models import LifecycleMixin


@task("check-deprecations")
def check_deprecations() -> None:
    from polium.models import Jurisdiction

    for obj in Jurisdiction.objects.filter(status=LifecycleMixin.STATUS_ACTIVE):
        if obj.should_deprecate():
            Jurisdiction.objects.filter(pk=obj.pk).update(
                status=LifecycleMixin.STATUS_DEPRECATED,
                deprecated_at=timezone.now(),
            )


@task("check-deletions")
def check_deletions() -> None:
    from polium.models import Jurisdiction

    threshold = timezone.now() - timedelta(days=settings.LIFECYCLE["DELETION_DAYS"])
    for obj in Jurisdiction.objects.filter(
        status=LifecycleMixin.STATUS_DEPRECATED,
        active_engagement=0,
        deprecated_at__lte=threshold,
    ):
        obj.delete()
