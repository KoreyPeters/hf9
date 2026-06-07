# Points Redesign Plan

## What the design now says

The design has been updated with a precise, unified points formula that applies to both Polium and Spendium:

**Polium (vote declaration):**
> total points = Σ (criterion value × criterion probability) × modifier × membership multiplier

**Spendium (purchase):**
> total points = dollars spent × Σ (criterion value × criterion probability) × membership multiplier

Where:
- **criterion value** — `Criterion.weight`, now interpreted as an absolute point value (e.g. 100 pts), not a relative weighting ratio
- **criterion probability** — the fraction of "yes" survey responses for that criterion on this subject in the last 12 months (e.g. 9 of 10 = 0.9)
- **only criteria with ≥ k survey responses** are included (k = 5, configurable). Criteria below threshold are excluded entirely — not counted as 0%.
- **modifier** (Polium only): 1.0 standard, 2.0 endorsed, 0.25 blacklisted
- **membership multiplier**: 1.0 (free player), 1.5 (Member, $10/yr), 2.0 (Sustaining Member, $50/yr)

There is no ceiling on points.

---

## What is currently wrong

### 1. Wrong declaration formula
`polium/service.py` computes `base * candidate.current_rating * modifier` where `base = 500`. This uses a pre-aggregated rating scalar. The design requires the raw per-criterion sum: Σ(weight × probability), computed fresh at declaration time.

### 2. `Criterion.weight` is a ratio, not a point value
`compute_rating()` in `surveys/ratings.py` uses `weight` as a relative weighting factor in a weighted average producing a 0–1 number. The design says `weight` IS the point value — a criterion weighted at 100 contributes 90 pts when 90% of surveys answered "yes." The two usages are compatible (a higher point-value criterion should also have more influence on the aggregate rating), but the seed data weights (1.0, 1.5, 2.0) produce trivially small point totals when used as point values. They must be updated.

### 3. Minimum survey threshold (k) not enforced
No k threshold exists anywhere. A criterion with a single "yes" response would contribute the full criterion value, which is noisy and wrong.

### 4. Membership multipliers not implemented
`Membership` model has no tier field. `award_points()` applies no multiplier. The 1.5× / 2× benefit of membership doesn't exist.

### 5. `VOTE_DECLARATION_BASE` is obsolete
The new formula has no fixed base constant. The `POLIUM["VOTE_DECLARATION_BASE"]` setting and all references to it should be removed.

### 6. `award_points()` returns None
With multipliers now applied inside `award_points()`, callers need to know the actual awarded amount. The return type must change to `Decimal`.

---

## What does NOT change

- `compute_rating()` — the 0–1 aggregate rating used for the percentage display and blacklisting logic remains correct. Higher-weight criteria still have more influence on the aggregate.
- `Candidate.current_rating` — still stores the 0–1 aggregate.
- `update_candidate_rating` task — unchanged.
- Survey points (100/50/25) — unchanged.
- Cooldown and diminishing returns — unchanged.

---

## Files changed

| File | Change |
|---|---|
| `surveys/ratings.py` | Add `compute_declaration_points(subject)` — the new core formula |
| `surveys/models.py` | Add `min_survey_threshold` to `SurveyConfig` (default 5) |
| `surveys/service.py` | `submit_survey()` returns `tuple[SurveyResponse, Decimal]` (points awarded) |
| `points/service.py` | `award_points()` applies membership multiplier; returns `Decimal` |
| `accounts/models.py` | Add `tier` field to `Membership` (`member` / `sustaining_member`) |
| `polium/service.py` | `declare_vote()` uses `compute_declaration_points()` |
| `polium/views.py` | `_points_preview()` uses `compute_declaration_points()`; survey view uses new return type |
| `hf/settings/base.py` | Remove `VOTE_DECLARATION_BASE`; add `MEMBER_MULTIPLIER`, `SUSTAINING_MEMBER_MULTIPLIER` |
| `polium/management/commands/seed_criteria.py` | Update weights to meaningful point values |
| `polium/tests.py` | Update declare_vote tests for new formula |
| `surveys/tests.py` | Update points tests for membership multiplier; add k-threshold tests |
| Migrations | `SurveyConfig.min_survey_threshold`; `Membership.tier` |

---

## Detailed design

### `compute_declaration_points(subject: Model) -> Decimal`

New function in `surveys/ratings.py`. Called by `polium/service.py` and `polium/views.py`.

```python
def compute_declaration_points(subject: Model) -> Decimal:
    """Σ (criterion weight × yes_probability) for criteria with ≥ k responses."""
    cutoff = timezone.now() - timedelta(days=365)
    ct = ContentType.objects.get_for_model(subject)

    responses = SurveyResponse.objects.filter(
        content_type=ct, object_id=subject.pk, submitted_at__gte=cutoff
    )
    if not responses.exists():
        return Decimal("0")

    # Count yes and total per criterion
    from django.db.models import Count
    rows = (
        CriterionAnswer.objects
        .filter(survey_response__in=responses, criterion__is_active=True)
        .values("criterion_id", "answer")
        .annotate(n=Count("pk"))
    )
    yes_counts: dict[int, int] = {}
    total_counts: dict[int, int] = {}
    for row in rows:
        cid = row["criterion_id"]
        total_counts[cid] = total_counts.get(cid, 0) + row["n"]
        if row["answer"]:
            yes_counts[cid] = yes_counts.get(cid, 0) + row["n"]

    k = SurveyConfig.get().min_survey_threshold
    eligible = [cid for cid, total in total_counts.items() if total >= k]
    if not eligible:
        return Decimal("0")

    weights = {
        c.pk: c.weight
        for c in Criterion.objects.filter(pk__in=eligible, is_active=True)
    }

    total = Decimal("0")
    for cid in eligible:
        if cid not in weights:
            continue
        prob = Decimal(yes_counts.get(cid, 0)) / Decimal(total_counts[cid])
        total += weights[cid] * prob

    return total.quantize(Decimal("0.01"))
```

