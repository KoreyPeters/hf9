from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import EmailVerification, Membership, PasskeyCredential, Player


@admin.register(Player)
class PlayerAdmin(UserAdmin):
    readonly_fields = (*UserAdmin.readonly_fields, "sqid", "total_points")
    fieldsets = (
        *UserAdmin.fieldsets,
        ("HF", {"fields": ("display_name", "email_verified", "email_verified_at", "jurisdiction_country", "jurisdiction_region", "total_points", "sqid")}),
    )


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ("player", "is_active", "started_at", "expires_at")
    list_filter = ("is_active",)


@admin.register(EmailVerification)
class EmailVerificationAdmin(admin.ModelAdmin):
    list_display = ("player", "expires_at", "verified_at")
    list_filter = ("verified_at",)
    readonly_fields = ("token_hash",)


@admin.register(PasskeyCredential)
class PasskeyCredentialAdmin(admin.ModelAdmin):
    list_display = ("player", "device_name", "aaguid", "created_at", "last_used_at")
    readonly_fields = ("credential_id", "public_key", "sign_count", "aaguid")
