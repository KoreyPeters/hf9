"""Pruning the Litestream replica.

Litestream 0.3 creates a new *generation* — a full snapshot plus its WAL
segments — every time the container starts. On Cloud Run with scale-to-zero that
is 25-33 a day, and nothing removes them: `retention-check-interval` is
deliberately longer than a container lives (see `litestream.yml`), so the
in-process check never fires.

Left alone they accumulate without limit, and that is not merely untidy.
`litestream restore` inspects **every** generation to find the newest, at roughly
50ms each. Generations reached 758 in September 2026 and put 40 seconds into the
front of every cold start, which tripled boot time and began pushing the service
back toward the 80-second startup probe budget it had already blown once.
`plans/cron-collision-and-boot-regression.md` has the measurements.

**Why a task rather than a GCS lifecycle rule.** A lifecycle rule can only prune
by age, and age is the wrong axis: what boot time depends on is the *count*. An
age rule also deletes unconditionally, so an application idle longer than the
rule loses its whole replica — and `start.sh` runs
`litestream restore -if-replica-exists`, which would then start from an empty
database rather than fail loudly. Keeping the newest N has no such cliff: there
is no elapsed time after which the replica disappears.

The 30-day lifecycle rule on the bucket stays as a backstop, as does object
versioning, which keeps deleted objects recoverable as noncurrent versions —
but only because deletion goes through `bucket.delete_blob(name)`. See the
comment at the delete itself; using the obvious `blob.delete()` there silently
destroys the version instead of archiving it, and costs this module its only
safety net.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from django.conf import settings

logger = logging.getLogger(__name__)

# Matches `path: hf/db` in litestream.yml. If that moves, this must move with it
# — a mismatch here means the prune silently finds nothing and reports success,
# which is why `prune` treats "no generations at all" as an error rather than as
# nothing to do.
REPLICA_PREFIX = "hf/db/generations/"

# Nothing this recent is ever deleted, whatever the count says.
#
# The newest generation is the one the running container is currently writing,
# and keeping the newest N already protects it. This is deliberate belt and
# braces on top of that: it makes the safety property explicit rather than
# emergent from the sort order, so a future change to the ordering cannot
# quietly turn this into a task that deletes a live replica.
MINIMUM_AGE = timedelta(hours=1)


@dataclass(frozen=True)
class PruneResult:
    """What a prune did, for logging and for the task's return value."""

    total: int
    kept: int
    deleted: int
    blobs_deleted: int
    skipped_too_recent: int

    def __str__(self) -> str:
        return (
            f"{self.total} generations, kept {self.kept}, deleted {self.deleted} "
            f"({self.blobs_deleted} objects), "
            f"{self.skipped_too_recent} retained as too recent"
        )


def _bucket():
    """The replica bucket, or a clear failure.

    Refuses to guess. An empty setting in production means the env var did not
    arrive, and a prune pointed at the wrong bucket is worse than no prune.
    """
    from google.cloud import storage

    name = settings.LITESTREAM_GCS_BUCKET
    if not name:
        raise RuntimeError(
            "LITESTREAM_GCS_BUCKET is not set; refusing to guess a bucket name."
        )
    return storage.Client().bucket(name)


def _generations(bucket) -> dict[str, tuple[datetime, list]]:
    """Every generation, as `{id: (newest object time, [blobs])}`.

    One paginated LIST over the whole prefix rather than a call per generation.
    That distinction is the entire point: walking generations individually is
    what makes `litestream restore` slow, and repeating the mistake here would
    put the cost back in a different place.
    """
    generations: dict[str, tuple[datetime, list]] = {}
    for blob in bucket.list_blobs(prefix=REPLICA_PREFIX):
        rest = blob.name[len(REPLICA_PREFIX) :]
        generation_id, _, _ = rest.partition("/")
        if not generation_id:
            continue
        created = blob.time_created
        if generation_id in generations:
            newest, blobs = generations[generation_id]
            blobs.append(blob)
            generations[generation_id] = (max(newest, created), blobs)
        else:
            generations[generation_id] = (created, [blob])
    return generations


def prune(keep: int | None = None, *, dry_run: bool = False) -> PruneResult:
    """Delete all but the newest `keep` generations.

    Ordered by the newest object within each generation, using GCS's own
    server-side creation times rather than anything derived from the generation
    id, which is random hex and carries no ordering.
    """
    if keep is None:
        keep = settings.LITESTREAM_KEEP_GENERATIONS
    if keep < 1:
        raise ValueError(f"keep must be at least 1, got {keep}")

    bucket = _bucket()
    generations = _generations(bucket)

    if not generations:
        # Not "nothing to do". Either the prefix is wrong or the replica is
        # missing, and both are worth waking someone for — a silent success here
        # would hide exactly the failure this task exists to prevent.
        raise RuntimeError(
            f"No generations found under {REPLICA_PREFIX!r}. "
            "The replica is missing or the prefix no longer matches litestream.yml."
        )

    ordered = sorted(generations.items(), key=lambda kv: kv[1][0], reverse=True)
    candidates = ordered[keep:]

    cutoff = datetime.now(timezone.utc) - MINIMUM_AGE
    doomed = [(gid, blobs) for gid, (created, blobs) in candidates if created < cutoff]
    too_recent = len(candidates) - len(doomed)

    blobs_deleted = 0
    if not dry_run:
        for generation_id, blobs in doomed:
            for blob in blobs:
                try:
                    # `bucket.delete_blob(name)`, deliberately, **not**
                    # `blob.delete()`.
                    #
                    # `list_blobs` returns blobs with `generation` populated, so
                    # `blob.delete()` issues a versioned delete — it destroys
                    # that exact version permanently and bypasses object
                    # versioning entirely. Deleting by name instead removes the
                    # *live* version and archives it as a noncurrent one, which
                    # the bucket's 30-day lifecycle rule then clears up.
                    #
                    # That distinction is the whole safety net for this task. A
                    # prune that deleted the wrong generations stays recoverable
                    # for 30 days one way and is unrecoverable the other. Found
                    # the hard way on 2026-09-23, after a manual run
                    # permanently removed 1,686 objects that were supposed to
                    # have been archived.
                    bucket.delete_blob(blob.name)
                    blobs_deleted += 1
                except Exception:
                    # One failure must not abort the sweep. Tomorrow's run picks
                    # up whatever is left; a half-deleted generation is already
                    # unusable, so stopping early would only delay the cleanup.
                    logger.warning(
                        "Failed to delete %s from generation %s",
                        blob.name,
                        generation_id,
                        exc_info=True,
                    )
    else:
        blobs_deleted = sum(len(blobs) for _, blobs in doomed)

    result = PruneResult(
        total=len(generations),
        kept=len(ordered) - len(doomed),
        deleted=len(doomed),
        blobs_deleted=blobs_deleted,
        skipped_too_recent=too_recent,
    )
    logger.info("Litestream prune%s: %s", " (dry run)" if dry_run else "", result)
    return result
