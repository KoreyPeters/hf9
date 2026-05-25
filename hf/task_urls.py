import lifecycle.task_views as lifecycle_task_views
from django.urls import path

urlpatterns = [
    path("check-deprecations/", lifecycle_task_views.check_deprecations, name="task_check_deprecations"),
    path("check-deletions/", lifecycle_task_views.check_deletions, name="task_check_deletions"),
]
