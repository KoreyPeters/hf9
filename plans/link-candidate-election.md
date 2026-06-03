# Plan: Link Existing Candidate to an Existing Election

## Problem

`Candidate.election` is a nullable FK set only at creation time. If an election is added to a
jurisdiction after a candidate was already created, there is no way to link them. The same gap
exists if both were created independently and the user simply forgot to connect them at creation.

## Goal

Allow any authenticated player to link (or change) the election attached to an existing candidate,
from the candidate's own profile page — the same place they submit evidence, view ratings, and
declare votes.

The action must:
- Be available on the candidate profile page only (not the jurisdiction page, which already has
  enough controls).
- Be restricted to elections that belong to the same jurisdiction as the candidate.
- Allow clearing the election link as well as setting one (setting back to "none").
- Require login; no maturity gate (same bar as creating a candidate or election).

## Design Decisions

### Where: candidate profile page

The candidate's profile is the canonical place to act on a specific candidate. Adding the control
there keeps the jurisdiction page clean and makes the action discoverable by anyone who navigates
to a candidate they want to curate.

### What the UI looks like

Currently the profile shows the candidate's linked election (if any) as plain text. The new UI
replaces that with a small inline widget:

```
Election: 2026 Federal Election (2026-10-15)  [Change]
```

or, if none:

```
Election: None  [Link to election]
```

Clicking the button swaps the static text for a `<select>` populated with all elections in the
candidate's jurisdiction, plus a "— none —" option. A "Save" button posts the change; a "Cancel"
button reverts without saving. On save, the section re-renders with the updated value.

This follows exactly the same Datastar SSE + partial-render pattern used for elections and
candidates on the jurisdiction detail page.

### No data-model changes

`Candidate.election` is already a nullable FK. No migration needed.

## Endpoints

Two new endpoints, both on the candidate SQID:

| Name | Method | URL | Purpose |
|---|---|---|---|
| `candidate_link_election_form` | GET | `/candidates/<sqid>/link-election-form/` | Returns the section with the `<select>` visible |
| `candidate_link_election` | POST | `/candidates/<sqid>/link-election/` | Validates and saves, returns updated section |

Both return a Datastar SSE patch targeting `#candidate-election-section` in the candidate profile
template.

## Validation

- The selected election (if not blank) must exist and must belong to `candidate.jurisdiction`.
- If the election belongs to a different jurisdiction: error.
- If the election pk is non-numeric or not found: error.
- No date-based restriction (the design doc does not specify one; an admin might legitimately
  link a candidate to a past or future election).

## Template changes

`templates/polium/candidate_profile.html`:
- Wrap the existing election display in `<div id="candidate-election-section">`.
- Show the current election name (or "None") plus a "Change" / "Link to election" button that
  triggers `@get('...link-election-form/')`.

New partial `templates/polium/partials/candidate_election_section.html`:
- Renders both states: static display (default) and edit form (when `show_form=True`).
- The `<select>` lists all elections in `candidate.jurisdiction`, ordered by `-election_date`.
- On save, POSTs to `link-election/`.
- Cancel calls `@get('...link-election-form-cancel/')` — or more simply, re-renders the section
  in static mode via a GET to a read-only section endpoint (same pattern as `candidates_section`
  vs `add_candidate_form`).

## View changes (`polium/views.py`)

Two new view functions:

```python
@login_required
def candidate_link_election_form(request, sqid):
    # renders partial with show_form=True

@login_required
@require_POST
def candidate_link_election(request, sqid):
    # reads candidate_election_id signal
    # validates election belongs to candidate.jurisdiction
    # saves candidate.election
    # renders partial with show_form=False
```

Helper `_candidate_election_ctx(candidate, show_form, error)` to avoid duplication.

## URL changes (`polium/urls.py`)

```python
path("candidates/<str:sqid>/link-election-form/", views.candidate_link_election_form, name="candidate_link_election_form"),
path("candidates/<str:sqid>/link-election/", views.candidate_link_election, name="candidate_link_election"),
```

Also add a read-only section endpoint for Cancel to target:
```python
path("candidates/<str:sqid>/election-section/", views.candidate_election_section, name="candidate_election_section"),
```

## Implementation order

