# Phase 4 Todo — Surveys App

Phase 4 produces the survey engine — the shared infrastructure that all three games build on. It comprises four models in `surveys/models.py`, a standalone rating calculator in `surveys/ratings.py`, admin registrations, and the migration.

---

#### 4.1 — Models (surveys/models.py)

- [x] Replace the auto-generated placeholder in `surveys/models.py`
- [x] Define `Category(models.Model)`:
  - `name = models.CharField(max_length=200)`
  - `description = models.TextField()`
  - `game = models.CharField(max_length=50)` — one of `"polium"`, `"spendium"`, `"humanium"`; no `choices` constraint at the model level, values are enforced by application logic
  - `class Meta: verbose_name_plural = "categories"`
  - `__str__` returns `f"{self.game} / {self.name}"`

- [x] Define `Criterion(models.Model)`:
  - `category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="criteria")` — `PROTECT` prevents deleting a category that still has active criteria
  - `question = models.TextField()`
  - `weight = models.DecimalField(max_digits=5, decimal_places=2, default=1.0)`
  - `is_active = models.BooleanField(default=True)`
  - `created_at = models.DateTimeField(auto_now_add=True)`
  - `__str__` returns `self.question[:80]`

- [x] Define `SurveyResponse(models.Model)`:
  - `player` FK: use `settings.AUTH_USER_MODEL` (the string `"accounts.Player"`) — **not** a direct import of `Player`. The plan.md uses a direct import, but cross-app FKs to the user model must reference `settings.AUTH_USER_MODEL` to remain swappable and avoid import-order issues. Set `on_delete=models.SET_NULL, null=True, related_name="survey_responses"`. The `related_name="survey_responses"` is load-bearing — `core/maturity.py` calls `player.survey_responses.count()`.
  - `content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)` — import `ContentType` from `django.contrib.contenttypes.models`
  - `object_id = models.PositiveIntegerField()`
  - `subject = GenericForeignKey("content_type", "object_id")` — import `GenericForeignKey` from `django.contrib.contenttypes.fields`. This field is not a real database column and does not appear in migrations.
  - `submitted_at = models.DateTimeField(auto_now_add=True)`
  - `class Meta: indexes = [models.Index(fields=["content_type", "object_id"])]`
  - No `__str__` required; add a minimal one if desired for admin legibility

- [x] Define `CriterionAnswer(models.Model)`:
  - `survey_response = models.ForeignKey(SurveyResponse, on_delete=models.CASCADE, related_name="answers")`
  - `criterion = models.ForeignKey(Criterion, on_delete=models.PROTECT, related_name="answers")` — `PROTECT` prevents deleting a criterion that has recorded answers
  - `answer = models.BooleanField()` — `True` = yes, `False` = no; no default, must be explicitly set on every answer
  - No extra indexes needed — queries always go through `survey_response`

---

#### 4.2 — Rating calculator (surveys/ratings.py)

- [x] Create `surveys/ratings.py`
- [x] Write `compute_rating(subject: Model) -> float | None`:
  - Import `Model` from `django.db.models` for the type annotation on `subject`
  - Rolling 12-month window: `cutoff = timezone.now() - timedelta(days=365)`
  - Resolve the ContentType: `ct = ContentType.objects.get_for_model(subject)`
  - Query `SurveyResponse` filtered by `content_type=ct`, `object_id=subject.pk`, `submitted_at__gte=cutoff`
  - Return `None` immediately if no responses exist (`.exists()` check before the answer query)
  - Query `CriterionAnswer` for those responses where `criterion__is_active=True`, using `.select_related("criterion")` to avoid N+1 on weight access
  - Accumulate `total_weight` and `weighted_sum` in a loop — `1.0` for a `True` answer, `0.0` for `False`, multiplied by `float(answer.criterion.weight)`
  - Return `None` if `total_weight == 0` (all active criteria have zero weight — degenerate case)
  - Return `weighted_sum / total_weight` — a `float` between `0.0` and `1.0`

---

#### 4.3 — Admin (surveys/admin.py)

- [x] Replace the auto-generated placeholder in `surveys/admin.py`
- [x] Register `Criterion` as a `TabularInline` on `Category` — makes it natural to add criteria when creating or editing a category
- [x] Register `Category` with a `ModelAdmin` that includes the `CriterionInline`; add `list_display = ("name", "game")` and `list_filter = ("game",)`
- [x] Register `Criterion` with a standalone `ModelAdmin`; add `list_display = ("question", "category", "weight", "is_active")` and `list_filter = ("is_active", "category__game")`
- [x] Register `CriterionAnswer` as a `TabularInline` on `SurveyResponse`
- [x] Register `SurveyResponse` with a `ModelAdmin` that includes the `CriterionAnswerInline`; add `list_display = ("player", "content_type", "object_id", "submitted_at")`

---

#### 4.4 — Migration

- [x] Run `uv run python manage.py makemigrations surveys`
  - Expect three new tables: `surveys_category`, `surveys_criterion`, `surveys_surveyresponse`, `surveys_criterionanswer`
  - `subject` (GenericForeignKey) does not appear as a column — confirm it is absent from the migration
- [x] Run `uv run python manage.py check` — must be clean

---

#### Phase 4 complete when
- [x] `surveys/models.py` defines `Category`, `Criterion`, `SurveyResponse` (with GenericForeignKey), and `CriterionAnswer`
- [x] `SurveyResponse.player` FK uses `settings.AUTH_USER_MODEL`, not a direct import
- [x] `surveys/ratings.py` defines `compute_rating(subject: Model) -> float | None` with the 12-month rolling window and weighted average logic
- [x] `surveys/admin.py` registers all four models with appropriate inlines
- [x] `surveys/migrations/0001_initial.py` exists and was generated without errors
- [x] `uv run python manage.py check` → `System check identified no issues`
