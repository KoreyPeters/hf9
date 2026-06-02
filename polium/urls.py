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
    path("jurisdictions/flag-search/", views.jurisdiction_search_flag, name="jurisdiction_search_flag"),
    path("jurisdictions/<str:sqid>/follow/", views.jurisdiction_follow_detail, name="jurisdiction_follow_detail"),
    path("jurisdictions/<str:sqid>/unfollow/", views.jurisdiction_unfollow_detail, name="jurisdiction_unfollow_detail"),
    path("jurisdictions/<str:sqid>/elections-section/", views.elections_section, name="elections_section"),
    path("jurisdictions/<str:sqid>/add-election-form/", views.add_election_form, name="add_election_form"),
    path("jurisdictions/<str:sqid>/add-election/", views.add_election, name="add_election"),
    path("jurisdictions/<str:sqid>/candidates-section/", views.candidates_section, name="candidates_section"),
    path("jurisdictions/<str:sqid>/add-candidate-form/", views.add_candidate_form, name="add_candidate_form"),
    path("jurisdictions/<str:sqid>/add-candidate/", views.add_candidate, name="add_candidate"),
    path("jurisdictions/<str:sqid>/flag-duplicate/", views.flag_jurisdiction_duplicate, name="flag_jurisdiction_duplicate"),
    path("candidates/<str:sqid>/", views.candidate_detail, name="candidate_detail"),
    path("elections/<str:sqid>/", views.election_detail, name="election_detail"),
    path("jurisdictions/<str:sqid>/", views.jurisdiction_detail, name="jurisdiction_detail"),
    path("candidates/<str:sqid>/survey/", views.submit_survey, name="submit_survey"),
    path("candidates/<str:sqid>/declare/", views.declare_vote, name="declare_vote"),
    path("candidates/<str:sqid>/evidence/submit/", views.evidence_submit, name="evidence_submit"),
    path("evidence/<int:pk>/vote/", views.evidence_vote, name="evidence_vote"),
    path("evidence/<int:pk>/flag/", views.evidence_flag, name="evidence_flag"),
]
