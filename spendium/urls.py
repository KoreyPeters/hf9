from django.urls import path

from . import views

app_name = "spendium"

urlpatterns = [
    path("notify/", views.notify, name="notify"),
    path("privacy/", views.privacy, name="privacy"),
    path("purchases/<int:pk>/", views.purchase_detail, name="purchase_detail"),
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
