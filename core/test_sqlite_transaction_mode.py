"""How our `atomic` blocks begin, and why that is not a detail.

Reproduces the production failure in `plans/receipt-upload-database-locked.md`:
every receipt upload returning `OperationalError: database is locked` from
`/tasks/process-receipt/`.

The name of that error is misleading, and it sent the first investigation at the
wrong thing. It is not contention — production runs one Cloud Run instance
(`max_instance_count = 1`) and one uvicorn worker, so there is no second writer
to contend with. It is `SQLITE_BUSY_SNAPSHOT`, which SQLite reports with the same
"database is locked" wording:

  `DATABASES["default"]["OPTIONS"]` sets no `transaction_mode`, so Django begins
  every `atomic` block with a bare `BEGIN` — deferred. The connection takes no
  lock there. It becomes a reader at its first SELECT, pinning a WAL snapshot,
  and only tries to become a writer later. If anything else committed in
  between, that upgrade fails.

The distinguishing fact, and the one the contention story cannot account for, is
that it fails *immediately*. `timeout: 60` buys nothing, because SQLite does not
call the busy handler for a stale snapshot — no amount of waiting can rescue it,
only a rollback. That is why raising the timeout from 20s to 60s changed nothing.

These run against a real file-backed database rather than the test database.
Django's SQLite test database is the shared in-memory one, where `PRAGMA
journal_mode=WAL` is a no-op — and with no WAL there is no snapshot to
invalidate, so the bug is unreproducible there.
"""

import copy
import sqlite3
import threading
import time

import pytest
from django.conf import settings
from django.db import OperationalError
from django.db.backends.sqlite3.base import DatabaseWrapper


@pytest.fixture(autouse=True)
def _allow_a_connection_of_our_own(django_db_blocker):
    """pytest-django blocks database access outside its own fixtures.

    Unblocked rather than marked `django_db`, because these deliberately do not
    touch the test database — they open a scratch file so there is a real WAL to
    invalidate. The mark would give us the in-memory database these tests exist
    to avoid.
    """
    with django_db_blocker.unblock():
        yield


def _wrapper(tmp_path, **options) -> DatabaseWrapper:
    """A connection configured exactly as production is, on a scratch file.

    Deliberately built from `settings.DATABASES["default"]` rather than from a
    hand-written dict. The claim under test is about *our* configuration, so
    reading it from the real setting is what makes these tests notice when it
    changes — which is the entire point of the second one.
    """
    settings_dict = copy.deepcopy(settings.DATABASES["default"])
    settings_dict["NAME"] = str(tmp_path / "repro.sqlite3")
    settings_dict["OPTIONS"] = {**settings_dict.get("OPTIONS", {}), **options}
    wrapper = DatabaseWrapper(settings_dict, alias="repro")
    wrapper.connect()
    return wrapper