### `award_points()` with membership multiplier

```python
def _membership_multiplier(player: Player) -> Decimal:
    try:
        m = player.membership
    except Exception:
        return Decimal("1")
    if not m.is_active or m.expires_at < timezone.now():
        return Decimal("1")
    if m.tier == "sustaining_member":
        return Decimal(str(settings.SUSTAINING_MEMBER_MULTIPLIER))
    return Decimal(str(settings.MEMBER_MULTIPLIER))

def award_points(player, amount, reason, source=None) -> Decimal:
    if not player.email_verified:
        return Decimal("0")
    multiplier = _membership_multiplier(player)
    final = (Decimal(str(amount)) * multiplier).quantize(Decimal("0.01"))
    with atomic():
        PointTransaction.objects.create(player=player, amount=final, reason=reason, ...)
        Player.objects.filter(pk=player.pk).update(total_points=F("total_points") + final)
    return final
```

### `submit_survey()` return type

Changes from `-> SurveyResponse` to `-> tuple[SurveyResponse, Decimal]`. The second element is the actual points awarded (after membership multiplier, or 0 if email unverified).

Callers:
- `polium/views.py` survey view — unpacks the tuple; uses the points amount for the "submitted" confirmation
- Tests — updated to unpack

### `declare_vote()` updated formula

```python
def declare_vote(player, candidate, election) -> Decimal:
    from surveys.ratings import compute_declaration_points
    base = compute_declaration_points(candidate)
    pre_multiplier = (base * _vote_multiplier(candidate)).quantize(Decimal("0.01"))
    ...
    awarded = award_points(player, pre_multiplier, "vote_declaration", source=declaration)
    return awarded
```

The membership multiplier is applied inside `award_points()`, so `declare_vote()` returns the actual amount earned.

### `Membership.tier`

```python
TIER_MEMBER = "member"
TIER_SUSTAINING = "sustaining_member"
TIER_CHOICES = [
    (TIER_MEMBER, "Member"),
    (TIER_SUSTAINING, "Sustaining Member"),
]
tier = models.CharField(max_length=20, choices=TIER_CHOICES, default=TIER_MEMBER)
```

### `SurveyConfig.min_survey_threshold`

```python
min_survey_threshold = models.PositiveIntegerField(
    default=5,
    help_text="Minimum survey responses a criterion must have to count toward points.",
)
```

### Settings

Remove:
```python
"VOTE_DECLARATION_BASE": config("VOTE_DECLARATION_BASE", default=500, cast=int),
```

Add at module level (not inside POLIUM dict):
```python
MEMBER_MULTIPLIER = config("MEMBER_MULTIPLIER", default=1.5, cast=float)
SUSTAINING_MEMBER_MULTIPLIER = config("SUSTAINING_MEMBER_MULTIPLIER", default=2.0, cast=float)
```

### Seed criteria weights

Current weights are relative ratios (1.0, 1.5). Update to absolute point values. Proposed:

| Criterion | New weight |
|---|---|
| "Has the candidate voted consistently to reduce carbon emissions?" | 100 |
| "Has the candidate opposed subsidies for fossil fuel industries?" | 75 |

At k=5 with these values, a candidate with 8 of 10 surveys answering "yes" to the 100-pt criterion earns `100 × 0.8 = 80` base declaration points. With endorsement (2×) and membership (1.5×): `80 × 2 × 1.5 = 240` points. These are small but meaningful numbers that grow dramatically as more criteria are added and ratings mature.

The seed command uses `get_or_create`, so it won't update existing rows. A `migrations.RunPython` data migration is needed to update existing weight values, or the command is updated to also call `.update()` on existing criteria.

---

## Todo steps

- [ ] Add `min_survey_threshold` to `SurveyConfig`; generate and run migration
- [ ] Add `tier` to `Membership`; generate and run migration
- [ ] Add `MEMBER_MULTIPLIER` and `SUSTAINING_MEMBER_MULTIPLIER` to `hf/settings/base.py`; remove `VOTE_DECLARATION_BASE`
- [ ] Add `_membership_multiplier()` helper and update `award_points()` to apply it and return `Decimal`
- [ ] Add `compute_declaration_points()` to `surveys/ratings.py`
- [ ] Update `surveys/service.py` `submit_survey()` to return `tuple[SurveyResponse, Decimal]`
- [ ] Update `polium/service.py` `declare_vote()` to use `compute_declaration_points()`
- [ ] Update `polium/views.py`: `_points_preview()` and survey submit view for new return type
- [ ] Update seed criteria weights and add a data migration (or update script) for existing rows
- [ ] Update all affected tests
- [ ] Run full test suite
