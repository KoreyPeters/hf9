from django.urls import path

from . import views

app_name = "polium"

urlpatterns = [
    path("", views.polium_home, name="home"),
    path("jurisdictions/search/", views.jurisdiction_search, name="jurisdiction_search"),
    path("jurisdictions/search-parent/", views.jurisdiction_search_parent, name="jurisdiction_search_parent"),
    path("jurisdictions/create-form/", views.jurisdiction_create_form, name="jurisdiction_create_form"),
    path("jurisdictions/create/", views.create_jurisdiction, name="create_jurisdiction"),
    path("jurisdictions/follow/", views.follow_jurisdiction, name="follow_jurisdiction"),
    path("jurisdictions/unfollow/", views.unfollow_jurisdiction, name="unfollow_jurisdiction"),
    path("candidates/<str:sqid>/", views.candidate_detail, name="candidate_detail"),
    path("elections/<str:sqid>/", views.election_detail, name="election_detail"),
    path("jurisdictions/<str:sqid>/", views.jurisdiction_detail, name="jurisdiction_detail"),
    path("candidates/<str:sqid>/survey/", views.submit_survey, name="submit_survey"),
    path("candidates/<str:sqid>/declare/", views.declare_vote, name="declare_vote"),
    path("candidates/<str:sqid>/evidence/submit/", views.evidence_submit, name="evidence_submit"),
    path("evidence/<int:pk>/vote/", views.evidence_vote, name="evidence_vote"),
    path("evidence/<int:pk>/flag/", views.evidence_flag, name="evidence_flag"),
]
