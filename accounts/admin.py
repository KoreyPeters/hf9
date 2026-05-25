from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import Membership, Player


@admin.register(Player)
class PlayerAdmin(UserAdmin):
    readonly_fields = (*UserAdmin.readonly_fields, "sqid", "total_points")
    fieldsets = (
        *UserAdmin.fieldsets,
        ("HF", {"fields": ("total_points", "sqid")}),
    )


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ("player", "is_active", "started_at", "expires_at")
    list_filter = ("is_active",)
