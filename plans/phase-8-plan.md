# Phase 8 Todo — Polium App

Phase 8 produces the Polium game's full data layer: eight models covering jurisdictions, elections, candidates, office history, blacklist history, vote declarations, and the two supporting join tables. It also adds the blacklist-engine task view and wires it into the task URL registry.

---

#### 8.1 — Models (polium/models.py)

- [x] Replace the auto-generated placeholder in `polium/models.py`
- [x] Add imports at the top of the file:
  - `from decimal import Decimal`
  - `from django.conf import settings`
  - `from django.db import models`
  - `from sqids import Sqids`
  - `from core.models import SqidMixin`
  - `from lifecycle.models import LifecycleMixin`
  - Do **not** import `Player` directly — all player FKs use `settings.AUTH_USER_MODEL` (consistent with every prior phase)

##### Jurisdiction

- [x] Define `Jurisdiction(SqidMixin, LifecycleMixin)`:
  - No explicit `models.Model` in the base list — both mixins are abstract and already inherit from it; Python MRO resolves correctly
  - `name = models.CharField(max_length=300)`
  - `level = models.CharField(max_length=100)` — e.g. "federal", "state", "county", "city"
  - `parent = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="children")`
  - `created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="jurisdictions_created")`
  - `created_at = models.DateTimeField(auto_now_add=True)`
  - `generate_sqid(self) -> str` — `Sqids(alphabet=settings.SQID_SALTS["jurisdiction"]).encode([self.pk])`
  - `flag_count` as a `@property` returning `self.duplicate_flags.count()` — satisfies `LifecycleMixin`'s `NotImplementedError`
  - `_winning_jurisdiction(self)` — returns `flag.points_to` from `self.duplicate_flags.order_by("-created_at").first()`, or `None` if no flags
  - `delete(self, *args, **kwargs) -> None`:
    - `winning = self._winning_jurisdiction()`
    - If `winning`: call `self.children.all().update(parent=winning)` to re-parent child jurisdictions; then iterate `self.followers.all()` and for each `follow`: set `follow.jurisdiction = winning` and call `follow.save()`
    - Call `super().delete(*args, **kwargs)` — always, regardless of whether a winning jurisdiction was found
  - `class Meta`:
    - `indexes = [models.Index(fields=["status", "active_engagement"]), models.Index(fields=["parent", "status"])]`
  - `__str__` returning `f"{self.name} ({self.level})"`

##### JurisdictionDuplicateFlag

- [x] Define `JurisdictionDuplicateFlag(models.Model)` below `Jurisdiction`:
  - `flagging_player = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="jurisdiction_flags")`
  - `flagged_jurisdiction = models.ForeignKey(Jurisdiction, on_delete=models.CASCADE, related_name="duplicate_flags")`
  - `points_to = models.ForeignKey(Jurisdiction, on_delete=models.CASCADE, related_name="flagged_as_duplicate_of")` — the jurisdiction this one is considered a duplicate of
  - `created_at = models.DateTimeField(auto_now_add=True)`
  - `class Meta: unique_together = [["flagging_player", "flagged_jurisdiction"]]` — one flag per player per jurisdiction

##### JurisdictionFollow

- [x] Define `JurisdictionFollow(models.Model)` below `JurisdictionDuplicateFlag`:
  - Two class-level depth constants: `DEPTH_THIS = "this"`, `DEPTH_ALL = "all"`, and `DEPTH_CHOICES` built from them
  - `player = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="followed_jurisdictions")`
  - `jurisdiction = models.ForeignKey(Jurisdiction, on_delete=models.CASCADE, related_name="followers")`
  - `depth = models.CharField(max_length=10, choices=DEPTH_CHOICES, default=DEPTH_ALL)`
  - `created_at = models.DateTimeField(auto_now_add=True)`
  - `class Meta: unique_together = [["player", "jurisdiction"]]`

##### Election

- [x] Define `Election(SqidMixin)` below `JurisdictionFollow`:
  - `name = models.CharField(max_length=300)`
  - `jurisdiction = models.ForeignKey(Jurisdiction, on_delete=models.SET_NULL, null=True, related_name="elections")`
  - `election_date = models.DateField()`
  - `created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="elections_created")`
  - `external_reference = models.URLField(blank=True)`
  - `created_at = models.DateTimeField(auto_now_add=True)`
  - `generate_sqid(self) -> str` — `Sqids(alphabet=settings.SQID_SALTS["election"]).encode([self.pk])`
  - `class Meta: indexes = [models.Index(fields=["jurisdiction", "election_date"])]`
  - `__str__` returning `self.name`

##### Candidate

