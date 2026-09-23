"""Pruning the Litestream replica.

This task deletes backups, so the tests are weighted towards the ways it could
delete the wrong thing rather than towards the happy path. Every guard in
core/replica.py has a test that fails without it.

Background: generations accumulated to 758 by 2026-09-22 and put 40 seconds into
the front of every cold start. See plans/cron-collision-and-boot-regression.md.
"""

from datetime import datetime, timedelta, timezone

import pytest

from core import replica


class FakeBlob:
    def __init__(self, name: str, created: datetime, registry: list) -> None:
        self.name = name
        self.time_created = created
        # Real blobs from `list_blobs` carry this, which is exactly what makes
        # `blob.delete()` a permanent versioned delete rather than an archiving
        # one. Present here so the test below can catch that mistake.
        self.generation = 1790197294196249
        self._registry = registry

    def delete(self) -> None:
        raise AssertionError(
            "blob.delete() permanently destroys this version and bypasses "
            "object versioning. Use bucket.delete_blob(name) instead."
        )


class FakeBucket:
    """Just enough of a GCS bucket to exercise the prune."""

    def __init__(self, blobs: list[FakeBlob]) -> None:
        self._blobs = blobs

    def list_blobs(self, prefix: str = "") -> list[FakeBlob]:
        return [b for b in self._blobs if b.name.startswith(prefix)]

    def delete_blob(self, name: str) -> None:
        """The archiving delete: removes the live version, keeps it noncurrent."""
        for blob in self._blobs:
            if blob.name == name:
                blob._registry.append(name)
                return
        raise FileNotFoundError(name)


def make_bucket(
    count: int,
    *,
    age_hours: int = 24,
    spacing_seconds: int = 3600,
    deleted: list | None = None,
):
    """`count` generations, newest first at `age_hours` old.

    `spacing_seconds` controls how far apart they are, which is what decides
    whether they fall inside `MINIMUM_AGE`. The default spreads them an hour
    apart so they are unambiguously old; the recency test packs them close
    together so the whole set is recent.
    """
    deleted = deleted if deleted is not None else []
    now = datetime.now(timezone.utc)
    blobs = []
    for i in range(count):
        created = now - timedelta(hours=age_hours, seconds=spacing_seconds * i)
        gid = f"gen{i:04d}"
        for leaf in (
            "snapshots/00000000.snapshot.lz4",
            "wal/00000000_00000000.wal.lz4",
        ):
            blobs.append(
                FakeBlob(f"{replica.REPLICA_PREFIX}{gid}/{leaf}", created, deleted)
            )
    return FakeBucket(blobs), deleted


@pytest.fixture
def bucket_of(monkeypatch):
    def _install(count: int, **kwargs):
        bucket, deleted = make_bucket(count, **kwargs)
        monkeypatch.setattr(replica, "_bucket", lambda: bucket)
        return deleted

    return _install


# ── It prunes ─────────────────────────────────────────────────────────────────


def test_it_keeps_exactly_the_requested_number(bucket_of) -> None:
    bucket_of(80)

    result = replica.prune(keep=50)

    assert result.total == 80
    assert result.kept == 50
    assert result.deleted == 30


def test_it_deletes_every_object_in_a_pruned_generation(bucket_of) -> None:
    """A generation is a snapshot *and* its WAL segments. Leaving the WAL behind
    would keep the restore enumerating a generation it can no longer use."""
    deleted = bucket_of(60)

    result = replica.prune(keep=50)

    assert result.blobs_deleted == 20  # 10 generations x 2 objects
    assert len(deleted) == 20
    assert all("gen00" in name for name in deleted)


def test_it_deletes_the_oldest_not_the_newest(bucket_of) -> None:
    """The whole safety property. `make_bucket` numbers newest-first, so
    everything deleted must come from the high-numbered tail."""
    deleted = bucket_of(55)

    replica.prune(keep=50)

    pruned_ids = {name.split("/")[3] for name in deleted}
    assert pruned_ids == {"gen0050", "gen0051", "gen0052", "gen0053", "gen0054"}


