from django.urls import path

from . import views

app_name = "polium"

urlpatterns = [
    path("candidates/<str:sqid>/", views.candidate_detail, name="candidate_detail"),
    path("elections/<str:sqid>/", views.election_detail, name="election_detail"),
    path("jurisdictions/<str:sqid>/", views.jurisdiction_detail, name="jurisdiction_detail"),
    path("candidates/<str:sqid>/survey/", views.submit_survey, name="submit_survey"),
    path("candidates/<str:sqid>/declare/", views.declare_vote, name="declare_vote"),
]
