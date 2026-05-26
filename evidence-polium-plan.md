# Evidence — Polium Implementation Plan

Implements the Evidence section of `design.md` for Polium. Evidence is the research
layer that sits beneath the rating layer — community-submitted links to publicly
verifiable records that inform surveys on a candidate's profile page.

Spendium evidence is explicitly out of scope.

---

## What Exists

The `evidence` app was built speculatively and is closely aligned with the design.
No model changes are needed.

| Component | State |
|---|---|
| `Evidence` model (url, note, criterion, subject GFK, submitter, status, net_usefulness_score, submitted_at) | ✅ Complete |
| `EvidenceUsefulness` (player, evidence, is_useful, unique_together) | ✅ Complete |
| `EvidenceFlag` (flagging_player, evidence, reason, unique_together) | ✅ Complete |
| `recompute_evidence_status()` — auto-hides on flag threshold | ✅ Complete |
| `recompute_usefulness_score()` — recalculates net score | ✅ Complete |
| `EvidenceAdmin` registration | ✅ Complete |
| `submit_evidence()` service function | ✅ Complete |
| `vote_usefulness()` service function | ✅ Complete |
| `flag_evidence()` service function | ✅ Complete |
| `candidate_detail` view | ✅ Complete |
| Evidence action views | ✅ Complete |
| Evidence action URL patterns | ✅ Complete |
| Candidate profile template | ✅ Complete |
| Tests | ✅ Complete (22 tests, all passing) |

---

## Design Rules to Enforce

From `design.md § Evidence`:

- **Any authenticated player** may submit evidence. No maturity gate.
- **Any authenticated player** may vote usefulness. One vote per player per piece of
  evidence; changing your mind is allowed (update-or-create).
- **Only mature players** may flag. Maturity: account ≥7 days old AND ≥3 survey
  responses submitted (per `settings.LIFECYCLE`). Immature players receive an error.
- Evidence earns **no points**.
- Evidence is **never deleted** — the full record remains. Hiding (`STATUS_HIDDEN`) is the
  community response to poor evidence; permanent removal (`STATUS_REMOVED`) is an admin-only
  action for genuinely harmful content.
- Evidence on the candidate profile is sorted by `net_usefulness_score` descending —
  most useful at top, least useful at bottom.
- Only `STATUS_VISIBLE` evidence is shown on the public profile. `STATUS_HIDDEN` and
  `STATUS_REMOVED` are invisible to players but remain in the database and in admin.

---

## What Changes

### §1 — `evidence/service.py` — three new functions

Add two exception classes and three service functions.

**Exceptions:**

```python
class NotMatureError(Exception):
    pass

class AlreadyFlaggedError(Exception):
    pass
```

**`submit_evidence(player, subject, url, note, criterion=None)`:**

```python
def submit_evidence(
    player: Player,
    subject: Model,
    url: str,
    note: str,
    criterion: Criterion | None = None,
) -> Evidence:
    ct = ContentType.objects.get_for_model(subject)
    return Evidence.objects.create(
        content_type=ct,
        object_id=subject.pk,
        submitted_by=player,
        url=url,
        note=note,
        criterion=criterion,
    )
```

No maturity check. No points call. Returns the created `Evidence` instance.

**`vote_usefulness(player, evidence, is_useful)`:**

```python
def vote_usefulness(player: Player, evidence: Evidence, is_useful: bool) -> EvidenceUsefulness:
    vote, _ = EvidenceUsefulness.objects.update_or_create(
        player=player,
        evidence=evidence,
        defaults={"is_useful": is_useful},
    )
    recompute_usefulness_score(evidence)
    recompute_evidence_status(evidence)
    return vote
```

`update_or_create` handles the change-of-mind case (player previously voted useful,
now votes not useful). After each vote the score and status are recomputed.

**`flag_evidence(player, evidence, reason)`:**

```python
def flag_evidence(player: Player, evidence: Evidence, reason: str) -> EvidenceFlag:
    from django.conf import settings
    from django.utils import timezone
    from surveys.models import SurveyResponse

    maturity_days = settings.LIFECYCLE["MATURITY_ACCOUNT_AGE_DAYS"]
    maturity_surveys = settings.LIFECYCLE["MATURITY_SURVEY_COUNT"]
    account_age = (timezone.now() - player.date_joined).days
    survey_count = SurveyResponse.objects.filter(player=player).count()

    if account_age < maturity_days or survey_count < maturity_surveys:
        raise NotMatureError(
            f"Account must be {maturity_days}+ days old with {maturity_surveys}+ surveys."
        )

    if EvidenceFlag.objects.filter(flagging_player=player, evidence=evidence).exists():
        raise AlreadyFlaggedError("You have already flagged this evidence.")

    flag = EvidenceFlag.objects.create(
        flagging_player=player,
        evidence=evidence,
        reason=reason,
    )
    recompute_evidence_status(evidence)
    return flag
```