def test_it_does_nothing_when_under_the_limit(bucket_of) -> None:
    deleted = bucket_of(10)

    result = replica.prune(keep=50)

    assert result.deleted == 0
    assert deleted == []


# ── It refuses to delete the wrong thing ──────────────────────────────────────


def test_it_never_deletes_a_recent_generation(bucket_of) -> None:
    """Belt and braces over the sort order.

    The newest generation is the one the running container is writing. Keeping
    the newest N already protects it, but this guard means a future change to
    the ordering cannot quietly turn this into a task that deletes a live
    replica. Every generation here is minutes old, so nothing may be deleted
    however far over the limit the count is.
    """
    deleted = bucket_of(200, age_hours=0, spacing_seconds=10)

    result = replica.prune(keep=50)

    assert result.deleted == 0
    assert result.skipped_too_recent == 150
    assert deleted == []


def test_an_empty_replica_is_an_error_not_a_no_op(monkeypatch) -> None:
    """A silent success here would hide exactly the failure this task exists to
    prevent: a wrong prefix, or a replica that is not there at all."""
    monkeypatch.setattr(replica, "_bucket", lambda: FakeBucket([]))

    with pytest.raises(RuntimeError, match="No generations found"):
        replica.prune(keep=50)


def test_it_refuses_to_keep_nothing(bucket_of) -> None:
    """keep=0 would delete the entire replica."""
    bucket_of(80)

    with pytest.raises(ValueError, match="at least 1"):
        replica.prune(keep=0)


def test_it_refuses_to_guess_a_bucket(settings) -> None:
    settings.LITESTREAM_GCS_BUCKET = ""

    with pytest.raises(RuntimeError, match="refusing to guess"):
        replica.prune(keep=50)


def test_it_archives_rather_than_destroying_versions(bucket_of) -> None:
    """The only safety net this task has.

    `list_blobs` populates `generation`, so `blob.delete()` issues a versioned
    delete: it destroys that exact version permanently and object versioning
    never engages. `bucket.delete_blob(name)` removes the live version and
    leaves a noncurrent one, recoverable until the bucket's 30-day rule clears
    it.

    `FakeBlob.delete` raises, so this test fails loudly if the implementation
    ever goes back to the obvious call. Learned on 2026-09-23, when a manual run
    permanently removed 1,686 objects that were meant to be archived.
    """
    deleted = bucket_of(60)

    result = replica.prune(keep=50)

    assert result.blobs_deleted == 20
    assert len(deleted) == 20


def test_one_failed_delete_does_not_abort_the_sweep(bucket_of, monkeypatch) -> None:
    """A half-deleted generation is already unusable, so stopping early would
    only leave more of them behind for tomorrow."""
    deleted = bucket_of(60)

    original = FakeBucket.delete_blob
    calls = {"n": 0}

    def flaky(self, name: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient GCS failure")
        original(self, name)

    monkeypatch.setattr(FakeBucket, "delete_blob", flaky)

    result = replica.prune(keep=50)

    assert result.blobs_deleted == 19  # 20 attempted, 1 failed
    assert len(deleted) == 19


# ── Dry run ───────────────────────────────────────────────────────────────────


def test_a_dry_run_reports_without_deleting(bucket_of) -> None:
    """Used to verify the prune against the real replica before letting it run
    unattended."""
    deleted = bucket_of(80)

    result = replica.prune(keep=50, dry_run=True)

    assert result.deleted == 30
    assert result.blobs_deleted == 60
    assert deleted == []


# ── The setting ───────────────────────────────────────────────────────────────


def test_it_defaults_to_the_configured_keep_count(bucket_of, settings) -> None:
    settings.LITESTREAM_KEEP_GENERATIONS = 25
    bucket_of(80)

    result = replica.prune()

    assert result.kept == 25
