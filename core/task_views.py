"""Background tasks that belong to the platform rather than to a game."""

from core.tasks import task

from . import replica


@task("prune-generations")
def prune_generations() -> None:
    """Keep the Litestream replica to a bounded number of generations.

    Daily. One generation is created per container start, and nothing else
    removes them — `retention-check-interval` in litestream.yml is deliberately
    longer than a container lives. Without this, cold starts get slower every
    day, because `litestream restore` inspects every generation to find the
    newest. See core.replica for the full reasoning.

    Deliberately not idempotent-by-count: it keeps the newest N, so running it
    twice in a row is harmless and running it late just means a slower boot in
    the meantime.
    """
    replica.prune()
