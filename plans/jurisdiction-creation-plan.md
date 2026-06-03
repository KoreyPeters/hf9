# Jurisdiction Creation Plan

## What we're building

Players searching for a jurisdiction that doesn't exist yet should be able to create it without leaving the flow. The design requires duplicate prevention UX first (show matches before offering creation), open creation (no maturity gate), and an auto-follow on creation.

This also fixes a pre-existing gap: `active_engagement` on `Jurisdiction` is never updated anywhere, so the deprecation mechanic is currently blind to follower counts. We fix that here since follower count is what `active_engagement` means for jurisdictions.

---

## User flow (design.md §Community Lifecycle Management)

1. Player types a jurisdiction name in the search box on the Polium home page (both `no_follows` and `no_elections` states).
2. Results stream back live as they type (Datastar active search, 300ms debounce).
3. **Results found** → player selects one → follows it → redirected to home (existing behaviour). A "Don't see yours? Add it." link appears below the results.
4. **No results** → "No jurisdictions found. Add '{{query}}'?" appears with an "Add it" button.
5. Clicking "Add it" (either variant) reveals an inline create form via Datastar click-to-edit — the form replaces the results area in-place, pre-filled with the current query.
6. The creation form collects: name (pre-filled), level (dropdown), optional parent jurisdiction (separate search using the same endpoint).
7. On submit → jurisdiction created with `status=active`, `active_engagement=1`, `created_by=request.user` → player auto-followed with `DEPTH_ALL` → standard redirect to home.

The create option is always secondary to selecting an existing result. Creating new requires a deliberate extra step (clicking the "Add it" link to reveal the form).

---

## Jurisdiction levels

The `level` CharField currently has no enforced choices. The creation form will offer:

| Value stored | Label shown |
|---|---|
| `country` | Country |
| `province` | State / Province / Territory |
| `region` | County / District / Region |
| `city` | City / Municipality / Borough |
| `other` | Other |

No migration needed — the field stays a plain `CharField`.

---

## Backend changes

### 1. `jurisdiction_search` view — convert to Datastar SSE

The existing view returns JSON and is driven by a vanilla JS fetch. Replace it entirely with a Datastar SSE endpoint that returns an HTML fragment for `#search-results`.

- Method: `GET`
- Signal received: `$q` (the search query, sent automatically by Datastar)
- Returns: SSE `datastar-merge-fragments` replacing `#search-results`
- Fragment contains:
  - When `len(q) < 2`: empty `#search-results`
  - When results exist: a follow-form button per result, plus a "Don't see yours? Add it." link at the bottom
  - When no results: a "No jurisdictions found." message and an "Add '{{q}}'" button
- The "Add it" link/button carries `data-on:click="@get('/polium/jurisdictions/create-form/?name=$q')"` to trigger the form reveal

### 2. New `jurisdiction_create_form` view — Datastar click-to-edit

`GET /polium/jurisdictions/create-form/`

Returns a Datastar SSE fragment that replaces `#search-results` with the inline create form. Receives `name` from the query string (pre-filled from `$q`). No auth required to view the form.

The form itself is standard HTML:

```html
<div id="search-results">
  <form method="post" action="{% url 'polium:create_jurisdiction' %}">
    {% csrf_token %}
    <input type="text" name="name" value="{{ name }}">
    <select name="level">
      <option value="country">Country</option>
      <option value="province">State / Province / Territory</option>
      <option value="region">County / District / Region</option>
      <option value="city">City / Municipality / Borough</option>
      <option value="other">Other</option>
    </select>
    <input type="hidden" name="parent_sqid" id="parent-sqid-val">
    <input type="text" id="parent-search" placeholder="Parent jurisdiction (optional)"
           data-bind:parent_q=""
           data-on:input__debounce.300ms="@get('/polium/jurisdictions/search-parent/')">
    <div id="parent-results"></div>
    <button type="submit">Add jurisdiction</button>
    <button type="button" data-on:click="@get('/polium/jurisdictions/search/?q={{ name }}')">Cancel</button>
  </form>
</div>
```

Cancel re-runs the search for the original query, restoring the results view.

### 3. New `jurisdiction_search_parent` view — parent picker

`GET /polium/jurisdictions/search-parent/`

Identical logic to `jurisdiction_search` but returns a fragment for `#parent-results`. Each result is a button that sets the hidden `parent_sqid` field (via a signal or a small `data-on:click` that writes to a signal bound to the hidden input). No "create" option in this picker — parent must already exist.

### 4. New `create_jurisdiction` view

`POST /polium/jurisdictions/create/`

