from django.contrib import admin

from .models import (
    BlacklistHistory,
    Candidate,
    Election,
    JurisdictionDuplicateFlag,
    JurisdictionFollow,
    OfficeHistory,
    VoteDeclaration,
    Jurisdiction,
)


class JurisdictionDuplicateFlagInline(admin.TabularInline):
    model = JurisdictionDuplicateFlag
    extra = 0
    fk_name = "flagged_jurisdiction"


class JurisdictionFollowInline(admin.TabularInline):
    model = JurisdictionFollow
    extra = 0


@admin.register(Jurisdiction)
class JurisdictionAdmin(admin.ModelAdmin):
    list_display = ("name", "level", "parent", "status", "active_engagement", "created_at")
    list_filter = ("status", "level")
    readonly_fields = ("sqid", "status", "active_engagement", "deprecated_at", "created_at")
    inlines = [JurisdictionDuplicateFlagInline, JurisdictionFollowInline]


class OfficeHistoryInline(admin.TabularInline):
    model = OfficeHistory
    extra = 0


class BlacklistHistoryInline(admin.TabularInline):
    model = BlacklistHistory
    extra = 0
    can_delete = False


class VoteDeclarationInline(admin.TabularInline):
    model = VoteDeclaration
    extra = 0


@admin.register(Candidate)
class CandidateAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "office",
        "jurisdiction",
        "election",
        "current_rating",
        "is_blacklisted",
        "engagement_count",
        "created_at",
    )
    list_filter = ("is_blacklisted", "jurisdiction")
    readonly_fields = (
        "sqid",
        "current_rating",
        "is_blacklisted",
        "blacklisted_at",
        "engagement_count",
        "created_at",
    )
    inlines = [OfficeHistoryInline, BlacklistHistoryInline, VoteDeclarationInline]


@admin.register(Election)
class ElectionAdmin(admin.ModelAdmin):
    list_display = ("name", "jurisdiction", "election_date", "created_at")
    readonly_fields = ("sqid", "created_at")
