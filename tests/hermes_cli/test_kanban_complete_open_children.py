"""Gate: ``complete_task`` rejects an EMPTY-EVIDENCE parent completion while
un-started (non-terminal, evidence-empty) children exist — card t_ca1c4a33
"loop-gate-noop". Escapes: ``metadata['allow_open_children']`` and
``metadata['superseded_by']``; a completion with a real ``result``/``summary``
is never rejected."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB.

    Same shape as ``test_kanban_db.py``'s fixture, deliberately re-declared
    (not imported) so this file stays hermetic and independent.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_parent_with_open_child(conn) -> tuple[str, str]:
    """Claimed parent plus an un-started direct child (status ``todo``)."""
    parent = kb.create_task(conn, title="parent slice", assignee="alice")
    kb.claim_task(conn, parent)
    child = kb.create_task(conn, title="child slice", assignee="bob", parents=[parent])
    return parent, child


def _event_kinds(conn, task_id: str) -> list[str]:
    return [
        row["kind"]
        for row in conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (task_id,)
        )
    ]


def test_empty_complete_with_open_empty_child_rejected(kanban_home):
    conn = kbc.connect()
    try:
        parent, child = _running_parent_with_open_child(conn)
        child_status = kb.get_task(conn, child).status

        with pytest.raises(kb.OpenChildrenError) as excinfo:
            kb.complete_task(conn, parent)

        message = str(excinfo.value)
        # Every offending child listed as "<id> (<status>)" + both escapes named.
        assert f"{child} ({child_status})" in message
        assert "allow_open_children" in message
        assert "superseded_by" in message
        # Recoverable: ValueError, structured payload on the exception.
        assert isinstance(excinfo.value, ValueError)
        assert excinfo.value.open_children == [(child, child_status)]
        assert excinfo.value.completing_task_id == parent

        # Parent untouched; auditable event landed on the parent.
        assert kb.get_task(conn, parent).status == "running"
        assert "completion_blocked_open_children" in _event_kinds(conn, parent)

        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? "
            "AND kind='completion_blocked_open_children'",
            (parent,),
        ).fetchone()
        assert json.loads(row["payload"]) == {
            "open_children": [{"id": child, "status": child_status}],
            "result_len": 0,
            "summary": None,
        }
    finally:
        conn.close()


def test_allow_open_children_override_passes(kanban_home):
    conn = kbc.connect()
    try:
        parent, _child = _running_parent_with_open_child(conn)
        ok = kb.complete_task(
            conn, parent,
            metadata={"allow_open_children": "slices deferred to follow-up"},
        )
        assert ok is True
        assert kb.get_task(conn, parent).status == "done"
        assert "completion_blocked_open_children" not in _event_kinds(conn, parent)
    finally:
        conn.close()


def test_superseded_by_passes(kanban_home):
    conn = kbc.connect()
    try:
        parent, _child = _running_parent_with_open_child(conn)
        ok = kb.complete_task(conn, parent, metadata={"superseded_by": "t_deadbeef"})
        assert ok is True
        assert kb.get_task(conn, parent).status == "done"
        assert "completion_blocked_open_children" not in _event_kinds(conn, parent)
    finally:
        conn.close()


def test_all_children_done_empty_parent_completes(kanban_home):
    conn = kbc.connect()
    try:
        parent = kb.create_task(conn, title="parent slice", assignee="alice")
        kb.claim_task(conn, parent)
        child = kb.create_task(conn, title="child slice", assignee="bob")
        kb.claim_task(conn, child)
        assert kb.complete_task(conn, child, result="child shipped its slice") is True
        kb.link_tasks(conn, parent, child)  # link after the fact: child is done
        assert kb.get_task(conn, child).status == "done"

        assert kb.complete_task(conn, parent) is True
        assert kb.get_task(conn, parent).status == "done"
        assert "completion_blocked_open_children" not in _event_kinds(conn, parent)
    finally:
        conn.close()


def test_non_empty_parent_result_not_rejected(kanban_home):
    conn = kbc.connect()
    try:
        parent, _child = _running_parent_with_open_child(conn)
        ok = kb.complete_task(conn, parent, result="real handoff evidence")
        assert ok is True
        assert kb.get_task(conn, parent).status == "done"
        assert "completion_blocked_open_children" not in _event_kinds(conn, parent)
    finally:
        conn.close()


def test_gate_uses_scratch_kanban_db_fixture(kanban_home, tmp_path):
    """Integration: the gate runs against the scratch kanban.db under the
    tmp HERMES_HOME (never the live ~/.hermes), and a rejected complete
    leaves the tasks schema/rows untouched except the additive event row."""
    conn = kbc.connect()
    try:
        db_path = kb.kanban_db_path().resolve()
        assert db_path.is_relative_to(tmp_path.resolve())
        real_home = Path(os.path.expanduser("~")).resolve()
        assert not db_path.is_relative_to(real_home)
        assert db_path.exists()

        parent, _child = _running_parent_with_open_child(conn)
        schema_before = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()["sql"]
        tasks_before = [tuple(r) for r in conn.execute("SELECT * FROM tasks ORDER BY id")]
        links_before = [tuple(r) for r in conn.execute("SELECT * FROM task_links ORDER BY parent_id, child_id")]
        runs_before = [tuple(r) for r in conn.execute("SELECT * FROM task_runs ORDER BY id")]
        events_before = [
            (r["id"], r["task_id"], r["kind"])
            for r in conn.execute("SELECT id, task_id, kind FROM task_events ORDER BY id")
        ]

        with pytest.raises(kb.OpenChildrenError):
            kb.complete_task(conn, parent)

        assert conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()["sql"] == schema_before
        assert [tuple(r) for r in conn.execute("SELECT * FROM tasks ORDER BY id")] == tasks_before
        assert [tuple(r) for r in conn.execute("SELECT * FROM task_links ORDER BY parent_id, child_id")] == links_before
        assert [tuple(r) for r in conn.execute("SELECT * FROM task_runs ORDER BY id")] == runs_before
        events_after = [
            (r["id"], r["task_id"], r["kind"])
            for r in conn.execute("SELECT id, task_id, kind FROM task_events ORDER BY id")
        ]
        # Exactly one additive event row on the parent; nothing else changed.
        new_events = events_after[len(events_before):]
        assert new_events == [(events_before[-1][0] + 1, parent, "completion_blocked_open_children")]
    finally:
        conn.close()


def test_already_done_parent_with_open_child_is_noop_false(kanban_home):
    """Bulk/scripted completes over non-actionable ids stay noops: a parent
    already ``done`` (a status the CAS rejects) with an open evidence-empty
    child returns ``False`` — no OpenChildrenError, no audit spam."""
    conn = kbc.connect()
    try:
        parent = kb.create_task(conn, title="parent slice", assignee="alice")
        kb.claim_task(conn, parent)
        assert kb.complete_task(conn, parent, result="shipped slice") is True
        assert kb.get_task(conn, parent).status == "done"
        child = kb.create_task(conn, title="child slice", assignee="bob", parents=[parent])
        assert kb.get_task(conn, child).status not in ("done", "archived")

        assert kb.complete_task(conn, parent) is False
        assert kb.get_task(conn, parent).status == "done"
        assert "completion_blocked_open_children" not in _event_kinds(conn, parent)
    finally:
        conn.close()
