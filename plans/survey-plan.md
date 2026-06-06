# Survey Experience Plan

## What Already Exists

The entire data and logic layer is done — no new models or migrations needed.

| Layer | File | Status |
|---|---|---|
| Data model | `surveys/models.py` | Done — `Category`, `Criterion`, `SurveyResponse`, `CriterionAnswer`, `SurveyConfig` |
| Submit + points | `surveys/service.py` | Done — `submit_survey()`, `check_cooldown()`, `award_points()` |
| Rating calc | `surveys/ratings.py` | Done — `compute_rating()` — weighted avg over last 12 months |
| Rating update task | `polium/task_views.py` | Done — `update_candidate_rating` — called via `enqueue()` |
| Survey URL | `polium/urls.py` | Done — `candidates/<sqid>/survey/` → `views.submit_survey` |
| Survey config | `SurveyConfig` (db row) | Done — `cooldown_days=30`, points `100/50/25` |
| Tests | `surveys/tests.py` | Done — 18 tests cover service + rating + points + cooldown |
| Seed criteria | `seed_criteria` command | Done — 2 polium criteria in "Climate and Environment" |

**What the stub view currently returns:** `HttpResponse("TODO")` — lines 803–804 of `polium/views.py`.

---

## Design Decisions

**Scope of this plan — candidate only.** The service layer is already generic (`subject: Model`). When Spendium needs surveys, it adds its own view and partial following the same pattern; nothing here changes.

**Rating storage scale — 0.0–1.0.** `compute_rating()` returns a float in [0, 1]. `update_candidate_rating` stores it directly as `Decimal("0.75")`. Two existing places display this incorrectly:
- `candidate_profile.html` line 18 shows `{{ candidate.current_rating }}%` → renders "0.75%" instead of "75%"
- `_points_preview()` divides by 100 again (`current_rating / 100`) making points negligibly small

Both are pre-existing bugs. The survey section partial will display the rating correctly (`current_rating × 100`). The `_points_preview` bug is out of scope for this plan.

**Datastar SSE pattern** — consistent with election declare and jurisdiction follow sections. The survey form uses `data-on:submit="@post(url, {contentType: 'form'})"`. The POST view returns a `DatastarResponse` patching `#survey-section`. This gives instant in-page feedback (points awarded, updated rating) without a page reload.

**Form data, not signals** — criteria answers (`criterion_{pk} = yes|no`) sent as HTML form fields with `contentType: 'form'`. Consistent with `evidence_submit` and the `spendium:notify` form pattern in the landing page.

**State machine** for the survey section — rendered server-side on initial page load, replaced by SSE on submission:
- `anonymous` — not logged in → "Log in to submit a survey"
- `ready` — can submit (first-time or cooldown passed) → form
- `in_cooldown` — submitted within cooldown window → message with days remaining + current answers
- `submitted` — just submitted this request → confirmation with points earned + updated rating

**Rating refresh** — after `submit_survey()` succeeds, call `core.tasks.enqueue("update-candidate-rating", {"candidate_id": candidate.pk})`. In `DEBUG` mode this runs synchronously, so the rating is immediately fresh on the confirmation render. // How will this handle a heavily-surveyed item? Like, dozens if not hundreds of surveys every minute?

**Criteria grouping** — grouped by `Category.name`, filtered to `category__game="polium"` and `is_active=True`. Each criterion is a yes/no choice — two clearly labelled buttons per question. Grouping by category provides context for the player.

**Validation** — if the player submits with no answers (all criteria skipped), require at least one answer. If criteria have been deleted between page load and submit, ignore unknown criterion PKs silently.

**Unanswered criteria** — a player does not have to answer every criterion. `submit_survey()` only stores the answers provided. `compute_rating()` only scores the criteria that have answers — a partial response is valid and contributes proportionally.

**Existing answers in cooldown state** — when in cooldown, show the player's current answers (as read-only) so they can see what they submitted. This is context, not re-submission.

---

## Files Changed

| File | Change |
|---|---|
| `polium/views.py` | Add survey state context to `candidate_detail()`; implement `submit_survey()` |
| `templates/polium/candidate_profile.html` | Add `<div id="survey-section">` with included partial |
| `templates/polium/partials/survey_section.html` | New — all 4 states |
| `polium/tests.py` | Add view-level tests for the survey endpoint |

---

## Todo Steps

- [ ] Update `candidate_detail()` to compute and pass survey state context
- [ ] Create `templates/polium/partials/survey_section.html` with all 4 states
- [ ] Add `<div id="survey-section">` to `candidate_profile.html`
- [ ] Implement `submit_survey()` view in `polium/views.py`
- [ ] Add view-level tests to `polium/tests.py`
- [ ] Run the test suite to confirm nothing is broken