- `@login_required`
- Reads: `name` (required), `level` (required), `parent_sqid` (optional)
- Validates: `name` non-empty, `level` one of the five valid values
- Creates `Jurisdiction(name=..., level=..., parent=..., created_by=request.user)`
- Creates `JurisdictionFollow(player=request.user, jurisdiction=new, depth=DEPTH_ALL)`
- Sets `new.active_engagement = 1`
- Standard `redirect("polium:home")` — not Datastar (full page refresh is appropriate after creation)
- On validation error: redirect back with a `messages` error

### 5. `follow_jurisdiction` view — maintain `active_engagement`

Increment `active_engagement` atomically using `F()`, but only when the follow is actually new (use the `created` flag from `get_or_create`):

```python
if created:
    Jurisdiction.objects.filter(pk=jurisdiction.pk).update(
        active_engagement=F("active_engagement") + 1
    )
```

### 6. New `unfollow_jurisdiction` view

`POST /polium/jurisdictions/unfollow/`

- `@login_required`
- Deletes the `JurisdictionFollow` if it exists
- Decrements `active_engagement` by 1 (floor at 0) using `F()` + `Greatest(F(...) - 1, 0)`
- Redirects to home

### 7. URL additions

```python
path("jurisdictions/search/", views.jurisdiction_search, name="jurisdiction_search"),          # existing, converted
path("jurisdictions/search-parent/", views.jurisdiction_search_parent, name="jurisdiction_search_parent"),
path("jurisdictions/create-form/", views.jurisdiction_create_form, name="jurisdiction_create_form"),
path("jurisdictions/create/", views.create_jurisdiction, name="create_jurisdiction"),
path("jurisdictions/unfollow/", views.unfollow_jurisdiction, name="unfollow_jurisdiction"),
```

---

## Frontend changes

### Template structure (both `no_follows` and `no_elections` states)

Replace the duplicated vanilla JS fetch blocks with Datastar attributes. No manual JavaScript:

```html
<input type="text"
       placeholder="e.g. California, United Kingdom, Ontario…"
       autocomplete="off"
       data-bind:q=""
       data-on:input__debounce.300ms="@get('{% url "polium:jurisdiction_search" %}')">

<div id="search-results"></div>
```

That's it for the template — Datastar handles debounce, the GET request, and merging the response fragment into `#search-results`. No `<script>` block needed.

The two currently-duplicated JS blocks are removed entirely. If the two states (`no_follows` / `no_elections`) need different post-follow behaviour, that's handled by what the server returns, not by client logic.

### Partial templates

Three new partials under `templates/polium/partials/`:

- `search_results.html` — rendered by `jurisdiction_search`; contains result buttons + "Add it" link or "no results" + "Add it" button
- `create_form.html` — rendered by `jurisdiction_create_form`; the inline creation form
- `parent_results.html` — rendered by `jurisdiction_search_parent`; parent picker result buttons

---

## Implementation todo

- [x] `jurisdiction_search` — rewrite as Datastar SSE endpoint returning `search_results.html` fragment
- [x] `jurisdiction_create_form` — new Datastar SSE view returning `create_form.html` fragment
- [x] `jurisdiction_search_parent` — new Datastar SSE view returning `parent_results.html` fragment
- [x] `create_jurisdiction` — new POST view: validate, create, auto-follow, set active_engagement=1, redirect
- [x] `follow_jurisdiction` — add `F()` increment of `active_engagement` on actual create
- [x] `unfollow_jurisdiction` — new POST view: delete follow, decrement active_engagement floor 0
- [x] `urls.py` — add four new paths, keep existing `jurisdiction_search` path
- [x] `templates/polium/partials/search_results.html` — results list + "Add it" affordance
- [x] `templates/polium/partials/create_form.html` — inline create form with level dropdown and parent picker
- [x] `templates/polium/partials/parent_results.html` — parent picker results
- [x] `home.html` (both search states) — replaced JS fetch blocks with Datastar `data-bind`/`data-on` attributes
- [x] Tests — 13 new tests covering search, create, follow engagement, unfollow, auth guards; all 29 pass

---

## What this deliberately does not include

- **Maturity gate on creation** — the design explicitly says no maturity requirement to create, only to flag.
- **Duplicate flagging UI** — `JurisdictionDuplicateFlag` model exists; the flag action itself is a separate feature.
- **Admin-only creation** — open creation is a constitutional principle.
- **Unique constraint on name** — the same name can exist at different levels (e.g. "Wellington" city and "Wellington" region). No DB-level uniqueness.
- **Datastar for `create_jurisdiction` POST** — a standard redirect is correct here; full page refresh after creation is the right UX.
