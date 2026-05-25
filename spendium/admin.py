from django.contrib import admin

from .models import SpendiumWaitlist


@admin.register(SpendiumWaitlist)
class SpendiumWaitlistAdmin(admin.ModelAdmin):
    list_display = ["email", "created_at"]
    readonly_fields = ["email", "created_at"]