---

## Detailed Design

### `candidate_detail()` context additions

```python
from surveys.service import check_cooldown
from surveys.models import Category, CriterionAnswer

def candidate_detail(request, sqid):
    candidate = get_object_or_404(Candidate, sqid=sqid)
    
    # Survey state
    criteria_qs = (
        Criterion.objects
        .filter(is_active=True, category__game="polium")
        .select_related("category")
        .order_by("category__name", "question")
    )
    # Group by category: [(Category, [Criterion, ...]), ...]
    from itertools import groupby
    criteria_by_category = [
        (cat, list(crits))
        for cat, crits in groupby(criteria_qs, key=lambda c: c.category)
    ]

    survey_state = "anonymous"
    cooldown_remaining = None
    existing_answers: dict[int, bool] = {}

    if request.user.is_authenticated:
        cooldown_remaining = check_cooldown(request.user, candidate)
        if cooldown_remaining is not None:
            survey_state = "in_cooldown"
            # Fetch existing answers for display
            ct = ContentType.objects.get_for_model(Candidate)
            latest = (
                SurveyResponse.objects
                .filter(player=request.user, content_type=ct, object_id=candidate.pk)
                .order_by("-submitted_at")
                .first()
            )
            if latest:
                existing_answers = {
                    a.criterion_id: a.answer
                    for a in latest.answers.all()
                }
        else:
            survey_state = "ready"
    
    # rest of existing context...
    return render(request, "polium/candidate_profile.html", {
        ...,
        "survey_state": survey_state,
        "cooldown_remaining": cooldown_remaining,
        "criteria_by_category": criteria_by_category,
        "existing_answers": existing_answers,
    })
```

### `submit_survey()` view

```python
@login_required
@require_POST
def submit_survey(request: HttpRequest, sqid: str) -> DatastarResponse:
    from surveys.service import submit_survey as svc_submit, CoolDownError
    from core.tasks import enqueue

    candidate = get_object_or_404(Candidate, sqid=sqid)

    # Parse yes/no answers from form fields named criterion_<pk>
    answers: dict[int, bool] = {}
    for key, value in request.POST.items():
        if key.startswith("criterion_"):
            try:
                cid = int(key[len("criterion_"):])
                answers[cid] = (value == "yes")
            except ValueError:
                pass

    def _render(state: str, **extra: object) -> DatastarResponse:
        # Recompute criteria_by_category for the partial
        ...
        html = render_to_string("polium/partials/survey_section.html", ctx, request=request)
        return DatastarResponse(ServerSentEventGenerator.patch_elements(html, selector="#survey-section"))

    if not answers:
        return _render("ready", error="Please answer at least one question.")

    try:
        svc_submit(request.user, candidate, answers)
    except CoolDownError as e:
        return _render("in_cooldown", cooldown_remaining=e.remaining, ...)

    enqueue("update-candidate-rating", {"candidate_id": candidate.pk})
    candidate.refresh_from_db()

    return _render("submitted", candidate=candidate, points_awarded=amount)
```

### Template states (`survey_section.html`)

**anonymous**
```
Survey this candidate
Log in to answer questions and earn up to 100 points.
[Sign in] [Sign up]
```

**ready**
```
Survey this candidate
Your answers shape this candidate's rating. First survey earns 100 points.

[Category name]
  Question text?     [Yes]  [No]
  Question text?     [Yes]  [No]

[Submit survey]
```

**in_cooldown**
```
Survey submitted
You can survey again in X days.
  
Your current answers:
  Question text? → Yes / No
```

**submitted** (just POSTed this request)
```
Survey submitted — you earned X points!
[candidate name] is now rated Y%.

You can survey again in 30 days.
```

### Criteria by category — template loop

```django
{% for category, criteria in criteria_by_category %}
<div class="survey-category">
  <h3>{{ category.name }}</h3>
  {% for criterion in criteria %}
  <div class="survey-question">
    <p>{{ criterion.question }}</p>
    <label>
      <input type="radio" name="criterion_{{ criterion.pk }}" value="yes"> Yes
    </label>
    <label>
      <input type="radio" name="criterion_{{ criterion.pk }}" value="no"> No
    </label>
  </div>
  {% endfor %}
</div>
{% endfor %}
```

### View tests to add (`polium/tests.py`)

- Survey endpoint requires login → 302 to login
- Anonymous GET is not allowed (POST only)
- Valid POST with answers → 200, survey section HTML in response
- Valid POST → `compute_rating` is called (via task), `SurveyResponse` created
- Valid POST → points awarded, player total updated
- CoolDown POST → response contains cooldown state HTML, no new SurveyResponse
- Empty answers POST → response contains error, no SurveyResponse created
