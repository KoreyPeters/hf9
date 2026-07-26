from django.urls import path

from . import views

app_name = "spendium"

urlpatterns = [
    path("notify/", views.notify, name="notify"),
    path("privacy/", views.privacy, name="privacy"),
    path("products/<str:sqid>/", views.product_detail, name="product_detail"),
    path(
        "products/<str:sqid>/rate/",
        views.submit_product_survey,
        name="submit_product_survey",
    ),
    path("receipts/", views.purchase_list, name="purchase_list"),
    path("receipts/upload/", views.receipt_upload, name="receipt_upload"),
    path(
        "receipts/export/",
        views.purchase_history_export,
        name="purchase_history_export",
    ),
    path(
        "receipts/delete-all/",
        views.purchase_history_delete,
        name="purchase_history_delete",
    ),
    path("purchases/<int:pk>/", views.purchase_detail, name="purchase_detail"),
    path("purchases/<int:pk>/delete/", views.purchase_delete, name="purchase_delete"),
    path(
        "purchases/<int:pk>/prompts/",
        views.disambiguation_section,
        name="disambiguation_section",
    ),
    path("lines/<int:pk>/confirm/", views.confirm_line, name="confirm_line"),
    path(
        "lines/<int:pk>/choose/", views.choose_line_product, name="choose_line_product"
    ),
    path(
        "lines/<int:pk>/describe/",
        views.submit_line_free_text,
        name="submit_line_free_text",
    ),
]