Raises `NotMatureError` before touching the database if the player is not mature.
Raises `AlreadyFlaggedError` for a clean error message (the DB `unique_together`
is a safety net, not the primary guard).

All three functions are called from views — none are Cloud Tasks or background jobs.
The recompute calls are synchronous and fast (a handful of COUNT queries).

---

### §2 — `polium/views.py` — implement `candidate_detail` and three evidence action views

**`candidate_detail(request, sqid)`** replaces the current `HttpResponse("TODO")` stub:

```python
def candidate_detail(request: HttpRequest, sqid: str) -> HttpResponse:
    candidate = get_object_or_404(Candidate, sqid=sqid)
    ct = ContentType.objects.get_for_model(Candidate)
    evidence_qs = (
        Evidence.objects.filter(
            content_type=ct,
            object_id=candidate.pk,
            status=Evidence.STATUS_VISIBLE,
        )
        .select_related("submitted_by", "criterion")
        .order_by("-net_usefulness_score", "-submitted_at")
    )
    blacklist_record = (
        candidate.blacklist_history.order_by("-blacklisted_at").first()
        if candidate.is_blacklisted else None
    )
    criteria = Criterion.objects.filter(is_active=True).order_by("category__name", "question")
    return render(request, "polium/candidate_profile.html", {
        "candidate": candidate,
        "evidence_list": evidence_qs,
        "blacklist_record": blacklist_record,
        "criteria": criteria,
        "flag_reasons": EvidenceFlag.REASON_CHOICES,
    })
```

No login required — anonymous browsing is permitted per the design.

**`evidence_submit(request, sqid)`** — POST only, login required:

```python
@login_required
@require_POST
def evidence_submit(request: HttpRequest, sqid: str) -> HttpResponse:
    candidate = get_object_or_404(Candidate, sqid=sqid)
    url = request.POST.get("url", "").strip()
    note = request.POST.get("note", "").strip()
    criterion_id = request.POST.get("criterion_id") or None
    criterion = get_object_or_404(Criterion, pk=criterion_id) if criterion_id else None
    if url and note:
        submit_evidence(request.user, candidate, url, note, criterion)
    return redirect("polium:candidate_detail", sqid=sqid)
```

Redirects back to the profile on success. Silently ignores empty submissions (the
template uses `required` on both fields). A future iteration can add inline
Datastar validation feedback.

**`evidence_vote(request, pk)`** — POST only, login required:

```python
@login_required
@require_POST
def evidence_vote(request: HttpRequest, pk: int) -> HttpResponse:
    evidence = get_object_or_404(Evidence, pk=pk)
    is_useful = request.POST.get("is_useful") == "true"
    vote_usefulness(request.user, evidence, is_useful)
    return redirect(request.META.get("HTTP_REFERER", "polium:home"))
```

Redirects to the referring page (the candidate profile). Datastar enhancement
can replace this with an inline score update in a later iteration.

**`evidence_flag(request, pk)`** — POST only, login required:

```python
@login_required
@require_POST
def evidence_flag(request: HttpRequest, pk: int) -> HttpResponse:
    evidence = get_object_or_404(Evidence, pk=pk)
    reason = request.POST.get("reason", EvidenceFlag.REASON_IRRELEVANT)
    try:
        flag_evidence(request.user, evidence, reason)
    except NotMatureError:
        messages.error(request, "Your account must be at least 7 days old with 3 surveys submitted to flag evidence.")
    except AlreadyFlaggedError:
        messages.error(request, "You have already flagged this evidence.")
    return redirect(request.META.get("HTTP_REFERER", "polium:home"))
```

---

### §3 — `polium/urls.py` — three new evidence action patterns

```python
path("candidates/<str:sqid>/evidence/submit/", views.evidence_submit, name="evidence_submit"),
path("evidence/<int:pk>/vote/", views.evidence_vote, name="evidence_vote"),
path("evidence/<int:pk>/flag/", views.evidence_flag, name="evidence_flag"),
```

The submit URL is nested under `candidates/<sqid>/` so the candidate is always
available from the URL without a hidden form field.

---

### §4 — `templates/polium/candidate_profile.html`

The candidate profile has four sections in order:

**Header** — name, office, jurisdiction, current rating (as a percentage). If the
candidate is endorsed, a small "HF Endorsed" badge. If blacklisted, a prominent red
notice block containing `blacklist_record.blacklisted_at`, `blacklist_record.reason`,
and a link to `blacklist_record.forum_discussion_url`.

