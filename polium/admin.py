from decimal import Decimal

from django.conf import settings
from django.contrib import admin, messages
from django.http import HttpRequest
from django.template.response import TemplateResponse
from django.utils import timezone

from .models import (
    BlacklistHistory,
    Candidate,
    Election,
    Jurisdiction,
    JurisdictionDuplicateFlag,
    JurisdictionFollow,
    OfficeHistory,
    VoteDeclaration,
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
    readonly_fields = (
        "blacklisted_at",
        "rating_at_blacklist",
        "blacklisted_by",
        "forum_discussion_url",
        "reason",
    )


class VoteDeclarationInline(admin.TabularInline):
    model = VoteDeclaration
    extra = 0


def initiate_blacklisting(
    modeladmin: "CandidateAdmin", request: HttpRequest, queryset: admin.ModelAdmin
) -> TemplateResponse | None:
    sustained_days = settings.BLACKLIST_SUSTAINED_DAYS
    ratio = Decimal(str(settings.BLACKLIST_RATIO))

    if "apply" in request.POST:
        candidate_ids = request.POST.getlist("candidate_ids")
        forum_url = request.POST.get("forum_discussion_url", "")
        reason = request.POST.get("reason", "")
        now = timezone.now()
        count = 0
        for candidate in Candidate.objects.filter(pk__in=candidate_ids):
            BlacklistHistory.objects.create(
                candidate=candidate,
                blacklisted_at=now,
                rating_at_blacklist=candidate.current_rating,
                blacklisted_by=request.user,
                forum_discussion_url=forum_url,
                reason=reason,
            )
            Candidate.objects.filter(pk=candidate.pk).update(
                is_blacklisted=True,
                blacklisted_at=now,
                is_endorsed=False,
                endorsement_verified_at=None,
            )
            count += 1
        messages.success(request, f"Permanently blacklisted {count} candidate(s).")
        return None

    now = timezone.now()
    eligible = []
    skipped = []

    for candidate in queryset:
        issues = []
        if not candidate.is_endorsed:
            issues.append("Not endorsed")
        if not candidate.election_win_confirmed:
            issues.append("Election win not confirmed")
        if candidate.pre_election_rating_snapshot is None:
            issues.append("No pre-election rating snapshot")
        if not candidate.rating_below_threshold_since:
            issues.append("Rating has not fallen below threshold")
        else:
            days_below = (now - candidate.rating_below_threshold_since).days
            if days_below < sustained_days:
                issues.append(
                    f"Only {days_below} of {sustained_days} required days below threshold"
                )
        if candidate.is_blacklisted:
            issues.append("Already blacklisted")

        if issues:
            skipped.append((candidate, issues))
        else:
            threshold = candidate.pre_election_rating_snapshot * ratio
            days_below = (now - candidate.rating_below_threshold_since).days
            candidate.blacklist_threshold = threshold
            candidate.days_below = days_below
            eligible.append(candidate)

    return TemplateResponse(
        request,
        "admin/polium/candidate/blacklist_confirm.html",
        {
            "eligible": eligible,
            "skipped": skipped,
            "opts": Candidate._meta,
            "sustained_days": sustained_days,
            "title": "Confirm blacklisting",
        },
    )


initiate_blacklisting.short_description = "Initiate permanent blacklisting"  # type: ignore[attr-defined]


@admin.register(Candidate)
class CandidateAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "office",
        "jurisdiction",
        "current_rating",
        "is_endorsed",
        "election_win_confirmed",
        "is_blacklisted",
        "engagement_count",
    )
    list_filter = ("is_blacklisted", "is_endorsed", "election_win_confirmed", "jurisdiction")
    readonly_fields = (
        "sqid",
        "current_rating",
        "is_blacklisted",
        "blacklisted_at",
        "rating_below_threshold_since",
        "endorsement_verified_at",
        "engagement_count",
        "created_at",
    )
    fieldsets = (
        (None, {
            "fields": ("sqid", "name", "office", "jurisdiction", "election", "created_by",
                       "external_reference", "bio", "current_rating", "engagement_count",
                       "created_at"),
        }),
        ("Endorsement", {
            "fields": ("is_endorsed", "endorsement_url", "endorsement_verified_at"),
        }),
        ("Election win", {
            "fields": ("election_win_confirmed", "pre_election_rating_snapshot"),
        }),
        ("Blacklist", {
            "fields": ("is_blacklisted", "blacklisted_at", "rating_below_threshold_since"),
        }),
    )
    actions = [initiate_blacklisting]
    inlines = [OfficeHistoryInline, BlacklistHistoryInline, VoteDeclarationInline]


@admin.register(BlacklistHistory)
class BlacklistHistoryAdmin(admin.ModelAdmin):
    list_display = ("candidate", "blacklisted_at", "rating_at_blacklist", "blacklisted_by")
    readonly_fields = (
        "candidate",
        "blacklisted_at",
        "rating_at_blacklist",
        "blacklisted_by",
        "forum_discussion_url",
        "reason",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: object = None) -> bool:
        return False


@admin.register(Election)
class ElectionAdmin(admin.ModelAdmin):
    list_display = ("name", "jurisdiction", "election_date", "created_at")
    readonly_fields = ("sqid", "created_at")
