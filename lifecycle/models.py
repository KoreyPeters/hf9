from django.conf import settings
from django.db import models


class LifecycleMixin(models.Model):
    STATUS_ACTIVE = "active"
    STATUS_DEPRECATED = "deprecated"
    STATUS_DELETED = "deleted"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_DEPRECATED, "Deprecated"),
        (STATUS_DELETED, "Deleted"),
    ]

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    active_engagement = models.PositiveIntegerField(default=0)
    deprecated_at = models.DateTimeField(null=True, blank=True)

    @property
    def flag_count(self) -> int:
        raise NotImplementedError("Concrete models must implement flag_count.")

    def should_deprecate(self) -> bool:
        ratio: int = settings.LIFECYCLE["DEPRECATION_RATIO"]
        if self.active_engagement == 0:
            return self.flag_count > 0
        return self.flag_count >= self.active_engagement / ratio

    class Meta:
        abstract = True
