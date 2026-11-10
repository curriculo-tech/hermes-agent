"""``complete_task`` open-children gate (loop-gate-noop).

A parent/tracking card cannot be completed while it still has open (not
done/archived) children with no evidence — the close would be a no-op while
the real work sits unstarted. Overrides: ``metadata['allow_open_children']``
(reason recorded as an event) and ``metadata['superseded_by']`` (silent
carve-out). Scratch-DB fixture only; the live ``~/.hermes/kanban.db`` is never
touched (``HERMES_HOME`` is pointed at ``tmp_path``).
"""

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.init_db()
    return home


def _gate_events(conn, task_id: str, kind: str) -> list[dict]:
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
        (task_id, kind),
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows if r["payload"]]


def test_open_child_without_evidence_blocks_completion(kanban_home):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child", parents=(parent,))

        with pytest.raises(kb.OpenChildrenError) as excinfo:
            kb.complete_task(conn, parent)
        message = str(excinfo.value)
        assert child in message
        assert "todo" in message  # the child's status is listed
        assert kb.get_task(conn, parent).status == "ready"  # parent still not done
        assert excinfo.value.blockers == [(child, "todo")]


def test_allow_open_children_override_completes_and_records_reason(kanban_home):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child", parents=(parent,))
        reason = "parent ships, child tracked separately"

        assert kb.complete_task(
            conn, parent, metadata={"allow_open_children": reason},
        ) is True
        assert kb.get_task(conn, parent).status == "done"
        events = _gate_events(conn, parent, "completion_allowed_open_children")
        assert len(events) == 1
        assert events[0]["reason"] == reason
        assert events[0]["children"] == [{"id": child, "status": "todo"}]


def test_superseded_by_override_completes_silently(kanban_home):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        kb.create_task(conn, title="child", parents=(parent,))

        assert kb.complete_task(
            conn, parent, metadata={"superseded_by": "t_00000000"},
        ) is True
        assert kb.get_task(conn, parent).status == "done"
        # Carve-out: no gate event of either kind.
        assert _gate_events(conn, parent, "completion_allowed_open_children") == []
        assert _gate_events(conn, parent, "completion_blocked_open_children") == []


def test_done_child_with_result_lets_parent_complete(kanban_home):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child", parents=(parent,))
        # The child cannot be live-completed while its parent is open (parent
        # dependency gating); the gate under test only reads child status +
        # evidence, so mark the child done with a real result directly.
        conn.execute(
            "UPDATE tasks SET status = 'done', result = ? WHERE id = ?",
            ("child finished with real work", child),
        )

        assert kb.complete_task(conn, parent) is True
        assert kb.get_task(conn, parent).status == "done"


def test_open_child_with_result_evidence_lets_parent_complete(kanban_home):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child", parents=(parent,))
        conn.execute("UPDATE tasks SET result = ? WHERE id = ?", ("partial work", child))

        assert kb.complete_task(conn, parent) is True  # non-empty evidence never rejected
        assert kb.get_task(conn, parent).status == "done"


def test_already_done_parent_with_open_child_returns_false_without_event(kanban_home):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        kb.create_task(conn, title="child", parents=(parent,))
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))

        # Pre-gate behavior preserved: terminal parent is not a gate case.
        assert kb.complete_task(conn, parent) is False
        assert _gate_events(conn, parent, "completion_blocked_open_children") == []
        assert _gate_events(conn, parent, "completion_allowed_open_children") == []


def test_integration_block_lists_all_children_then_override_and_schema_unchanged(kanban_home):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        child_a = kb.create_task(conn, title="child a", parents=(parent,))
        child_b = kb.create_task(conn, title="child b", parents=(parent,))

        before_schema = [
            r["sql"] for r in conn.execute(
                "SELECT sql FROM sqlite_master ORDER BY name"
            ).fetchall()
        ]

        with pytest.raises(kb.OpenChildrenError):
            kb.complete_task(conn, parent)

        blocked = _gate_events(conn, parent, "completion_blocked_open_children")
        assert len(blocked) == 1
        listed = {entry["id"] for entry in blocked[0]["children"]}
        assert listed == {child_a, child_b}
        assert kb.get_task(conn, parent).status == "ready"

        assert kb.complete_task(
            conn, parent, metadata={"allow_open_children": "superseded by follow-up plan"},
        ) is True
        assert kb.get_task(conn, parent).status == "done"

        after_schema = [
            r["sql"] for r in conn.execute(
                "SELECT sql FROM sqlite_master ORDER BY name"
            ).fetchall()
        ]
        assert after_schema == before_schema  # no schema migration
