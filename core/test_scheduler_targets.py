"""Every scheduled job must point at a path the application actually serves.

Added after `hf-snapshot-product-ratings` returned 404 every morning from
2026-08-09 to 2026-08-31. Commit 9e8a2d5 renamed the task path from
`snapshot-product-ratings/` to `snapshot-ratings/` and updated
`terraform/cloud_scheduler.tf` in the same commit — correctly — but the
Terraform was never applied, so the deployed job kept calling the old URL.
See plans/scheduler-url-drift.md.

**This test would have passed on 8 August.** Both files were already consistent;
the divergence was between the repo and the deployed world, which no test that
reads only the repo can see. That job is done by the `scheduler_failure` alert
policy in `terraform/monitoring.tf`, which watches Cloud Scheduler's own verdict.

What this catches is the other half of the same mistake: a rename that updates
one file and not the other. That is the more likely error of the two, it is
free to check, and unlike the alert it fails before the change ships rather
than the morning after.
"""

import pathlib
import re

from django.urls import get_resolver

SCHEDULER_TF = pathlib.Path("terraform/cloud_scheduler.tf")

# The task-mounted prefix, from hf/urls.py. Scheduler URIs are absolute, so the
# host and this prefix have to come off before what remains can be compared
# against the patterns registered in hf/task_urls.py.
TASK_PREFIX = "/tasks/"


def scheduled_task_paths() -> set[str]:
    """Every `/tasks/<path>/` a scheduler job in Terraform points at.

    Deliberately ignores any URI outside the task prefix. Not every scheduler
    job has to target a task endpoint, and one that does not is not this test's
    business to have an opinion about.
    """
    uris = re.findall(r'uri\s*=\s*"([^"]+)"', SCHEDULER_TF.read_text())
    paths = set()
    for uri in uris:
        _, _, path = uri.partition("humanflourish.ing")
        if path.startswith(TASK_PREFIX):
            paths.add(path.removeprefix(TASK_PREFIX))
    return paths


def registered_task_paths() -> set[str]:
    """Every path pattern mounted under /tasks/, from the real URLconf."""
    resolver = get_resolver()
    for pattern in resolver.url_patterns:
        if str(pattern.pattern) == TASK_PREFIX.lstrip("/"):
            return {str(entry.pattern) for entry in pattern.url_patterns}
    raise AssertionError(f"nothing is mounted at {TASK_PREFIX} in hf/urls.py")


# ── The check ─────────────────────────────────────────────────────────────────


def test_every_scheduled_job_targets_a_registered_task() -> None:
    """A job whose URI does not resolve is a silent 404 once a day. Cloud
    Scheduler records the failure in its own log and the application never sees
    the request, so nothing else in the codebase would notice."""
    orphaned = scheduled_task_paths() - registered_task_paths()

    assert not orphaned, (
        "scheduler jobs point at paths not registered in hf/task_urls.py: "
        f"{sorted(orphaned)}"
    )


def test_the_terraform_file_was_actually_read() -> None:
    """Guards the guard. A typo'd path, a moved file, or a changed `uri = `
    formatting convention would make `scheduled_task_paths` return an empty set,
    and an empty set passes the test above against anything at all."""
    assert SCHEDULER_TF.exists(), f"{SCHEDULER_TF} is missing"
    assert len(scheduled_task_paths()) >= 5, (
        "parsed suspiciously few task URIs from cloud_scheduler.tf — "
        "check the `uri = ` format has not changed"
    )
