from django.contrib import admin

from .models import Category, Criterion, CriterionAnswer, SurveyConfig, SurveyResponse


class CriterionInline(admin.TabularInline):
    model = Criterion
    extra = 0


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "game")
    list_filter = ("game",)
    inlines = [CriterionInline]


@admin.register(Criterion)
class CriterionAdmin(admin.ModelAdmin):
    list_display = ("question", "category", "weight", "is_active")
    list_filter = ("is_active", "category__game")


class CriterionAnswerInline(admin.TabularInline):
    model = CriterionAnswer
    extra = 0


@admin.register(SurveyResponse)
class SurveyResponseAdmin(admin.ModelAdmin):
    list_display = ("player", "content_type", "object_id", "created_at", "submitted_at")
    inlines = [CriterionAnswerInline]


@admin.register(SurveyConfig)
class SurveyConfigAdmin(admin.ModelAdmin):
    list_display = [
        "cooldown_days",
        "survey_points_first",
        "survey_points_second",
        "survey_points_subsequent",
    ]

    def has_add_permission(self, request) -> bool:
        return not SurveyConfig.objects.exists()

    def has_delete_permission(self, request, obj=None) -> bool:
        return False
