# Phase 10 Todo — URL Routing

Phase 10 locks in the URL structure for all apps. `hf/urls.py` and `hf/task_urls.py` are already complete from prior phases — no changes needed there. The remaining work is replacing the three empty app URL stubs with proper `app_name` namespaces and patterns, and adding the Polium view stubs that the URL patterns reference.

---

#### Already complete — no changes required
- `hf/urls.py` — manifest, admin, all app includes, tasks include
- `hf/task_urls.py` — all three task routes registered

---

#### 10.1 — Polium view stubs (polium/views.py)

The URL patterns in §10.2 reference five view functions. Full Datastar SSE implementations come later; Phase 10 only needs the stubs to be importable with correct signatures so the URL conf is complete and `manage.py check` passes.

- [x] Replace the placeholder in `polium/views.py`
- [x] Add imports:
  - `from django.http import HttpRequest, HttpResponse`
  - `from django.shortcuts import get_object_or_404`
  - `from .models import Candidate, Election, Jurisdiction`
- [x] Write helper `get_candidate_by_sqid(sqid: str) -> Candidate`:
  - `return get_object_or_404(Candidate, sqid=sqid)`
- [x] Write `candidate_detail(request: HttpRequest, sqid: str) -> HttpResponse` — stub returning `HttpResponse("TODO")`
- [x] Write `election_detail(request: HttpRequest, sqid: str) -> HttpResponse` — stub returning `HttpResponse("TODO")`
- [x] Write `jurisdiction_detail(request: HttpRequest, sqid: str) -> HttpResponse` — stub returning `HttpResponse("TODO")`
- [x] Write `submit_survey(request: HttpRequest, sqid: str) -> HttpResponse` — stub returning `HttpResponse("TODO")`
- [x] Write `declare_vote(request: HttpRequest, sqid: str) -> HttpResponse` — stub returning `HttpResponse("TODO")`

---

#### 10.2 — Polium URL patterns (polium/urls.py)

- [x] Replace the empty stub in `polium/urls.py`
- [x] Set `app_name = "polium"`
- [x] Add import: `from django.urls import path`; `from . import views`
- [x] Define `urlpatterns` with these five routes (all using `<str:sqid>` — SQIDs are strings, never PKs):
  - `path("candidates/<str:sqid>/", views.candidate_detail, name="candidate_detail")`
  - `path("elections/<str:sqid>/", views.election_detail, name="election_detail")`
  - `path("jurisdictions/<str:sqid>/", views.jurisdiction_detail, name="jurisdiction_detail")`
  - `path("candidates/<str:sqid>/survey/", views.submit_survey, name="submit_survey")`
  - `path("candidates/<str:sqid>/declare/", views.declare_vote, name="declare_vote")`

---

#### 10.3 — Accounts URL patterns (accounts/urls.py)

- [x] Replace the empty stub in `accounts/urls.py`
- [x] Add imports:
  - `from django.contrib.auth import views as auth_views`
  - `from django.urls import path`
- [x] Set `app_name = "accounts"`
- [x] Define `urlpatterns` with login and logout:
  - `path("login/", auth_views.LoginView.as_view(), name="login")`
  - `path("logout/", auth_views.LogoutView.as_view(), name="logout")`
  - Explicitly defining these two (rather than `include("django.contrib.auth.urls")`) avoids double-namespace nesting (`auth` inside `accounts`) and keeps reversals predictable: `{% url "accounts:login" %}`, `{% url "accounts:logout" %}`
  - Password reset and password change views are deferred — they require email template work and are not needed to complete the URL routing structure

---

#### 10.4 — Spendium URL patterns (spendium/urls.py)

Spendium's game models and views are not yet implemented. The URL file only needs a proper namespace and empty patterns so the `include("spendium.urls")` in `hf/urls.py` resolves cleanly.

- [x] Replace the empty stub in `spendium/urls.py`
- [x] Add import: `from django.urls import path`
- [x] Set `app_name = "spendium"`
- [x] Set `urlpatterns: list = []`

---

#### 10.5 — System check

- [x] Run `uv run python manage.py check` — must be clean

---

#### Phase 10 complete when
- [x] `polium/views.py` defines `get_candidate_by_sqid` and the five stub view functions, all with correct `HttpRequest`/`HttpResponse` type signatures
- [x] `polium/urls.py` has `app_name = "polium"` and all five SQID-based routes
- [x] `accounts/urls.py` has `app_name = "accounts"` with `login/` and `logout/` routes
- [x] `spendium/urls.py` has `app_name = "spendium"` with empty urlpatterns
- [x] `hf/urls.py` and `hf/task_urls.py` remain unchanged
- [x] `uv run python manage.py check` → `System check identified no issues`
