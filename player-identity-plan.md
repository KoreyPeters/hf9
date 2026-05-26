# Player Identity and Display Name Plan

Delivers the **Player Identity and Display Name Design** — non-unique display names combined
with a SQID fragment for public disambiguation.

---

## What Already Exists

| Requirement | Status |
|---|---|
| `display_name` field on Player (no uniqueness constraint) | ✅ `accounts/models.py` |
| `sqid` generated from PK via `SqidMixin` | ✅ `core/models.py` |
| Social login pre-fills `display_name` from provider | ✅ `SocialAccountAdapter.populate_user` |
| No uniqueness check at signup | ✅ `accounts/views.signup` |
| SQID as canonical URL identifier | ✅ all existing detail URLs |

Nothing in the model layer needs to change. The work is a property, one template update,
a new profile view, and tests.

---

## Gaps to Close

### §1 — `Player.display_label` property ✅ COMPLETE

Add a single property to `Player` that produces the canonical public display string:

```python
# accounts/models.py

@property
def display_label(self) -> str:
    name = self.display_name or self.username
    fragment = (self.sqid or "")[:4]
    return f"{name} #{fragment}" if fragment else name
```

The `if fragment` guard handles the narrow window between `create_user()` and the first
`save()` when `sqid` is still `None` (the `SqidMixin` populates it on the first save call).
In practice this state is never visible publicly, but the guard prevents a bare `# ` suffix.

`__str__` stays as-is (`display_name or username`) — it is used in admin and log output
where the fragment adds noise.

**Files:** `accounts/models.py`
**Migration:** none

---

### §2 — Update existing template that displays player name ✅ COMPLETE

`templates/polium/home.html` line 9 shows the authenticated player's name in the nav bar:

```html
{{ user.display_name|default:user.username }}
```

Replace with:

```html
{{ user.display_label }}
```

`display_label` already falls back to `username` when `display_name` is blank, so the
`|default` filter is no longer needed.

**Files:** `templates/polium/home.html`

---

### §3 — Player profile page ✅ COMPLETE

URL: `GET /accounts/profile/<sqid>/`

Public, no `@login_required`. Authenticated users viewing their own profile see an "Edit
display name" link (stub — edit functionality is not in scope here). Everyone else just sees
the public card.

**View:**

```python
# accounts/views.py

def player_profile(request: HttpRequest, sqid: str) -> HttpResponse:
    player = get_object_or_404(Player, sqid=sqid)
    return render(request, "accounts/profile.html", {
        "profile_player": player,
        "is_own_profile": request.user.is_authenticated and request.user.pk == player.pk,
    })
```

**Template** (`templates/accounts/profile.html`): shows `profile_player.display_label`,
`profile_player.total_points`, `profile_player.date_joined`. Attribution surfaces
(survey history, vote declarations) will populate this page when those features are built —
no placeholder content needed now.

**URL:** `path("profile/<str:sqid>/", views.player_profile, name="player_profile")`
added to `accounts/urls.py`.

**Files:** `accounts/views.py`, `accounts/urls.py`, `templates/accounts/profile.html`

---

### §4 — Future attribution surfaces (no work now)

These views are currently `TODO` stubs. When built, they must use `player.display_label`
for any public attribution:

| Surface | Model | Field |
|---|---|---|
| Survey responses on candidate/jurisdiction pages | `SurveyResponse.player` | `player.display_label` |
| Public vote declarations | `VoteDeclaration.player` | `player.display_label` |

No code changes in scope — noted here so the pattern is established before those views
are implemented.

---

## File Summary

| File | Change |
|---|---|
| `accounts/models.py` | Add `display_label` property to `Player` |
| `templates/polium/home.html` | Replace `display_name\|default:username` with `display_label` |
| `accounts/views.py` | Add `player_profile` view |
| `accounts/urls.py` | Add `profile/<str:sqid>/` route |
| `templates/accounts/profile.html` | New — public player profile card |

---

## Tests to Add ✅ COMPLETE

| Test | Location | Status |
|---|---|---|
| `display_label` returns `"{name} #{sqid[:4]}"` for a player with both fields | `accounts/tests.py` | ✅ |
| `display_label` falls back to `username` when `display_name` is blank | `accounts/tests.py` | ✅ |
| `display_label` omits fragment when `sqid` is None | `accounts/tests.py` | ✅ |
| `GET /accounts/profile/<sqid>/` returns 200 for anonymous visitor | `accounts/tests.py` | ✅ |
| `GET /accounts/profile/<sqid>/` returns 200 for authenticated owner | `accounts/tests.py` | ✅ |
| Profile response contains `display_label` text | `accounts/tests.py` | ✅ |
| Profile response for unknown sqid returns 404 | `accounts/tests.py` | ✅ |

---

## Sequencing

1. **§1** — property on model (no migration, zero risk, prerequisite for everything else)
2. **§2** — template tweak (one line change)
3. **§3** — profile view, URL, template
4. Tests alongside each section
