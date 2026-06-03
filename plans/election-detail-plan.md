# Plan: Election Detail Page

## Problem

`election_detail` returns `HttpResponse("TODO")`. Elections are visible on jurisdiction pages
and now linkable from candidate profiles, but clicking one leads nowhere. The election detail
page is where the core Polium action — declaring a vote — should happen.

## Design goals (from design.md)

- Show all candidates in the election ranked by HF rating (the "rating → interest" stage of the
  loop).
- Allow any logged-in player to declare they will vote for (or have voted for) the highest-rated
  candidate they choose. Trust-based, unverified, full points.
- Points = base × (candidate rating / 100) × endorsement multiplier × blacklist multiplier.
  - Endorsed candidate: 2× multiplier (they made a public commitment; HF rewards engagement with
    them).
  - Blacklisted candidate: 0.25× multiplier (HF always rewards engagement, but signals distrust).
  - Normal: 1× multiplier.
- One declaration per player per election (enforced by `VoteDeclaration.unique_together`).
  Changing a declaration (switching candidate) is allowed but earns no additional points.
- Points are awarded only to email-verified players (enforced by `award_points()`).

## Page layout

```
[Election name]
[Jurisdiction name] · [election_date] · [Upcoming / Past badge]
[Official page →]  (if external_reference set)

─── Candidates ───────────────────────────────────────
  [sorted: non-blacklisted by rating desc, blacklisted at bottom]

  For each candidate:
  ┌──────────────────────────────────────────────────┐
  │ [Name]                          [Rating]%        │
  │ [Office]                        [HF Endorsed]    │
  │                                 [Blacklisted]    │
  │ [Declare vote]  /  [Declared ✓]  [View profile →]│
  └──────────────────────────────────────────────────┘

  [If none linked: "No candidates linked yet. Add them
   from the jurisdiction page or a candidate's profile."]

─── Your declaration ──────────────────────────────────
  [Only shown when player is authenticated]
  [If declared: "You declared for [Name] on [date].
   Change your declaration below."]
  [If not declared and election is upcoming: "Declare
   your vote to earn points."]
  [If election is past and no declaration: "This
   election has passed. Declarations are still accepted."]
```

The declare section is a Datastar SSE partial so it updates in place without a full page reload.

## Points configuration

Add a `POLIUM` settings dict to `hf/settings/base.py`:

```python
POLIUM = {
    "VOTE_DECLARATION_BASE": config("VOTE_DECLARATION_BASE", default=500, cast=int),
    "ENDORSED_MULTIPLIER": config("ENDORSED_MULTIPLIER", default=2.0, cast=float),
    "BLACKLIST_MULTIPLIER": config("BLACKLIST_MULTIPLIER", default=0.25, cast=float),
}
```

Points formula (computed in the service layer):
```
amount = base × (candidate.current_rating / 100) × multiplier
```
where `multiplier = ENDORSED_MULTIPLIER if is_endorsed else BLACKLIST_MULTIPLIER if is_blacklisted else 1.0`.

Points are rounded to 2 decimal places. A candidate with 0% rating earns 0 points — this is
intentional; you still get credit for participating once ratings are non-zero.

## Service function

New function `polium/service.py`:

```python
def declare_vote(player, candidate, election) -> Decimal:
    """Create or update a VoteDeclaration. Returns points awarded (0 if changing)."""
```

- Wraps everything in a DB transaction.
- Checks for an existing declaration on this election.
- If no existing: creates VoteDeclaration, calculates and awards points, returns amount.
- If existing for a different candidate: updates `candidate` FK on the existing record, no points.
- If existing for the same candidate: no-op, returns 0.

## New/changed files

| File | Change |
|---|---|
| `hf/settings/base.py` | Add `POLIUM` dict |
| `polium/service.py` | New file — `declare_vote()` service function |
| `polium/views.py` | Replace `election_detail` stub; add `election_declare` view |
| `polium/urls.py` | Add `elections/<sqid>/declare/` POST endpoint |
| `templates/polium/election_detail.html` | New full page template |
| `templates/polium/partials/election_declare_section.html` | New partial (Datastar target) |

No model changes. No migrations.

## View design

### `election_detail` (GET)

