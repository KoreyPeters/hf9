from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest

from .models import (
    Manufacturer,
    Product,
    ProductAlias,
    ProductCategory,
    ProductUpc,
    SpendiumWaitlist,
    Store,
)


@admin.register(SpendiumWaitlist)
class SpendiumWaitlistAdmin(admin.ModelAdmin):
    list_display = ["email", "created_at"]
    readonly_fields = ["email", "created_at"]


@admin.register(Store)
class StoreAdmin(admin.ModelAdmin):
    list_display = ["name", "status", "created_at"]
    list_filter = ["status"]
    search_fields = ["name"]
    readonly_fields = ["sqid", "created_at"]


@admin.register(Manufacturer)
class ManufacturerAdmin(admin.ModelAdmin):
    list_display = ["name", "status", "created_at"]
    list_filter = ["status"]
    search_fields = ["name"]
    readonly_fields = ["sqid", "created_at"]


@admin.register(ProductCategory)
class ProductCategoryAdmin(admin.ModelAdmin):
    list_display = ["name", "parent", "is_sensitive"]
    list_filter = ["is_sensitive"]
    search_fields = ["name"]


class ProductUpcInline(admin.TabularInline):
    model = ProductUpc
    extra = 0


class ProductAliasInline(admin.TabularInline):
    model = ProductAlias
    extra = 0
    fields = [
        "raw_text",
        "store",
        "status",
        "confirmation_count",
        "contradiction_count",
    ]


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = [
        "canonical_name",
        "manufacturer",
        "category",
        "status",
        "confidence_source",
        "merged_into",
    ]
    list_filter = ["status", "confidence_source", "category"]
    search_fields = ["canonical_name", "aliases__raw_text", "upcs__upc"]
    autocomplete_fields = ["manufacturer", "category", "merged_into"]
    readonly_fields = ["sqid", "created_at", "updated_at"]
    inlines = [ProductUpcInline, ProductAliasInline]
    actions = ["merge_into_oldest"]

    @admin.action(description="Merge selected products into the oldest selected")
    def merge_into_oldest(
        self, request: HttpRequest, queryset: QuerySet[Product]
    ) -> None:
        """Retire the selected products into the oldest one.

        The oldest record wins because it has had the longest to accumulate
        aliases and ratings. Aliases and UPCs move across so the losing records'
        receipt strings keep resolving; ratings are not rewritten, they follow
        via `resolve_canonical()`, which keeps the merge reversible.
        """
        selected = list(queryset.order_by("created_at"))
        if len(selected) < 2:
            self.message_user(
                request,
                "Select at least two products to merge.",
                level=messages.WARNING,
            )
            return

        target = selected[0].resolve_canonical()
        losers = selected[1:]

        merged = 0
        for loser in losers:
            if loser.pk == target.pk:
                continue
            # Guard against cycles: merging one of the target's own ancestors
            # into it would make resolve_canonical() loop.
            if target.resolve_canonical().pk == loser.pk:
                self.message_user(
                    request,
                    f"Skipped {loser.canonical_name} — merging it would create a cycle.",
                    level=messages.WARNING,
                )
                continue

            # No collision check is needed. (store, raw_text_normalised) is
            # unique across the whole alias table, not per product, so a string
            # already resolves to exactly one product per retailer — the target
            # cannot already claim a string the loser holds.
            loser.aliases.update(product=target)
            loser.upcs.update(product=target)
            loser.merged_into = target
            loser.status = Product.STATUS_RETIRED
            loser.save()
            merged += 1

        self.message_user(
            request,
            f"Merged {merged} product(s) into '{target.canonical_name}'.",
            level=messages.SUCCESS,
        )


@admin.register(ProductUpc)
class ProductUpcAdmin(admin.ModelAdmin):
    list_display = ["upc", "product", "created_at"]
    search_fields = ["upc", "product__canonical_name"]
    autocomplete_fields = ["product"]


@admin.register(ProductAlias)
class ProductAliasAdmin(admin.ModelAdmin):
    list_display = [
        "raw_text",
        "store",
        "product",
        "status",
        "confirmation_count",
        "contradiction_count",
        "source",
    ]
    list_filter = ["status", "source", "store"]
    search_fields = ["raw_text", "raw_text_normalised", "product__canonical_name"]
    autocomplete_fields = ["product", "store"]
    readonly_fields = ["raw_text_normalised", "created_at", "updated_at"]
