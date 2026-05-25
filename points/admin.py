from django.contrib import admin
from django.http import HttpRequest

from .models import PointTransaction


@admin.register(PointTransaction)
class PointTransactionAdmin(admin.ModelAdmin):
    list_display = ("player", "amount", "reason", "content_type", "object_id", "created_at")
    list_filter = ("reason",)
    readonly_fields = (
        "player",
        "amount",
        "reason",
        "content_type",
        "object_id",
        "source",
        "created_at",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: object = None) -> bool:
        return False
