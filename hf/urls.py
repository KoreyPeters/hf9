from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("accounts.urls")),
    path("polium/", include("polium.urls")),
    path("spendium/", include("spendium.urls")),
    path("tasks/", include("hf.task_urls")),
]
