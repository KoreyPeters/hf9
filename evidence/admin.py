from django.contrib import admin

from .models import Evidence, EvidenceFlag, EvidenceUsefulness


class EvidenceUsefulnessInline(admin.TabularInline):
    model = EvidenceUsefulness
    extra = 0


class EvidenceFlagInline(admin.TabularInline):
    model = EvidenceFlag
    extra = 0


@admin.register(Evidence)
class EvidenceAdmin(admin.ModelAdmin):
    list_display = ("url", "submitted_by", "content_type", "object_id", "status", "net_usefulness_score", "submitted_at")
    list_filter = ("status",)
    readonly_fields = ("net_usefulness_score", "submitted_at")
    inlines = [EvidenceUsefulnessInline, EvidenceFlagInline]