- [x] Define `Candidate(SqidMixin)` below `Election`:
  - `name = models.CharField(max_length=300)`
  - `jurisdiction = models.ForeignKey(Jurisdiction, on_delete=models.SET_NULL, null=True, related_name="candidates")`
  - `office = models.CharField(max_length=200)`
  - `election = models.ForeignKey(Election, on_delete=models.SET_NULL, null=True, blank=True, related_name="candidates")`
  - `created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="candidates_created")`
  - `external_reference = models.URLField(blank=True)`
  - `bio = models.TextField(blank=True)`
  - `current_rating = models.DecimalField(max_digits=5, decimal_places=2, default=0)` — denormalised; updated only by `update_candidate_rating` task
  - `is_blacklisted = models.BooleanField(default=False)`
  - `blacklisted_at = models.DateTimeField(null=True, blank=True)`
  - `engagement_count = models.PositiveIntegerField(default=0)` — kept current by callers, not by this model
  - `created_at = models.DateTimeField(auto_now_add=True)`
  - `duplicates = models.ManyToManyField("self", blank=True, symmetrical=True)` — symmetrical so linking A→B also links B→A
  - `generate_sqid(self) -> str` — `Sqids(alphabet=settings.SQID_SALTS["candidate"]).encode([self.pk])`
  - `class Meta`:
    - `indexes = [models.Index(fields=["jurisdiction", "current_rating"]), models.Index(fields=["engagement_count"]), models.Index(fields=["is_blacklisted"])]`
  - `__str__` returning `self.name`

##### OfficeHistory

- [x] Define `OfficeHistory(models.Model)` below `Candidate`:
  - `candidate = models.ForeignKey(Candidate, on_delete=models.CASCADE, related_name="office_history")`
  - `office = models.CharField(max_length=300)`
  - `jurisdiction = models.CharField(max_length=300)` — plain string, not a FK; historical record that must survive jurisdiction deletion
  - `started_at = models.DateField()`
  - `ended_at = models.DateField(null=True, blank=True)`
  - `added_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="office_history_entries")`
  - `created_at = models.DateTimeField(auto_now_add=True)`
  - `class Meta: ordering = ["-started_at"]; verbose_name_plural = "office histories"`

##### BlacklistHistory

- [x] Define `BlacklistHistory(models.Model)` below `OfficeHistory`:
  - `candidate = models.ForeignKey(Candidate, on_delete=models.CASCADE, related_name="blacklist_history")`
  - `blacklisted_at = models.DateTimeField()`
  - `lifted_at = models.DateTimeField(null=True, blank=True)`
  - `rating_at_blacklist = models.DecimalField(max_digits=5, decimal_places=2)`
  - `rating_at_lift = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)`
  - No `__str__` or Meta required beyond defaults — this model is immutable audit data

##### VoteDeclaration

- [x] Define `VoteDeclaration(models.Model)` below `BlacklistHistory`:
  - `player = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="vote_declarations")`
  - `candidate = models.ForeignKey(Candidate, on_delete=models.CASCADE, related_name="vote_declarations")`
  - `election = models.ForeignKey(Election, on_delete=models.CASCADE, related_name="vote_declarations")`
  - `shared_on_social = models.BooleanField(default=False)`
  - `shared_at = models.DateTimeField(null=True, blank=True)`
  - `declared_at = models.DateTimeField(auto_now_add=True)`
  - `class Meta: unique_together = [["player", "election"]]` — one declaration per player per election

---

#### 8.2 — Task view (polium/task_views.py)

- [x] Create `polium/task_views.py`
- [x] Add module-level imports:
  - `from decimal import Decimal`
  - `from django.utils import timezone`
  - `from core.tasks import task`
  - `from surveys.ratings import compute_rating`
- [x] Define two module-level constants:
  - `BLACKLIST_ENTRY = Decimal("0.25")` — rating below which a candidate is blacklisted
  - `BLACKLIST_EXIT = Decimal("0.50")` — rating at or above which the blacklist is lifted
- [x] Decorate `update_candidate_rating(candidate_id: int) -> None` with `@task("update-candidate-rating")`:
  - Import `Candidate` and `BlacklistHistory` from `.models` **inside the function body** — consistent with the lazy-import convention established in `lifecycle/task_views.py`
  - Fetch: `candidate = Candidate.objects.select_for_update().get(pk=candidate_id)` — row lock prevents concurrent rating updates from racing
  - `rating = compute_rating(candidate)` — returns `float | None`
  - If `rating is None`: return early; no DB write
  - `new_rating = Decimal(str(round(rating, 2)))`
  - `Candidate.objects.filter(pk=candidate_id).update(current_rating=new_rating)` — use `.update()` not `.save()`
  - **Apply blacklist**: if `not candidate.is_blacklisted and new_rating < BLACKLIST_ENTRY`:
    - `now = timezone.now()`
    - `Candidate.objects.filter(pk=candidate_id).update(is_blacklisted=True, blacklisted_at=now)`
    - `BlacklistHistory.objects.create(candidate=candidate, blacklisted_at=now, rating_at_blacklist=new_rating)`
  - **Lift blacklist**: elif `candidate.is_blacklisted and new_rating >= BLACKLIST_EXIT`:
    - `now = timezone.now()`
    - `Candidate.objects.filter(pk=candidate_id).update(is_blacklisted=False, blacklisted_at=None)`
    - `BlacklistHistory.objects.filter(candidate=candidate, lifted_at__isnull=True).update(lifted_at=now, rating_at_lift=new_rating)`