def _interleave(tmp_path, **options) -> float:
    """Run the production sequence and return how long our write took to resolve.

    Read, let someone else try to write, then write ourselves — the shape of
    every phase in `process_receipt`, and of a great deal else besides.

    The other writer is a plain `sqlite3` connection on its own thread, because
    it stands in for a genuinely independent one: in production it is the
    redirect after upload rewriting a session row, which
    `SESSION_SAVE_EVERY_REQUEST = True` makes unconditional. It runs on a thread
    rather than inline precisely because the two modes are supposed to treat it
    differently — under DEFERRED it sails past us, under IMMEDIATE it must queue
    behind us. Inline, it could only ever do the first, which would make this
    unable to demonstrate the fix.
    """
    conn = _wrapper(tmp_path, **options)
    attempting = threading.Event()
    outcome: list[object] = []
    writer: threading.Thread | None = None

    def other_writer() -> None:
        # A patient timeout: under IMMEDIATE this connection is *supposed* to
        # wait for us. Anything shorter would fail the fix for being the fix.
        other = sqlite3.connect(conn.settings_dict["NAME"], timeout=30)
        try:
            attempting.set()
            other.execute("UPDATE thing SET v = 2 WHERE id = 1")
            other.commit()
            outcome.append(True)
        except Exception as exc:  # recorded, not raised: this is not the subject
            outcome.append(exc)
        finally:
            other.close()

    try:
        with conn.cursor() as cursor:
            cursor.execute("CREATE TABLE thing (id INTEGER PRIMARY KEY, v INTEGER)")
            cursor.execute("INSERT INTO thing (id, v) VALUES (1, 1)")

        # `atomic` reaches SQLite through exactly this call. Going through the
        # wrapper rather than issuing BEGIN by hand is what ties the result to
        # the `transaction_mode` setting instead of to this test's own SQL.
        conn.set_autocommit(False, force_begin_transaction_with_broken_autocommit=True)
        try:
            with conn.cursor() as cursor:
                # The snapshot is pinned here, not at BEGIN. In production this
                # is `Purchase.objects.filter(pk=...).first()`, after which the
                # task spent seconds in Gemini before writing anything.
                cursor.execute("SELECT v FROM thing WHERE id = 1")
                cursor.fetchone()

            writer = threading.Thread(target=other_writer, daemon=True)
            writer.start()
            attempting.wait(timeout=5)
            # Settling time for the other writer to either commit (DEFERRED) or
            # block (IMMEDIATE). Not a race we need to win — under DEFERRED it
            # has no lock to wait for, so it is always long enough, and under
            # IMMEDIATE waiting longer only makes it block for longer.
            time.sleep(0.25)

            started = time.monotonic()
            with conn.cursor() as cursor:
                cursor.execute("UPDATE thing SET v = 3 WHERE id = 1")
            elapsed = time.monotonic() - started
            conn.commit()

            writer.join(timeout=30)
            assert outcome and outcome[0] is True, (
                f"the other writer never completed: {outcome}. It should have "
                "gone either before us or after us, but it must go."
            )
            return elapsed
        finally:
            # Released before the connection closes. On the failure path the
            # other writer may still be blocked on our lock, and leaving it
            # there strands a thread on a file the fixture is about to delete.
            conn.rollback()
            if writer is not None:
                writer.join(timeout=30)
            conn.set_autocommit(True)
    finally:
        conn.close()


def test_a_transaction_that_reads_then_writes_survives_a_concurrent_commit(
    tmp_path,
) -> None:
    """The guard, and the one to watch fail.

    Under the current settings this raises `OperationalError: database is
    locked` — the production traceback, reproduced without Gemini, without
    Cloud Tasks and without a race to lose. It is deterministic: the mechanism
    turns on ordering, not on timing.

    Adding `"transaction_mode": "IMMEDIATE"` to the OPTIONS in
    `hf/settings/base.py` makes it pass, because the write lock is then taken at
    BEGIN and there is no upgrade left to fail.
    """
    try:
        _interleave(tmp_path)
    except OperationalError as exc:
        pytest.fail(
            f"a read-then-write transaction died on a concurrent commit: {exc}. "
            "This is the production failure — under BEGIN DEFERRED the upgrade "
            "from reader to writer fails outright once anything else has "
            "committed. Set OPTIONS['transaction_mode'] = 'IMMEDIATE'."
        )


def test_waiting_longer_does_not_help(tmp_path) -> None:
    """What separates this from ordinary contention, and the reason the busy
    timeout was raised 20s to 60s for no benefit.

    A contended write waits and then either succeeds or times out at `timeout`.
    A stale snapshot fails at once — SQLite never calls the busy handler for it.
    So if the failure above ever does become genuine contention, this is the
    test that stops us drawing the same wrong conclusion twice.
    """
    configured = settings.DATABASES["default"]["OPTIONS"].get("timeout", 5)

    started = time.monotonic()
    try:
        _interleave(tmp_path)
    except OperationalError:
        elapsed = time.monotonic() - started
        assert elapsed < configured / 2, (
            f"the write failed after {elapsed:.1f}s against a {configured}s "
            "timeout, so the busy handler did run and this is contention after "
            "all — not the stale-snapshot failure diagnosed in "
            "plans/receipt-upload-database-locked.md"
        )
    else:
        pytest.skip("no failure to characterise — transaction_mode is set")


def test_immediate_is_what_fixes_it(tmp_path) -> None:
    """The fix demonstrated rather than asserted.

    Pinned independently of the settings so the mechanism stays documented even
    once the setting is in place and the first test has gone quiet.
    """
    _interleave(tmp_path, transaction_mode="IMMEDIATE")