1. Add URL entries.
2. Add view functions and helper.
3. Create `candidate_election_section.html` partial.
4. Update `candidate_profile.html` to wrap the election display in the targetable div and wire the
   initial button.
5. Manual test: link a candidate with no election, change an existing link, clear a link.

---

## Todo List

### Phase 1 — URLs

- [x] Add `candidate_election_section` path to `polium/urls.py`
  (`GET /candidates/<sqid>/election-section/`)
- [x] Add `candidate_link_election_form` path to `polium/urls.py`
  (`GET /candidates/<sqid>/link-election-form/`)
- [x] Add `candidate_link_election` path to `polium/urls.py`
  (`POST /candidates/<sqid>/link-election/`)

### Phase 2 — Views

- [x] Add helper `_candidate_election_ctx(candidate, show_form, error="")` returning a dict with
  `candidate`, `elections_for_form` (all elections in the candidate's jurisdiction ordered by
  `-election_date`), `show_form`, and `error`
- [x] Add `candidate_election_section` view — GET, no login required, renders partial in static
  (read-only) mode; used by Cancel to reset the widget
- [x] Add `candidate_link_election_form` view — GET, `@login_required`, renders partial with
  `show_form=True`
- [x] Add `candidate_link_election` view — POST, `@login_required`, `@require_POST`:
  - [x] Read `candidate_election_id` from Datastar signals
  - [x] If blank: set `candidate.election = None`, save, render static partial
  - [x] If non-blank: parse as int; 404/error if non-numeric
  - [x] Look up `Election` by pk, filtering to `candidate.jurisdiction`; error if not found
  - [x] Save `candidate.election = election`
  - [x] Render static partial (no error path) or form partial (error path)
  - [x] On success, also patch the Datastar signal `candidate_election_id` back to `""`

### Phase 3 — Partial template

- [x] Create `templates/polium/partials/candidate_election_section.html`
- [x] Wrap everything in `<div id="candidate-election-section"
  data-signals='{"candidate_election_id": ""}'>`
- [x] **Static state** (`show_form=False`):
  - [x] Show current election name + date, or "None" if unlinked
  - [x] If user is authenticated: show "Change" button (if election set) or "Link to election"
    button (if none), both triggering
    `@get('{% url "polium:candidate_link_election_form" candidate.sqid %}')`
- [x] **Edit state** (`show_form=True`):
  - [x] Show error message if `error` is set
  - [x] Render `<select data-bind:candidate_election_id>` with a "— none —" option followed by
    all elections in `elections_for_form` (`{{ election.name }} ({{ election.election_date }})`)
  - [x] Pre-select the candidate's current election if one is set
  - [x] "Save" button: `@post('{% url "polium:candidate_link_election" candidate.sqid %}')`
    with `data-indicator` and `data-attr:disabled` while saving
  - [x] "Cancel" button: `@get('{% url "polium:candidate_election_section" candidate.sqid %}')`

### Phase 4 — Candidate profile template

- [x] Open `templates/polium/candidate_profile.html`
- [x] Locate the existing election display (the line/block showing `candidate.election`)
- [x] Wrap it in `<div id="candidate-election-section">` … `</div>`
- [x] Replace the static election display with an include/inline render of the
  `candidate_election_section.html` partial in static mode — or simply inline the equivalent
  static markup with the "Link to election" / "Change" button wired up

### Phase 5 — Pass elections context to candidate profile

- [x] In `candidate_detail` view, fetch `elections_for_form` (all elections for
  `candidate.jurisdiction`) and pass it in the context alongside `candidate`
- [x] Confirm the profile page renders correctly when the candidate has no jurisdiction set
  (elections list will be empty; hide the link button in that case)

### Phase 6 — Manual testing

- [x] Scenario A: candidate with no election → click "Link to election" → select an election →
  Save → section shows election name with "Change" button
- [x] Scenario B: candidate with an election → click "Change" → select a different election →
  Save → section updates to new election
- [x] Scenario C: candidate with an election → click "Change" → select "— none —" → Save →
  section reverts to "None" with "Link to election" button
- [x] Scenario D: click "Link to election" → select an election → click "Cancel" → widget
  returns to original state without saving
- [x] Scenario E: not logged in → no button shown, election displayed as plain text only
- [x] Scenario F: candidate has no jurisdiction → no button shown (no elections to choose from)