---

#### 8.3 — Wire into hf/task_urls.py

- [x] Add `import polium.task_views as polium_task_views` to `hf/task_urls.py` (after the existing lifecycle import, alphabetically by app name is fine)
- [x] Add `path("update-candidate-rating/", polium_task_views.update_candidate_rating, name="task_update_candidate_rating")` to `urlpatterns`
- [x] Confirm `uv run python manage.py check` is still clean after the import is added

---

#### 8.4 — Admin (polium/admin.py)

- [x] Replace the auto-generated placeholder in `polium/admin.py`
- [x] Define `JurisdictionDuplicateFlagInline(admin.TabularInline)` for `JurisdictionDuplicateFlag`
- [x] Define `JurisdictionFollowInline(admin.TabularInline)` for `JurisdictionFollow`
- [x] Register `Jurisdiction` with a `ModelAdmin`:
  - `list_display = ("name", "level", "parent", "status", "active_engagement", "created_at")`
  - `list_filter = ("status", "level")`
  - `readonly_fields = ("sqid", "status", "active_engagement", "deprecated_at", "created_at")`
  - `inlines = [JurisdictionDuplicateFlagInline, JurisdictionFollowInline]`
- [x] Define `OfficeHistoryInline(admin.TabularInline)` for `OfficeHistory`
- [x] Define `BlacklistHistoryInline(admin.TabularInline)` for `BlacklistHistory` with `extra = 0` and `can_delete = False`
- [x] Define `VoteDeclarationInline(admin.TabularInline)` for `VoteDeclaration`
- [x] Register `Candidate` with a `ModelAdmin`:
  - `list_display = ("name", "office", "jurisdiction", "election", "current_rating", "is_blacklisted", "engagement_count", "created_at")`
  - `list_filter = ("is_blacklisted", "jurisdiction")`
  - `readonly_fields = ("sqid", "current_rating", "is_blacklisted", "blacklisted_at", "engagement_count", "created_at")`
  - `inlines = [OfficeHistoryInline, BlacklistHistoryInline, VoteDeclarationInline]`
- [x] Register `Election` with a `ModelAdmin`:
  - `list_display = ("name", "jurisdiction", "election_date", "created_at")`
  - `readonly_fields = ("sqid", "created_at")`

---

#### 8.5 — Migration

- [x] Run `uv run python manage.py makemigrations polium`
  - Expect tables: `polium_jurisdiction`, `polium_jurisdictionduplicateflag`, `polium_jurisdictionfollow`, `polium_election`, `polium_candidate`, `polium_candidate_duplicates` (M2M through table), `polium_officehistory`, `polium_blacklisthistory`, `polium_votedeclaration`
  - Confirm the two `Jurisdiction` indexes (`status/active_engagement` and `parent/status`) are present in the migration
  - Confirm the three `Candidate` indexes (`jurisdiction/current_rating`, `engagement_count`, `is_blacklisted`) are present
- [x] Run `uv run python manage.py check` — must be clean

---

#### Phase 8 complete when
- [x] `polium/models.py` defines all eight models; all player FKs use `settings.AUTH_USER_MODEL`; `Jurisdiction.flag_count` satisfies `LifecycleMixin`; `Jurisdiction.delete()` migrates children and followers before calling `super()`
- [x] `polium/task_views.py` defines `update_candidate_rating` decorated with `@task`, using `select_for_update`, lazy model imports, `.update()` for all writes, and correctly branching on both blacklist entry and exit
- [x] `hf/task_urls.py` imports `polium.task_views` and exposes `update-candidate-rating/` as a named URL pattern
- [x] `polium/admin.py` registers `Jurisdiction` (with duplicate-flag and follow inlines), `Candidate` (with office-history, blacklist-history, and vote-declaration inlines), and `Election`
- [x] `polium/migrations/0001_initial.py` exists with all expected tables and indexes
- [x] `uv run python manage.py check` → `System check identified no issues`