```python
def election_detail(request, sqid):
    election = get_object_or_404(Election, sqid=sqid)
    candidates = list(
        election.candidates
        .select_related("jurisdiction")
        .order_by("is_blacklisted", "-current_rating")
    )
    declaration = None
    if request.user.is_authenticated:
        declaration = VoteDeclaration.objects.filter(
            player=request.user, election=election
        ).select_related("candidate").first()
    return render(request, "polium/election_detail.html", {
        "election": election,
        "candidates": candidates,
        "declaration": declaration,
        "today": date.today(),
    })
```

### `election_declare` (POST, `@login_required`)

- Reads `candidate_sqid` from POST body (standard form POST — no Datastar signals needed here,
  buttons can use a simple `<form method="post">`).
- Validates candidate belongs to this election.
- Calls `service.declare_vote(request.user, candidate, election)`.
- Returns a Datastar SSE patch of the `#election-declare-section` partial showing the updated
  declaration state and points awarded (if any).

### Helper

```python
def _declare_ctx(election, candidates, declaration, points_awarded=None):
    return {
        "election": election,
        "candidates": candidates,
        "declaration": declaration,
        "points_awarded": points_awarded,
    }
```

## Partial template (`election_declare_section.html`)

Renders inside `<div id="election-declare-section">`. States:

1. **Not authenticated** — "Log in to declare your vote."
2. **No declaration yet** — list of candidates with a "Declare" button for each, plus a points
   preview line ("Worth up to ~X pts").
3. **Declaration exists** — "[Name] · [office] — declared on [date]" plus a "Change" link that
   re-shows the candidate list with "Change to this candidate" buttons.
4. **Just declared** — same as (3) but with a green "You earned X points" flash line.

## URL changes

```python
path("elections/<str:sqid>/declare/", views.election_declare, name="election_declare"),
```

The existing `candidates/<str:sqid>/declare/` stub is left in place for future use (candidate-
centric declaration flow is a natural future feature); it is not wired up here.

---

## Todo List

### Phase 1 — Settings

- [x] Add `POLIUM` dict to `hf/settings/base.py` with `VOTE_DECLARATION_BASE` (default 500),
  `ENDORSED_MULTIPLIER` (default 2.0), `BLACKLIST_MULTIPLIER` (default 0.25), all
  `config()`-backed for admin override without deployment

### Phase 2 — Service layer

- [x] Create `polium/service.py`
- [x] Import `Decimal`, `transaction`, `award_points`, `VoteDeclaration`, `Candidate`,
  `Election`, `Player` (TYPE_CHECKING only)
- [x] Implement `declare_vote(player, candidate, election) -> Decimal`:
  - [x] Compute `multiplier`: `ENDORSED_MULTIPLIER` if `candidate.is_endorsed`, else
    `BLACKLIST_MULTIPLIER` if `candidate.is_blacklisted`, else `1.0`
  - [x] Compute `amount = Decimal(settings.POLIUM["VOTE_DECLARATION_BASE"]) * (candidate.current_rating / 100) * Decimal(str(multiplier))`, rounded to 2dp
  - [x] Inside `atomic()`: get-or-create-or-update the `VoteDeclaration`:
    - If none exists: `VoteDeclaration.objects.create(...)`, call `award_points(player, amount, "vote_declaration", source=declaration)`, return `amount`
    - If exists for same candidate: return `Decimal("0")`
    - If exists for different candidate: `declaration.candidate = candidate; declaration.save(update_fields=["candidate"])`, return `Decimal("0")`

### Phase 3 — URL

- [x] Add `path("elections/<str:sqid>/declare/", views.election_declare, name="election_declare")` to `polium/urls.py` (before the existing `elections/<str:sqid>/` catch-all)

### Phase 4 — Views

- [x] Add `VoteDeclaration` to the imports in `polium/views.py`
- [x] Add `service` import: `from . import service`
- [x] Replace `election_detail` stub with full view:
  - [x] `get_object_or_404(Election, sqid=sqid)`
  - [x] Fetch `candidates` from `election.candidates.select_related("jurisdiction").order_by("is_blacklisted", "-current_rating")`
  - [x] Fetch `declaration` if authenticated (else `None`)
  - [x] Pass `today = date.today()` in context
  - [x] Render `"polium/election_detail.html"`
