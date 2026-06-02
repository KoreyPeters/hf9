# Phase 6 Todo — Lifecycle App

Phase 6 produces the shared deprecation/deletion infrastructure: the `LifecycleMixin` abstract model and the two Cloud Tasks/Cloud Scheduler handlers that run the maintenance loop. It also wires those handlers into `hf/task_urls.py` so they are reachable as HTTP endpoints in production.

---

#### 6.1 — LifecycleMixin (lifecycle/models.py)

- [x] Replace the auto-generated placeholder in `lifecycle/models.py`
- [x] Define `LifecycleMixin(models.Model)` with:
  - Three class-level status constants: `STATUS_ACTIVE = "active"`, `STATUS_DEPRECATED = "deprecated"`, `STATUS_DELETED = "deleted"`
  - `STATUS_CHOICES` list built from those constants
  - `status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE)`
  - `active_engagement = models.PositiveIntegerField(default=0)` — concrete subclasses are responsible for keeping this count current; `LifecycleMixin` does not update it
  - `deprecated_at = models.DateTimeField(null=True, blank=True)`
  - `flag_count` as a `@property` that raises `NotImplementedError` — return type annotation is `int`. Not `@abstractmethod` for the same reason as `SqidMixin.generate_sqid()` — Django's model metaclass does not interact cleanly with ABC.
  - `should_deprecate(self) -> bool` — reads `settings.LIFECYCLE["DEPRECATION_RATIO"]`; when `active_engagement == 0` returns `self.flag_count > 0`; otherwise returns `self.flag_count >= self.active_engagement / ratio`
  - `class Meta: abstract = True`
- [x] Confirm `uv run python manage.py makemigrations lifecycle` → `No changes detected`
- [x] Confirm `uv run python manage.py check` is clean

---

#### 6.2 — Task views (lifecycle/task_views.py)

- [x] Create `lifecycle/task_views.py`
- [x] Decorate `check_deprecations() -> None` with `@task("check-deprecations")`:
  - Import `Jurisdiction` from `polium.models` **inside the function body**, not at module level — `lifecycle` is foundational and must not import from game-specific apps at import time; the lazy import breaks the potential circular dependency
  - Filter `Jurisdiction.objects.filter(status=LifecycleMixin.STATUS_ACTIVE)` — import `LifecycleMixin` from `.models` at the top of the file (same package, no circular risk)
  - For each object where `obj.should_deprecate()` is `True`, update in place: `Jurisdiction.objects.filter(pk=obj.pk).update(status=LifecycleMixin.STATUS_DEPRECATED, deprecated_at=timezone.now())` — use `.update()` not `.save()` to avoid triggering unrelated model logic
- [x] Decorate `check_deletions() -> None` with `@task("check-deletions")`:
  - Import `Jurisdiction` from `polium.models` inside the function body (same reason)
  - Compute `threshold = timezone.now() - timedelta(days=settings.LIFECYCLE["DELETION_DAYS"])`
  - Filter `Jurisdiction.objects.filter(status=LifecycleMixin.STATUS_DEPRECATED, active_engagement=0, deprecated_at__lte=threshold)`
  - Call `obj.delete()` on each — the concrete model's `delete()` override handles child migration and follower notification (implemented in Phase 8)
- [x] Both handlers return `None` — the `@task` decorator wraps them as views; the handler functions themselves just perform DB work and return nothing

---

#### 6.3 — Wire into hf/task_urls.py

- [x] Replace the current empty `urlpatterns` stub in `hf/task_urls.py` with the lifecycle task routes:
  - Import `lifecycle.task_views` — this import is what registers the `@task`-decorated functions in `core.tasks._registry`, making `enqueue()` able to call them in dev. The import must happen at module load time.
  - Add `path("check-deprecations/", lifecycle_task_views.check_deprecations, name="task_check_deprecations")`
  - Add `path("check-deletions/", lifecycle_task_views.check_deletions, name="task_check_deletions")`
  - Use an aliased import (`import lifecycle.task_views as lifecycle_task_views`) to keep the reference unambiguous — `task_urls.py` will accumulate imports from multiple apps over later phases
- [x] Confirm `uv run python manage.py check` remains clean after wiring

---

#### Phase 6 complete when
- [x] `lifecycle/models.py` defines `LifecycleMixin` as an abstract model with `status`, `active_engagement`, `deprecated_at`, `flag_count` property, and `should_deprecate()`
- [x] `lifecycle/task_views.py` defines `check_deprecations` and `check_deletions`, both decorated with `@task`, both using lazy imports of `Jurisdiction`
- [x] `hf/task_urls.py` imports `lifecycle.task_views` and exposes both handlers as named URL patterns under `tasks/`
- [x] `uv run python manage.py makemigrations lifecycle` → `No changes detected`
- [x] `uv run python manage.py check` → `System check identified no issues`
