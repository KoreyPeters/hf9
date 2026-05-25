from django.contrib import admin
from django.urls import include, path

from hf import views as hf_views

urlpatterns = [
    path("manifest.json", hf_views.manifest, name="manifest"),
    path("admin/", admin.site.urls),
    path("accounts/", include("accounts.urls")),
    path("polium/", include("polium.urls")),
    path("spendium/", include("spendium.urls")),
    path("tasks/", include("hf.task_urls")),
]
