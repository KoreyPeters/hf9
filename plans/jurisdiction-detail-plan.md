# Jurisdiction Detail Plan

## What we're building

A public-facing jurisdiction detail page at `/polium/jurisdictions/<sqid>/` that shows everything relevant to a jurisdiction: its place in the hierarchy, upcoming and past elections, candidates, and follower count. Authenticated players get contextual action buttons: follow/unfollow, flag as duplicate, add election, add candidate.

The `jurisdiction_detail` view currently returns `HttpResponse("TODO")`. This replaces it.

All interactive sections use the Datastar click-to-edit pattern — the server drives state transitions by returning replacement HTML fragments. No full-page reloads for any player action.

---

## Page layout (public, no login required)

### Header

- **Jurisdiction name** (h1)
- **Level badge** — human-readable: Country / State or Province / County or Region / City / Other. Derived from the stored level value via a dict passed in context.
- **Parent breadcrumb chain** — each ancestor linked to its own detail page, walking the `parent` FK to root. Example: `New Zealand › Wellington Region`
- **Deprecated notice** — if `status == STATUS_DEPRECATED`, a prominent yellow banner: "This jurisdiction has been flagged as a possible duplicate and is under community review." The page remains accessible; all write actions are hidden except unfollow.

### Stats bar

- Follower count (`active_engagement`)

### Sections (ordered)

1. **Children** — active (`STATUS_ACTIVE`) child jurisdictions, ordered by name, as pill links to their own detail pages. Hidden if none.
2. **Elections section** (`#elections-section`) — upcoming elections (≥ today, asc, up to 10) + past elections (< today, desc, up to 5) + add-election affordance. The entire section is server-managed via Datastar.
3. **Candidates section** (`#candidates-section`) — all candidates with `jurisdiction = this`, ordered by `current_rating` descending. Blacklisted candidates shown at bottom with a visual notice. Each links to candidate profile with rating. Add-candidate affordance at bottom.
4. **Follow section** (`#follow-section`) — follow/unfollow control.
5. **Flag section** (`#flag-section`) — flag as duplicate form (authenticated + mature + active).

---

## Datastar interaction model

All interactive sections use the click-to-edit pattern: a `<div id="...">` acts as the target. The server returns replacement HTML for that div via SSE `patch_elements`. State transitions happen without page reloads.

**Follow/unfollow**: `@post` buttons. Server creates/deletes follow and returns the `#follow-section` in its new state.

**Add election / Add candidate**: Three-state click-to-edit.
1. View mode — just an "Add election" / "Add candidate" button.
2. Form mode — revealed by `@get` on the button. Server returns the section with the inline form.
3. On save — `@post` submits the form. Server creates the record and returns the section in view mode with the new entry in the list.
4. On cancel — `@get` on the Cancel button. Server returns the section in view mode with no changes.

**Flag as duplicate**: The search is already Datastar live-search. The final submit uses a Datastar `@post` button (no `<form>` element). Server creates the flag and returns `#flag-section` in the "already flagged" state.

---

## Authenticated player actions

All actions hidden for anonymous visitors. Write actions (add election, add candidate, flag) also hidden when `status == STATUS_DEPRECATED`. Unfollow remains available for deprecated jurisdictions.

### 1. Follow / Unfollow

Two new SSE views at `jurisdictions/<str:sqid>/follow/` and `jurisdictions/<str:sqid>/unfollow/`. These are distinct from the existing flat-path `jurisdictions/follow/` and `jurisdictions/unfollow/` (used by the home page search) which remain unchanged.

**`#follow-section`** starts in the player's current follow state. All transitions happen in-place:

- **Not following**: depth selector + "Follow" button → `@post('{% url 'polium:jurisdiction_follow_detail' jurisdiction.sqid %}')` with `$follow_depth` signal. Server creates follow, returns `#follow-section` in following state.
- **Following**: current depth label + "Unfollow" button → `@post('{% url 'polium:jurisdiction_unfollow_detail' jurisdiction.sqid %}')`. Server deletes follow, returns `#follow-section` in not-following state.

The `#follow-section` carries `data-store='{"follow_depth": "all"}'` for the depth signal.

### 2. Flag as duplicate — protected action

