# Phase 7 Todo — Evidence App

Phase 7 produces the evidence system — community-submitted links attached to any HF entity via a GenericForeignKey, with usefulness voting and flag-based hiding. Three models, two service functions, admin registrations, and the migration.

---

#### 7.1 — Evidence model (evidence/models.py)

- [x] Replace the auto-generated placeholder in `evidence/models.py`
- [x] Define `Evidence(models.Model)`:
  - Three class-level status constants: `STATUS_VISIBLE = "visible"`, `STATUS_HIDDEN = "hidden"`, `STATUS_REMOVED = "removed"`, and `STATUS_CHOICES` built from them
  - `content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)` — import `ContentType` from `django.contrib.contenttypes.models`
  - `object_id = models.PositiveIntegerField()`
  - `subject = GenericForeignKey("content_type", "object_id")` — import `GenericForeignKey` from `django.contrib.contenttypes.fields`; not a DB column
  - `submitted_by`: FK to `settings.AUTH_USER_MODEL` — **not** a direct `Player` import; `on_delete=models.SET_NULL, null=True, related_name="evidence_submitted"`
  - `url = models.URLField()`
  - `note = models.TextField()`
  - `criterion`: FK to `surveys.Criterion` — import `Criterion` from `surveys.models` (no circular risk; surveys does not import from evidence); `on_delete=models.SET_NULL, null=True, blank=True, related_name="evidence"`. Optional — a piece of evidence doesn't have to link to a specific criterion.
  - `status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_VISIBLE)`
  - `net_usefulness_score = models.IntegerField(default=0)` — denormalised; updated by `recompute_usefulness_score()`, never written directly
  - `submitted_at = models.DateTimeField(auto_now_add=True)`
  - `class Meta: indexes = [models.Index(fields=["content_type", "object_id", "status", "net_usefulness_score"])]` — the four-column index supports the primary query pattern: all visible evidence for a subject, sorted by score descending
  - `__str__` returning something minimal, e.g. `f"{self.url} ({self.status})"`

#### 7.2 — EvidenceUsefulness model (evidence/models.py)

- [x] Define `EvidenceUsefulness(models.Model)` below `Evidence` in the same file:
  - `player`: FK to `settings.AUTH_USER_MODEL`; `on_delete=models.CASCADE, related_name="evidence_usefulness_votes"`
  - `evidence = models.ForeignKey(Evidence, on_delete=models.CASCADE, related_name="usefulness_votes")`
  - `is_useful = models.BooleanField()` — no default; every vote must state a position explicitly
  - `created_at = models.DateTimeField(auto_now_add=True)`
  - `class Meta: unique_together = [["player", "evidence"]]` — one vote per player per evidence item

#### 7.3 — EvidenceFlag model (evidence/models.py)

- [x] Define `EvidenceFlag(models.Model)` below `EvidenceUsefulness` in the same file:
  - Four class-level reason constants and `REASON_CHOICES`: `"irrelevant"`, `"low_quality"`, `"misleading"`, `"malicious"`
  - `flagging_player`: FK to `settings.AUTH_USER_MODEL`; `on_delete=models.CASCADE, related_name="evidence_flags"`
  - `evidence = models.ForeignKey(Evidence, on_delete=models.CASCADE, related_name="flags")`
  - `reason = models.CharField(max_length=20, choices=REASON_CHOICES)`
  - `created_at = models.DateTimeField(auto_now_add=True)`
  - `class Meta: unique_together = [["flagging_player", "evidence"]]` — one flag per player per evidence item

---

#### 7.4 — Service functions (evidence/service.py)

- [x] Create `evidence/service.py`
- [x] Write `recompute_evidence_status(evidence: Evidence) -> None`:
  - `flag_count = evidence.flags.count()`
  - `threshold = max(1, evidence.net_usefulness_score / 10)` — `max(1, ...)` ensures at least one flag is always required to hide, even when `net_usefulness_score` is 0 or negative. Division produces a `float`; the `>=` comparison with the integer `flag_count` is intentional and correct.
  - If `flag_count >= threshold` and `evidence.status == Evidence.STATUS_VISIBLE`: call `Evidence.objects.filter(pk=evidence.pk).update(status=Evidence.STATUS_HIDDEN)` — use `.update()` not `.save()`
  - Do nothing if the evidence is already hidden or removed — the condition on `STATUS_VISIBLE` handles this
- [x] Write `recompute_usefulness_score(evidence: Evidence) -> None`:
  - `useful = evidence.usefulness_votes.filter(is_useful=True).count()`
  - `not_useful = evidence.usefulness_votes.filter(is_useful=False).count()`
  - `Evidence.objects.filter(pk=evidence.pk).update(net_usefulness_score=useful - not_useful)` — use `.update()` not `.save()`

---

#### 7.5 — Admin (evidence/admin.py)

- [x] Replace the auto-generated placeholder in `evidence/admin.py`
- [x] Register `EvidenceUsefulness` as a `TabularInline` on `Evidence`
- [x] Register `EvidenceFlag` as a `TabularInline` on `Evidence`
- [x] Register `Evidence` with a `ModelAdmin`:
  - `list_display = ("url", "submitted_by", "content_type", "object_id", "status", "net_usefulness_score", "submitted_at")`
  - `list_filter = ("status",)`
  - `readonly_fields = ("net_usefulness_score", "submitted_at")` — score is managed by the service, not edited directly
  - Inlines: `[EvidenceUsefulnessInline, EvidenceFlagInline]`

---

#### 7.6 — Migration

- [x] Run `uv run python manage.py makemigrations evidence`
  - Expect three new tables: `evidence_evidence`, `evidence_evidenceusefulness`, `evidence_evidenceflag`
  - `subject` (GenericForeignKey) must not appear in the migration — confirm it is absent
  - Confirm the four-column index on `Evidence` is present in the migration
- [x] Run `uv run python manage.py check` — must be clean

---

#### Phase 7 complete when
- [x] `evidence/models.py` defines `Evidence` (with GenericForeignKey and four-column index), `EvidenceUsefulness`, and `EvidenceFlag`, all player FKs using `settings.AUTH_USER_MODEL`
- [x] `evidence/service.py` defines `recompute_evidence_status` and `recompute_usefulness_score`, both using `.update()` and both returning `None`
- [x] `evidence/admin.py` registers all three models with `Evidence` showing both inlines
- [x] `evidence/migrations/0001_initial.py` exists, `subject` is absent, four-column index is present
- [x] `uv run python manage.py check` → `System check identified no issues`
