from typing import Any

from django.conf import settings
from django.db import models
from sqids import Sqids


class SqidMixin(models.Model):
    sqid = models.CharField(max_length=20, unique=True, blank=True, db_index=True)

    def generate_sqid(self) -> str:
        raise NotImplementedError(
            "Subclasses must implement generate_sqid() using their own salt from settings."
        )

    def save(self, *args: Any, **kwargs: Any) -> None:
        super().save(*args, **kwargs)
        if not self.sqid:
            self.sqid = self.generate_sqid()
            type(self).objects.filter(pk=self.pk).update(sqid=self.sqid)

    class Meta:
        abstract = True