**Evidence** — placed before any future survey section, consistent with the design's
stated intent that evidence informs surveys. Contains:

- The sorted evidence list. Each item shows:
  - The URL as a truncated external link (opens in new tab)
  - The note
  - If `criterion` is set: a small tag with `criterion.question[:60]`
  - Submitter: `evidence.submitted_by.display_label` and `evidence.submitted_at|date`
  - Net usefulness score
  - For authenticated players: "Useful" and "Not useful" POST buttons
  - For authenticated players: a collapsible "Flag" form with reason select and submit

- A submit-evidence form below the list, shown only to authenticated players. Fields:
  - URL (required, `type="url"`)
  - Note (required, `<textarea>`)
  - Criterion (optional `<select>` over active criteria, with a blank/none option)

Anonymous visitors see a "Log in to submit evidence or vote" invitation instead of
both forms.

The template extends `base.html` and uses standard Django form POSTs with CSRF tokens.
No JavaScript is required for the core submit flow. Datastar enhancements (inline vote
updates, form clearing after submit) can be layered on in a follow-up.

---

### §5 — `evidence/tests.py` — replace empty stub

Remove `from django.test import TestCase` and write pytest tests covering:

**Service tests:**

| Test | What it covers |
|---|---|
| `test_submit_evidence_creates_record` | Evidence created with correct subject, url, note, submitter |
| `test_submit_evidence_no_criterion` | criterion=None is valid |
| `test_submit_evidence_with_criterion` | criterion FK stored correctly |
| `test_vote_useful_creates_vote` | EvidenceUsefulness created, score updated |
| `test_vote_not_useful_creates_vote` | is_useful=False decrements score |
| `test_vote_changes_existing_vote` | update_or_create: changing from useful to not-useful |
| `test_recompute_score_reflects_votes` | net score = useful_count - not_useful_count |
| `test_flag_evidence_creates_flag` | mature player flags evidence |
| `test_flag_evidence_not_mature_age` | immature by age → NotMatureError |
| `test_flag_evidence_not_mature_surveys` | immature by survey count → NotMatureError |
| `test_flag_evidence_already_flagged` | second flag → AlreadyFlaggedError |
| `test_flag_triggers_hide_when_threshold_met` | flag_count ≥ threshold → status=hidden |
| `test_flag_does_not_hide_high_usefulness` | high score raises threshold, single flag insufficient |

**View tests:**

| Test | What it covers |
|---|---|
| `test_candidate_profile_returns_200` | Anonymous GET of profile page |
| `test_candidate_profile_shows_evidence` | Visible evidence appears |
| `test_candidate_profile_hides_non_visible` | Hidden/removed evidence not shown |
| `test_candidate_profile_blacklist_notice` | Blacklisted candidate shows notice |
| `test_evidence_submit_requires_login` | Unauthenticated POST → 302 to login |
| `test_evidence_submit_creates_record` | Authenticated POST → Evidence created |
| `test_evidence_vote_requires_login` | Unauthenticated POST → 302 to login |
| `test_evidence_flag_requires_login` | Unauthenticated POST → 302 to login |
| `test_evidence_flag_not_mature_shows_message` | Immature player → error message |

All view tests use the Django test `client`. The `player` fixture from `conftest.py`
is already `email_verified=True`; a `mature_player` fixture already exists.

---

## What This Plan Does Not Include

- Datastar inline vote/submit enhancements (layered on later)
- Survey responses on the candidate profile (requires visibility toggle — not yet built)
- Spendium evidence (out of scope per user instruction)
- No new migrations — the existing evidence models are complete and correct

---

## File Summary

| File | Change |
|---|---|
| `evidence/service.py` | Add `NotMatureError`, `AlreadyFlaggedError`, `submit_evidence()`, `vote_usefulness()`, `flag_evidence()` |
| `polium/views.py` | Implement `candidate_detail()`; add `evidence_submit()`, `evidence_vote()`, `evidence_flag()` |
| `polium/urls.py` | Add three evidence action URL patterns |
| `templates/polium/candidate_profile.html` | New — candidate profile with evidence section |
| `evidence/tests.py` | Replace stub with 13 service tests + 9 view tests |

---

## Sequencing

1. ✅ **Service** — `submit_evidence()`, `vote_usefulness()`, `flag_evidence()` + exceptions
2. ✅ **Views + URLs** — `candidate_detail()` and the three action views; wire URLs
3. ✅ **Template** — candidate profile with header, blacklist notice, evidence list, submit form
4. ✅ **Tests** — service first, then views
