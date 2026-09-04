"""Migrations run before the units that read the new schema are restarted.

`db.init_db` is the only framework migration runner, and nothing on a
long-running process's own startup path calls it — not `get_db`, not the
scheduler, not `web_app`, not `serve`. So the window between new code and a
migrated database is closed by deployment ordering alone, in a file no schema
change ever touches. That ordering is what this pins.

The failure it guards is loud and total rather than subtle. `_TASK_COLUMNS` is
the SELECT list for every `_row_to_task`, and `sqlite3.Row` raises `IndexError`
for a column that is not in the result set rather than yielding None — so a
process holding code that names a column the file does not have fails on the
*first* task read, in every process that does one. Adding a column to that
tuple is therefore a change whose correctness lives here, and an implementer
who has only read `db.py` has no way to know it.

Two of the three shipped shapes are closed and are asserted below. The third,
Docker, is not: `docker/istota/entrypoint.sh` writes the config-ready flag the
web container waits on well before it runs `istota init`, so the web process
can serve requests against an unmigrated database for as long as the rest of
that script takes. Left as it is, deliberately — it is a pre-existing property
of that shape (`web_app.py` documents the same class of window for
`task_usage`) and closing it is a deployment change with its own argument, not
a consequence of any one column. It is named here rather than left out so the
absence of an assertion reads as a decision instead of an oversight.
"""

from __future__ import annotations

import inspect
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UPDATE_SCRIPT = REPO / "deploy" / "ansible" / "templates" / "istota-update.sh.j2"


def test_the_server_auto_update_migrates_before_it_restarts_anything():
    """The Ansible shape, which is the canonical deployment.

    Asserted on the raw template rather than a render, following
    `test_ansible_clone_credential.py`'s own ordering check: both lines are
    unconditional, and a render would only add the chance of choosing vars that
    hide one of them behind an `{% if %}`.
    """
    script = UPDATE_SCRIPT.read_text()

    migrate_at = script.find("init_db(Path(")
    restart_at = script.find("systemctl restart")

    assert migrate_at != -1, "the auto-update script no longer runs migrations"
    assert restart_at != -1, "the auto-update script no longer restarts anything"
    assert migrate_at < restart_at, (
        "the auto-update script restarts a unit before migrating, so new code "
        "serves against the old schema until the next run"
    )


def test_the_standalone_updater_migrates_from_the_new_code():
    """The standalone shape, where the ordering constraint is a second one.

    `istota update` reinstalls and then migrates, so the ordering above is not
    in question — the daemon is stopped for the whole of it. What can go wrong
    here instead is migrating with the *old* schema module still resident, and
    the fix is that the migration is a subprocess rather than an in-process
    `db.init_db`. A refactor back to the direct call would be invisible to
    every other test and would silently skip any migration shipped in the
    update itself.

    This reads one function and does **not** assert it is the default `migrate`
    the update path installs. That half is
    `tests/test_updater.py::TestCheckoutFlow::test_default_migrate_runs_fresh_istota_init`,
    which goes red on a rewiring this one cannot see; named here so neither is
    deleted in the belief the other covers it.
    """
    from istota import updater

    source = inspect.getsource(updater._run_fresh_migrations)
    # Defensive, not load-bearing today: that docstring names `db.init_db`
    # without a paren, so neither needle matches it as written. The strip is so
    # that a future docstring spelling `init_db(...)` cannot satisfy the check
    # from the prose alone.
    body = source.replace(updater._run_fresh_migrations.__doc__ or "", "")
    assert '"init"' in body, "the migration no longer shells out to `istota init`"
    assert "init_db(" not in body, (
        "the standalone updater migrates in-process, with the pre-update schema "
        "code still loaded"
    )
