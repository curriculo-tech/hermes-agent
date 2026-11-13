"""``complete_task`` refuses a parent whose children are un-started and
evidence-less (loop-gate-noop); supersede / override carve-outs pass."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kbc.connect() as c:
        yield c


def _event_kinds(conn, task_id) -> list[str]:
    return [r["kind"] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
    ).fetchall()]


def _parent_with_open_children(conn) -> tuple[str, dict[str, str]]:
    """Claimed parent with one ``todo`` and one ``blocked`` child, neither
    carrying evidence; the parent's own ``result`` stays empty. Most tests
    pass a substantive ``summary`` so the sibling empty-evidence gate stays
    quiet; the ordering test intentionally passes neither."""
    parent = kb.create_task(conn, title="parent", assignee="coder")
    assert kb.claim_task(conn, parent, claimer=kb._claimer_id()) is not None
    kb.create_task(conn, title="child todo", assignee="coder", parents=[parent])
    kb.create_task(
        conn, title="child blocked", assignee="coder", parents=[parent],
        initial_status="blocked",
    )
    kids = {
        child_id: kb.get_task(conn, child_id).status
        for child_id in kb.child_ids(conn, parent)
    }
    assert sorted(kids.values()) == ["blocked", "todo"]
    return parent, kids


def test_open_child_empty_rejects_and_names_children(conn):
    parent, kids = _parent_with_open_children(conn)
    with pytest.raises(kb.OpenChildrenWithoutEvidenceError) as ei:
        kb.complete_task(conn, parent, summary="closing while children still open")
    msg = str(ei.value)
    for child_id, status in kids.items():
        assert f"{child_id} ({status})" in msg
    # Deterministic ordering: children listed sorted by id.
    assert ei.value.children == sorted(kids.items())
    row = conn.execute("SELECT status, result FROM tasks WHERE id = ?", (parent,)).fetchone()
    assert row["status"] == "running"
    assert not row["result"]
    kinds = _event_kinds(conn, parent)
    assert "completion_blocked_open_children" in kinds
    assert "completed" not in kinds


def test_open_child_empty_parent_rejects_naming_children(conn):
    """The card's original bug case: parent completed with NO result and NO
    summary while children are un-started. The open-children gate runs first,
    so the child-naming refusal wins over the generic empty-evidence one."""
    parent, kids = _parent_with_open_children(conn)
    with pytest.raises(kb.OpenChildrenWithoutEvidenceError) as ei:
        kb.complete_task(conn, parent)
    assert not isinstance(ei.value, kb.EmptyCompletionError)
    msg = str(ei.value)
    for child_id, status in kids.items():
        assert f"{child_id} ({status})" in msg
    row = conn.execute("SELECT status, result FROM tasks WHERE id = ?", (parent,)).fetchone()
    assert row["status"] == "running"
    assert not row["result"]
    kinds = _event_kinds(conn, parent)
    assert "completion_blocked_open_children" in kinds
    assert "completed" not in kinds


def test_childless_empty_complete_still_raises_empty_completion(conn):
    """Leaf card with no children and no result/summary still hits the
    sibling empty-evidence gate; the ordering swap must not swallow it."""
    tid = kb.create_task(conn, title="leaf", assignee="coder")
    assert kb.claim_task(conn, tid, claimer=kb._claimer_id()) is not None
    with pytest.raises(kb.EmptyCompletionError):
        kb.complete_task(conn, tid)
    assert kb.get_task(conn, tid).status == "running"
    kinds = _event_kinds(conn, tid)
    assert "completion_blocked_empty_result" in kinds
    assert "completed" not in kinds


def test_open_child_allow_open_children_override_passes(conn):
    parent, _kids = _parent_with_open_children(conn)
    assert kb.complete_task(
        conn, parent, summary="closing with open children",
        metadata={"allow_open_children": "children tracked on the follow-up card"},
    ) is True
    assert kb.get_task(conn, parent).status == "done"


def test_open_child_superseded_by_passes(conn):
    parent, _kids = _parent_with_open_children(conn)
    assert kb.complete_task(
        conn, parent, summary="superseded by the re-planned card",
        metadata={"superseded_by": "t_17e0d3e2"},
    ) is True
    assert kb.get_task(conn, parent).status == "done"


def test_all_children_done_empty_parent_completes(conn):
    parent = kb.create_task(conn, title="parent", assignee="coder")
    for i in (1, 2):
        child = kb.create_task(conn, title=f"child {i}", assignee="coder")
        assert kb.claim_task(conn, child, claimer=kb._claimer_id()) is not None
        assert kb.complete_task(conn, child, result=f"child {i} shipped") is True
        assert kb.link_tasks(conn, parent, child) is not None
    assert kb.claim_task(conn, parent, claimer=kb._claimer_id()) is not None
    # Legitimate empty close: empty result, evidence via summary, all children done.
    assert kb.complete_task(conn, parent, summary="children covered everything") is True
    row = conn.execute("SELECT status, result FROM tasks WHERE id = ?", (parent,)).fetchone()
    assert row["status"] == "done"
    assert not row["result"]
    assert "completion_blocked_open_children" not in _event_kinds(conn, parent)


def test_children_done_nonempty_parent_unchanged(conn):
    parent = kb.create_task(conn, title="parent", assignee="coder")
    child = kb.create_task(conn, title="child", assignee="coder")
    assert kb.claim_task(conn, child, claimer=kb._claimer_id()) is not None
    assert kb.complete_task(conn, child, result="child shipped") is True
    assert kb.link_tasks(conn, parent, child) is not None
    assert kb.claim_task(conn, parent, claimer=kb._claimer_id()) is not None
    assert kb.complete_task(conn, parent, result="wrapped up the slice") is True
    row = conn.execute("SELECT status, result FROM tasks WHERE id = ?", (parent,)).fetchone()
    assert row["status"] == "done"
    assert row["result"] == "wrapped up the slice"
    kinds = _event_kinds(conn, parent)
    assert "completed" in kinds
    assert "completion_blocked_open_children" not in kinds


def test_integration_scratch_db(tmp_path, monkeypatch):
    """End-to-end on a scratch kanban.db under tmp_path: reject, then the
    ``allow_open_children`` override completes the same parent."""
    home = tmp_path / "scratch-home" / ".hermes"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    assert str(db_path.resolve()).startswith(str(tmp_path.resolve()))
    assert db_path.resolve().is_file()
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="coder")
        assert kb.claim_task(conn, parent, claimer=kb._claimer_id()) is not None
        kb.create_task(conn, title="child a", assignee="coder", parents=[parent])
        kb.create_task(conn, title="child b", assignee="coder", parents=[parent])
        with pytest.raises(kb.OpenChildrenWithoutEvidenceError):
            kb.complete_task(conn, parent, summary="closing too early")
        assert kb.get_task(conn, parent).status == "running"
        assert kb.complete_task(
            conn, parent, summary="closing with open children",
            metadata={"allow_open_children": "children moved to the follow-up card"},
        ) is True
        assert kb.get_task(conn, parent).status == "done"
        kinds = _event_kinds(conn, parent)
        assert "completion_blocked_open_children" in kinds
        assert "completed" in kinds


def test_open_child_with_result_evidence_not_gated(conn):
    """Never reject non-empty evidence: an open child carrying a substantive
    ``tasks.result`` exempts it from the gate."""
    parent = kb.create_task(conn, title="parent", assignee="coder")
    assert kb.claim_task(conn, parent, claimer=kb._claimer_id()) is not None
    child = kb.create_task(conn, title="child", assignee="coder")
    conn.execute("UPDATE tasks SET result = ? WHERE id = ?", ("child shipped its slice", child))
    conn.commit()
    assert kb.link_tasks(conn, parent, child) is not None
    assert kb.complete_task(conn, parent, summary="child carried the evidence") is True
    assert kb.get_task(conn, parent).status == "done"
    assert "completion_blocked_open_children" not in _event_kinds(conn, parent)


def test_open_child_summary_only_evidence_not_gated(conn):
    """A closed run summary counts as evidence even with an empty task result."""
    parent = kb.create_task(conn, title="parent", assignee="coder")
    assert kb.claim_task(conn, parent, claimer=kb._claimer_id()) is not None
    child = kb.create_task(conn, title="child", assignee="coder")
    assert kb.claim_task(conn, child, claimer=kb._claimer_id()) is not None
    assert kb.complete_task(conn, child, summary="evidence: reviewed") is True
    conn.execute("UPDATE tasks SET status = 'todo', result = '' WHERE id = ?", (child,))
    conn.commit()
    assert kb.link_tasks(conn, parent, child) is not None
    assert kb.complete_task(conn, parent, summary="child evidence lives in its run summary") is True
    assert kb.get_task(conn, parent).status == "done"


def test_archived_child_not_gated(conn):
    parent = kb.create_task(conn, title="parent", assignee="coder")
    assert kb.claim_task(conn, parent, claimer=kb._claimer_id()) is not None
    child = kb.create_task(conn, title="child", assignee="coder")
    conn.execute("UPDATE tasks SET status = 'archived' WHERE id = ?", (child,))
    conn.commit()
    assert kb.link_tasks(conn, parent, child) is not None
    assert kb.complete_task(conn, parent, summary="archived child needs no evidence") is True
    assert kb.get_task(conn, parent).status == "done"


@pytest.mark.parametrize("bad_override", ["", "   ", 0, False, ["reason"]])
def test_allow_open_children_requires_nonempty_string(conn, bad_override):
    """Override is opt-in and must be an explicit non-empty string reason."""
    parent, _kids = _parent_with_open_children(conn)
    with pytest.raises(kb.OpenChildrenWithoutEvidenceError):
        kb.complete_task(
            conn, parent, summary="closing early",
            metadata={"allow_open_children": bad_override},
        )
    assert kb.get_task(conn, parent).status == "running"


def test_review_parent_with_open_child_still_approved(conn):
    """Human review approval stays exempt: approving a ``review`` parent with
    open, evidence-less children must complete (the human is the record), and
    the gate must not write a blocked event."""
    parent = kb.create_task(conn, title="parent", assignee="coder")
    assert kb.claim_task(conn, parent, claimer=kb._claimer_id()) is not None
    kb.create_task(conn, title="child", assignee="coder", parents=[parent])
    conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (parent,))
    conn.commit()
    assert kb.get_task(conn, parent).status == "review"
    assert kb.complete_task(conn, parent) is True
    assert kb.get_task(conn, parent).status == "done"
    assert "completion_blocked_open_children" not in _event_kinds(conn, parent)