- [x] Add `_declare_ctx(election, candidates, declaration, points_awarded=None)` helper
- [x] Add `election_declare` view (`@login_required`, `@require_POST`):
  - [x] `get_object_or_404(Election, sqid=sqid)`
  - [x] Read `candidate_sqid` from Datastar signals (via `read_signals()`)
  - [x] Validate: candidate must exist and belong to this election; on error re-render partial with error message
  - [x] Call `service.declare_vote(request.user, candidate, election)` → `points_awarded`
  - [x] Re-fetch `declaration` from DB (now updated)
  - [x] Fetch `candidates` for the partial
  - [x] Return Datastar SSE patch of `#election-declare-section` with updated partial

### Phase 5 — Partial template (`election_declare_section.html`)

- [x] Create `templates/polium/partials/election_declare_section.html`
- [x] Wrap in `<div id="election-declare-section" data-signals='{"candidate_sqid": ""}'>`
- [x] **State: not authenticated** — show candidate list + "Log in to declare" note below
- [x] **State: declaration exists, not in change mode** — show declared candidate name + office + date; show "Change" `<details>` that reveals the candidate list
- [x] **State: candidate list (no declaration, or in change mode)** — for each candidate show:
  - [x] Name, office, rating%, endorsement badge, blacklisted badge
  - [x] Points preview: "Worth ~[computed_preview] pts"
  - [x] Declare button: `data-on:click="$candidate_sqid = '{{ candidate.sqid }}'; @post(...)"`
  - [x] If this candidate is already the declared one: show "Declared ✓" instead of button
- [x] **State: just declared (`points_awarded` is not None and > 0)** — same as declaration-exists state but with green "+X points earned" message above it

### Phase 6 — Main election detail template

- [x] Create `templates/polium/election_detail.html` extending `base.html`
- [x] Header section:
  - [x] Election name as `<h1>`
  - [x] Jurisdiction name linked to jurisdiction detail (if jurisdiction set)
  - [x] Election date, formatted
  - [x] Status badge: "Upcoming" (green) if `election.election_date >= today`, "Past" (grey) if before
  - [x] External reference link if `election.external_reference` is set
- [x] Candidates section:
  - [x] Section heading "Candidates"
  - [x] If candidates exist: render each as a card (name, office, rating, badges, link to profile)
  - [x] `{% ifchanged %}` separates blacklisted candidates with a label
  - [x] If no candidates: message linking to jurisdiction page
- [x] Declare section:
  - [x] `{% include "polium/partials/election_declare_section.html" %}`

### Phase 7 — Points preview helper

- [x] `_points_preview(candidate)` in views.py; passed as `candidates_with_preview` list of tuples

### Phase 8 — Tests

- [x] `test_election_detail_renders` — GET returns 200, shows election name
- [x] `test_election_detail_shows_linked_candidate` — candidates linked to election appear
- [x] `test_election_detail_hides_unlinked_candidate` — candidates NOT in election absent
- [x] `test_election_detail_upcoming_badge` — future election shows "Upcoming"
- [x] `test_election_detail_past_badge` — past election shows "Past"
- [x] `test_declare_creates_record` — POST creates VoteDeclaration
- [x] `test_declare_awards_points` — points awarded on first declaration
- [x] `test_declare_change_no_extra_points` — changing candidate awards 0 additional points
- [x] `test_declare_same_candidate_idempotent` — declaring same candidate again is a no-op
- [x] `test_declare_endorsed_2x` — endorsed candidate gives 2× points
- [x] `test_declare_blacklisted_025x` — blacklisted candidate gives 0.25× points
- [x] `test_election_declare_view_creates_record` — view creates record via Datastar POST
- [x] `test_election_declare_view_rejects_wrong_election` — candidate from different election rejected
- [x] `test_election_declare_view_requires_login` — unauthenticated POST redirects

### Phase 9 — Manual verification

- [x] Scenario A: visit election page → see candidates ranked by rating, blacklisted section below
- [x] Scenario B: declare vote → Datastar SSE updates DOM in place, points flash shown
- [x] Scenario C: change declaration → candidate updates, no new points
- [x] Scenario D: anonymous user → sees page and candidates, no Declare buttons, Log in prompt
- [x] Scenario E: election with no candidates → message shown, no crash
