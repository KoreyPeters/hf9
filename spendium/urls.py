from django.urls import path

from . import views

app_name = "spendium"

urlpatterns = [
    path("notify/", views.notify, name="notify"),
]
