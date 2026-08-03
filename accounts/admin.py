from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import EmailVerification, Membership, PasskeyCredential, Player


@admin.register(Player)
class PlayerAdmin(UserAdmin):
    """The player list, rebuilt around what this app actually stores.

    Inherited `UserAdmin.list_display` is username, email, first name, last
    name, staff status. On this model that is two permanently blank columns —
    nothing writes `first_name` or `last_name`, the app uses `display_name` —
    and a username column showing `uuid.uuid4().hex[:20]`, so every row looks
    identical and none of it distinguishes anybody.

    Which mattered. Twenty bot signups against a harvested email list sat in
    this list looking like a rendering quirk, and the two fields that would have
    settled it in one glance — whether they ever verified, and whether they all
    arrived at once — were neither shown nor filterable. See
    `plans/bot-signups.md`.
    """

    list_display = (
        "email",
        "display_name",
        "email_verified",
        "date_joined",
        "total_points",
        "is_staff",
    )
    # `email_verified` first because it is the single most useful cut: an
    # account that never verified did nothing, and a burst of them arrived
    # together. `date_joined` is the hierarchy rather than a filter for the same
    # reason — bursts are the tell, and a list of days makes them visible.
    list_filter = ("email_verified", "is_staff", "is_active", "date_joined")
    date_hierarchy = "date_joined"
    search_fields = ("email", "display_name", "username")
    ordering = ("-date_joined",)

    readonly_fields = (*UserAdmin.readonly_fields, "sqid", "total_points")
    fieldsets = (
        *UserAdmin.fieldsets,
        (
            "HF",
            {
                "fields": (
                    "display_name",
                    "email_verified",
                    "email_verified_at",
                    "jurisdiction_country",
                    "jurisdiction_region",
                    "total_points",
                    "sqid",
                )
            },
        ),
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
