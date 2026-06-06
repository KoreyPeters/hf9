# Player Profile Plan

## Per-game or combined?

**Recommendation: one profile URL, sectioned by game.**

The player is one person. Their points total is unified across games, their SQID is on the `Player` model, and their display label (`John Smith #a3kx`) is the single canonical way to identify them across the platform. Splitting into per-game profile URLs would mean either duplicating identity information or building cross-linking between profiles — neither is worth it.

The case against fully merged (everything in one undifferentiated feed): Polium activity (political opinions, vote declarations) and Spendium activity (purchase history) have very different privacy sensitivities. The design has separate privacy policies for each game precisely because of this. A player may want their candidate surveys publicly visible but their purchase history entirely private. Game sections make this natural to control independently later.

**The profile stays at `/accounts/profile/<sqid>/`.** Content is divided into clearly labelled game sections.

---

## Own profile vs. others' profiles

This distinction drives almost every layout decision.

**Own profile** — full detail in all sections. This is the player's personal dashboard.

**Others' profiles** — only what the player has made public. Today this is just name + member since + total points. Once the `is_public` toggle is added to `SurveyResponse` (on the "not yet built" list), public surveys with notes appear here too.

The view already has `is_own_profile` in context. The template uses it to gate sections.

---

## What to surface

### Header — always, for everyone

| Item | Source |
|---|---|
| Avatar initial (letter in a circle) | `profile_player.display_name` |
| Display name + SQID fragment | `profile_player.display_label` |
| Member since | `profile_player.date_joined` |
| Total points | `profile_player.total_points` |

Already partially built. No changes needed to the view — just the template.

---

### Polium section — own profile now; public surveys later

**Own profile:**

*Candidate surveys* — all `SurveyResponse` objects where `content_type` = `Candidate`, ordered by `submitted_at` descending. Each row shows:
- Candidate name (linked to their profile)
- Date of last survey
- How many times surveyed (`submit_count`)
- Points earned (derive from `submit_count`: 100 / 50 / 25 — or look up the `PointTransaction` where `source` = this `SurveyResponse`)

*Vote declarations* — all `VoteDeclaration` objects for this player, ordered by `declared_at` descending. Each row shows:
- Candidate name (linked)
- Election name (linked)
- Date declared
- Points earned (look up `PointTransaction` where `source` = this `VoteDeclaration`)

**Others' profile:**
Nothing shown here until `SurveyResponse.is_public` is built. No empty section — the Polium section is omitted entirely from public views for now rather than showing a blank panel.

**Dependency:** Public display of surveys requires adding `is_public: BooleanField(default=False)` and `note: CharField(max_length=200, blank=True)` to `SurveyResponse`. These are on the "not yet built" list. This plan does not add them — it builds the own-profile view now and leaves a clear hook for public display later.

---

### Spendium section — future

No Spendium surveys or purchases exist yet. The section is omitted entirely from the profile until Spendium activity is built. A placeholder is not shown — an empty section signals "nothing here" which is confusing and looks broken.

---

### Points history — own profile only

All `PointTransaction` rows for the player, ordered by `created_at` descending, paginated or capped at 50 most recent.

Each row:
- Human-readable reason (map `"survey"` → "Candidate survey", `"vote_declaration"` → "Vote declaration")
- Amount (with `+` prefix)
- Date
- Source link (if `source` resolves to a `Candidate`, link to their profile; if a `VoteDeclaration`, link to the election)

The `PointTransaction.source` is a GenericFK. Resolving it in the view (not the template) keeps the template clean. The view fetches all transactions, resolves each source to a URL, and passes a flat list of dicts.

---

## Files changed

| File | Change |
|---|---|
| `accounts/views.py` | Expand `player_profile()` to fetch and pass Polium surveys, vote declarations, and point transactions |
| `templates/accounts/profile.html` | Rebuild with header, Polium section (own only), points history (own only) |

No new URLs. No new models. No migrations.

---

## Todo steps

- [ ] Update `player_profile()` view to fetch survey responses, vote declarations, and point transactions for own-profile view
- [ ] Rebuild `templates/accounts/profile.html`:
  - Header (avatar, name, member since, points)
  - Polium section: surveys table + vote declarations table (own only)
  - Points history table (own only, 50 most recent)
- [ ] Run tests

---

## What this plan defers

- **Public survey display** — requires `is_public` + `note` on `SurveyResponse`. Not added here; left as a clean hook.
- **Display name editing** — the template currently says "coming soon." Left as-is; it's a separate UX task.
- **Spendium section** — no data to show yet.
- **Pagination** — the points history cap of 50 is sufficient for now. Full pagination is a polish task.