Maturity required: 7 days + 3 survey responses. Uses `account_is_mature(request.user)` from `core.maturity`.

**`#flag-section`** states:

- **Not authenticated** or **deprecated jurisdiction**: section hidden entirely.
- **Not mature**: subdued note — "Flagging requires an account at least 7 days old with 3 surveys submitted."
- **Already flagged**: "You have flagged this jurisdiction as a duplicate of [target name]." (rendered on page load from `flagged_target` context; also the state returned after a successful flag submission)
- **Mature, not yet flagged**: flag search form.

**Flag form (mature, not flagged):**

```html
<div id="flag-section" data-store='{"flag_q": "", "flag_target_sqid": "", "flag_target_name": ""}'>
  <input type="text"
         data-bind:flag_q
         data-on:input__debounce.300ms="@get('{% url 'polium:jurisdiction_search_flag' %}?exclude={{ jurisdiction.sqid }}')"
         placeholder="Search for the correct jurisdiction…">
  <div id="flag-results"></div>
  <button data-on:click="@post('{% url 'polium:flag_jurisdiction_duplicate' jurisdiction.sqid %}')"
          data-show="$flag_target_sqid !== ''">
    Flag as duplicate of <span data-text="$flag_target_name"></span>
  </button>
</div>
```

`@post` sends all current signals (including `$flag_target_sqid`) as JSON. No `<form>` element needed; CSRF is handled by the `X-CSRFToken` header that Datastar sends automatically from the CSRF cookie.

### 3. Add election (click-to-edit)

**`#elections-section`** wraps the elections list (upcoming + past) and the add-election affordance. The server always returns the full section.

Three endpoints:
- `@get('/polium/jurisdictions/<sqid>/elections-section/')` — view mode (used on Cancel). Returns elections list + "Add election" button.
- `@get('/polium/jurisdictions/<sqid>/add-election-form/')` — form mode. Returns elections list + inline form.
- `@post('/polium/jurisdictions/<sqid>/add-election/')` — creates election. Returns elections list (now including new entry) + "Add election" button.

