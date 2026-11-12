"""Dependency refusals name the open parents on every surface.

``complete_task`` reports every refusal as bare ``False``. On the CLI surface
the open PRs #110323/#110330/#110334 make the parents refusal actionable;
the worker-facing TOOL surface (``tools/kanban_tools._handle_complete`` — the
call a dispatcher-owned worker actually makes) collapsed it into
"unknown id, stale run, or already terminal", sending the worker/operator
chasing stale runs while the real blocker was a reopened or unfinished parent.
"""
import json

import pytest


@pytest.fixture
def running_child_with_parent(monkeypatch, tmp_path):
    """A claimed (running) child whose parent is NOT done. Returns ids."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        parent_id = kb.create_task(conn, title="parent gate", assignee="op")
        assert kb.complete_task(conn, parent_id, result="parent shipped")
        child_id = kb.create_task(
            conn, title="child work", assignee="test-worker", parents=[parent_id])
        assert kb.claim_task(conn, child_id) is not None
        run_id = kb._current_run_id(conn, child_id)
        # Parent reopens mid-run: the #113373 timeline.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'todo', completed_at = NULL WHERE id = ?",
                (parent_id,),
            )
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", child_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return parent_id, child_id


def test_complete_names_unsatisfied_parent(running_child_with_parent):
    """The refusal names the blocking parent instead of crying stale run."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    parent_id, child_id = running_child_with_parent
    out = json.loads(kt._handle_complete(
        {"task_id": child_id, "summary": "genuinely done work"}))
    assert out.get("error")
    assert parent_id in out["error"]
    assert "parent" in out["error"].lower()
    assert "stale run" not in out["error"]
    # Nothing mutated: the run is still open for a retry after the parent.
    conn = kbc.connect()
    try:
        assert kb.get_task(conn, child_id).status == "running"
    finally:
        conn.close()
    # The board view names the same parents on the running card.
    shown = json.loads(kt._handle_show({"task_id": child_id}))
    assert shown["unsatisfied_parents"] == [{"id": parent_id, "status": "todo"}]


def test_cli_complete_names_unsatisfied_parent(running_child_with_parent, monkeypatch, capsys):
    """`hermes kanban complete` (operator, --force) reports the same blockers."""
    import argparse

    from hermes_cli import kanban as kc

    parent_id, child_id = running_child_with_parent
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    rc = kc._cmd_complete(argparse.Namespace(
        task_ids=[child_id], result=None, summary=None, metadata=None, force=True))
    out = capsys.readouterr()
    assert rc != 0
    assert parent_id in out.out + out.err
    assert "unknown id or terminal state" not in out.out + out.err


@pytest.fixture
def running_parent_with_open_child(monkeypatch, tmp_path):
    """A claimed (running) parent with one un-started, evidence-less child:
    the open-children completion gate (loop-gate-noop). Returns ids."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        parent_id = kb.create_task(conn, title="parent stage", assignee="test-worker")
        assert kb.claim_task(conn, parent_id) is not None
        run_id = kb._current_run_id(conn, parent_id)
        child_id = kb.create_task(
            conn, title="next stage", assignee="test-worker", parents=[parent_id])
        assert kb.get_task(conn, child_id).status == "todo"
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", parent_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return parent_id, child_id


def test_cli_complete_open_children_is_friendly_failure(
    running_parent_with_open_child, monkeypatch, capsys
):
    """CLI completion of a parent with an un-started evidence-less child fails
    with a plain message naming the child and the --metadata override; the
    kernel exception must never escape as a traceback."""
    import argparse

    from hermes_cli import kanban as kc

    parent_id, child_id = running_parent_with_open_child
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    rc = kc._cmd_complete(argparse.Namespace(
        task_ids=[parent_id], result=None, summary="stage done", metadata=None, force=False))
    out = capsys.readouterr()
    assert rc != 0
    assert child_id in out.out + out.err
    assert "allow_open_children" in out.out + out.err


def test_tool_complete_open_children_returns_error_string(running_parent_with_open_child):
    """The worker-facing tool surface answers with a retryable error string
    (child id + override hint), not an exception the run would crash on."""
    import json

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    parent_id, child_id = running_parent_with_open_child
    out = kt._handle_complete({"task_id": parent_id, "summary": "stage handoff"})
    assert isinstance(out, str)
    payload = json.loads(out)
    assert payload.get("error")
    assert child_id in payload["error"]
    assert "allow_open_children" in payload["error"]
    # The refusal ran before the write txn: nothing mutated, retry is safe.
    conn = kbc.connect()
    try:
        assert kb.get_task(conn, parent_id).status == "running"
    finally:
        conn.close()