Form fields (bound to signals in the section's `data-store`):
- `$election_name` → `data-bind:election_name`
- `$election_date` → `data-bind:election_date`
- `$election_external_reference` → `data-bind:election_external_reference`

Save button: `data-on:click="@post('{% url 'polium:add_election' jurisdiction.sqid %}')"` with `data-attr:disabled` guard if name or date is empty.

Cancel button: `data-on:click="@get('{% url 'polium:elections_section' jurisdiction.sqid %}')"`.

On validation failure: the `add_election` POST view returns the form mode with an inline error message (no redirect).

### 4. Add candidate (click-to-edit)

Same pattern as add election. **`#candidates-section`** wraps candidates list + add-candidate affordance.

Three endpoints:
- `@get('/polium/jurisdictions/<sqid>/candidates-section/')` — view mode (Cancel target).
- `@get('/polium/jurisdictions/<sqid>/add-candidate-form/')` — form mode.
- `@post('/polium/jurisdictions/<sqid>/add-candidate/')` — creates candidate. Returns updated list + button.

Form signals: `$candidate_name`, `$candidate_office`, `$candidate_election_id`, `$candidate_external_reference`, `$candidate_bio`.

The election `<select>` uses plain HTML `<option>` tags (not a live search) since it's bound to a simple integer signal. Election options come from `elections_for_form` passed to the view. Guard: `election_id` must belong to this jurisdiction (validated server-side).

---

## Backend changes

### Existing views — unchanged

`follow_jurisdiction` and `unfollow_jurisdiction` at flat paths remain as-is for the home page. The `next` parameter approach is dropped — the detail page uses SSE instead.

### New views (`polium/views.py`)

**`jurisdiction_detail`** — replaces TODO stub
- GET, public
- `get_object_or_404(Jurisdiction, sqid=sqid)`
- Builds breadcrumb by walking `parent` FK to root
- Queries: active children (by name), upcoming elections (≥ today, asc, [:10]), past elections (< today, desc, [:5]), candidates (by `-current_rating`), elections for form (by `-election_date`)
- For authenticated users: `is_following`, `follow_depth`, `is_mature`, `already_flagged`, `flagged_target`
- Passes `LEVEL_LABELS` dict and `is_active` flag

```python
LEVEL_LABELS = {
    "country": "Country",
    "province": "State / Province / Territory",
    "region": "County / District / Region",
    "city": "City / Municipality",
    "other": "Other",
}
```

**`jurisdiction_follow_detail`** — POST SSE, `@login_required`
- Reads `follow_depth` from signals (default `DEPTH_ALL`)
- `get_or_create` JurisdictionFollow; increments `active_engagement` if created
- Returns `patch_elements` with `follow_section.html` partial (following state)

**`jurisdiction_unfollow_detail`** — POST SSE, `@login_required`
- Deletes follow if exists; decrements `active_engagement` floor 0
- Returns `patch_elements` with `follow_section.html` partial (not-following state)

**`elections_section`** — GET SSE, public
- Returns `patch_elements` with `elections_section.html` partial in view mode (no form)

**`add_election_form`** — GET SSE, `@login_required`
- Returns `patch_elements` with `elections_section.html` partial in form mode

**`add_election`** — POST SSE, `@login_required`
- Reads `election_name`, `election_date`, `election_external_reference` from signals
- Validates name (non-empty) and date (parseable ISO date)
- On failure: returns `elections_section.html` in form mode with `error` context
- On success: creates `Election`, returns `elections_section.html` in view mode with refreshed queries

**`candidates_section`** — GET SSE, public
- Returns `patch_elements` with `candidates_section.html` partial in view mode

**`add_candidate_form`** — GET SSE, `@login_required`
- Returns `patch_elements` with `candidates_section.html` partial in form mode

**`add_candidate`** — POST SSE, `@login_required`
- Reads `candidate_name`, `candidate_office`, `candidate_election_id`, `candidate_external_reference`, `candidate_bio` from signals
- Validates name and office (non-empty); election FK must belong to this jurisdiction if provided
- On failure: returns `candidates_section.html` in form mode with `error` context
- On success: creates `Candidate`, returns `candidates_section.html` in view mode with refreshed query

**`flag_jurisdiction_duplicate`** — POST SSE, `@login_required`
- Maturity check; returns `flag_section.html` with error if not mature
- Reads `flag_target_sqid` from signals; looks up target jurisdiction
- Guards: target ≠ current jurisdiction; no duplicate flag (catches `IntegrityError`)
- On success: creates `JurisdictionDuplicateFlag`; returns `flag_section.html` in "already flagged" state
- On error: returns `flag_section.html` in form mode with inline error message

**`jurisdiction_search_flag`** — GET SSE, public
- Reads `flag_q` from signals; `exclude` sqid from GET param
- Returns `patch_elements` with `flag_results.html` partial (no "Add it" affordance)

### URL additions (`polium/urls.py`)

`flag-search` must appear before the `<str:sqid>` catch-all. All `<str:sqid>/suffix/` patterns are unambiguous alongside `<str:sqid>/`:

```python
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
```

---

## Templates

**`templates/polium/jurisdiction_detail.html`** — new full-page template

Sections in order:
1. Breadcrumb → name (h1) → level badge → stats bar (follower count)
2. Deprecated notice (conditional on `not is_active`)
3. Django messages block
4. `#follow-section` — rendered inline from `follow_section.html` partial
5. Children list (static; hidden if empty)
6. `#elections-section` — rendered inline from `elections_section.html` partial (view mode)
7. `#candidates-section` — rendered inline from `candidates_section.html` partial (view mode)
8. `#flag-section` — rendered inline from `flag_section.html` partial

**New partials under `templates/polium/partials/`:**

- **`follow_section.html`** — follow/unfollow toggle. Context: `jurisdiction`, `is_following`, `follow_depth`, `is_active`. Wraps in `<div id="follow-section" data-store='{"follow_depth": "all"}'>`.

- **`elections_section.html`** — elections list + add-election affordance. Context: `jurisdiction`, `upcoming_elections`, `past_elections`, `elections_for_form`, `is_active`, `show_form` (bool), `error` (optional string). Wraps in `<div id="elections-section" data-store='{"election_name": "", "election_date": "", "election_external_reference": ""}'>`.

- **`candidates_section.html`** — candidates list + add-candidate affordance. Context: `jurisdiction`, `candidates`, `elections_for_form`, `is_active`, `show_form` (bool), `error` (optional). Wraps in `<div id="candidates-section" data-store='{"candidate_name": "", ...}'>`.

- **`flag_section.html`** — flag form in all states. Context: `jurisdiction`, `already_flagged`, `flagged_target`, `is_mature`, `is_active`, `error` (optional). Wraps in `<div id="flag-section" data-store='{"flag_q": "", "flag_target_sqid": "", "flag_target_name": ""}'>`.

- **`flag_results.html`** — live search results for flag picker. Same visual pattern as `parent_results.html`. Each button sets `$flag_target_sqid` and `$flag_target_name` signals and clears `#flag-results`.

---

## What this deliberately does not include

- **Deprecation trigger on flag submit** — the hourly scheduler handles the ratio check; no synchronous trigger.
- **Election detail page** — `election_detail` stays as `TODO`; out of scope.
- **Candidate editing** — creation only; immutable after creation per design.
- **Duplicate flagging UI for elections or candidates** — out of scope.
- **`active_engagement` sync** — already handled in existing views via `Greatest(F(...) - 1, 0)`.
- **Modifications to existing `follow_jurisdiction` / `unfollow_jurisdiction`** — those views are unchanged; detail page uses new sqid-scoped endpoints.

---

## Implementation todo

- [x] `jurisdiction_detail` — replace TODO; breadcrumb, queries, auth context, render
- [x] `jurisdiction_follow_detail` — POST SSE; get_or_create follow + engagement increment; return `follow_section.html`
- [x] `jurisdiction_unfollow_detail` — POST SSE; delete follow + engagement decrement; return `follow_section.html`
- [x] `elections_section` — GET SSE; return `elections_section.html` in view mode
- [x] `add_election_form` — GET SSE; return `elections_section.html` in form mode
- [x] `add_election` — POST SSE; validate signals; create Election; return `elections_section.html` view mode (or form mode with error)
- [x] `candidates_section` — GET SSE; return `candidates_section.html` in view mode
- [x] `add_candidate_form` — GET SSE; return `candidates_section.html` in form mode
- [x] `add_candidate` — POST SSE; validate signals; guard election FK to this jurisdiction; create Candidate; return `candidates_section.html` view mode (or form mode with error)
- [x] `flag_jurisdiction_duplicate` — POST SSE; maturity check; unique guard (savepoint); create flag; return `flag_section.html`
- [x] `jurisdiction_search_flag` — GET SSE; `$flag_q` from signals; `exclude` from GET; return `flag_results.html`
- [x] `urls.py` — add 10 new paths (flag-search before sqid patterns)
- [x] `templates/polium/jurisdiction_detail.html` — full page template
- [x] `templates/polium/partials/follow_section.html`
- [x] `templates/polium/partials/elections_section.html` (view + form modes via `show_form` flag)
- [x] `templates/polium/partials/candidates_section.html` (view + form modes via `show_form` flag)
- [x] `templates/polium/partials/flag_section.html` (all states)
- [x] `templates/polium/partials/flag_results.html`
- [x] Tests:
  - `jurisdiction_detail` returns 200 for active jurisdiction
  - `jurisdiction_detail` returns 200 for deprecated jurisdiction with deprecated banner
  - `jurisdiction_detail` 404s for unknown sqid
  - `jurisdiction_follow_detail` creates follow, increments active_engagement, returns SSE
  - `jurisdiction_unfollow_detail` deletes follow, decrements active_engagement, returns SSE
  - `add_election` creates election and returns updated section
  - `add_election` returns form mode with error on missing name or date
  - `add_candidate` creates candidate and returns updated section
  - `add_candidate` rejects election FK belonging to a different jurisdiction
  - `flag_jurisdiction_duplicate` requires maturity
  - `flag_jurisdiction_duplicate` prevents self-flagging
  - `flag_jurisdiction_duplicate` blocks second flag
  - `flag_jurisdiction_duplicate` creates flag and returns "already flagged" state
  - `jurisdiction_search_flag` excludes current jurisdiction sqid from results
