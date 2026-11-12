"""loop-gate-noop: an EMPTY-EVIDENCE parent ``complete`` is rejected while
un-started/evidence-less children remain (with ``allow_open_children`` and
``superseded_by`` carve-outs), and legitimate empty closes still pass.

All tests run against a scratch ``kanban.db`` in ``tmp_path`` — never the
live board. Helper style modeled on
``tests/hermes_cli/test_kanban_review_lifecycle_complete.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


DB_PATH_NAME = "kanban.db"


@pytest.fixture
def conn(tmp_path: Path):
    db = kbc.connect(tmp_path / DB_PATH_NAME)
    try:
        yield db
    finally:
        db.close()


def _parent_with_open_children(conn, count: int = 1) -> tuple[str, list[str]]:
    """Parent in ``ready`` with ``count`` fresh ``todo`` children (created via
    ``parents=`` so they are un-started: never claimed, no run, no result)."""
    parent_id = kb.create_task(conn, title="Parent slice", assignee="builder")
    children = [
        kb.create_task(
            conn,
            title=f"Child step {i + 1}",
            assignee="builder",
            parents=[parent_id],
        )
        for i in range(count)
    ]
    return parent_id, children


def _done_child(conn, title: str) -> str:
    """Standalone child completed with a real summary, then linked to the
    parent. (Children created under an open parent are ``todo`` and cannot be
    claimed until the parent is done, so done children must precede the link.)"""
    child_id = kb.create_task(conn, title=title, assignee="builder")
    assert kb.claim_task(conn, child_id, claimer="builder:test") is not None
    assert kb.complete_task(conn, child_id, summary=f"{title}: finished and verified")
    return child_id


def _status(conn, task_id: str) -> str:
    task = kb.get_task(conn, task_id)
    assert task is not None
    return task.status


# 1. Open-child + empty-evidence parent -> reject, name the child, no mutation.
def test_open_child_without_evidence_blocks_empty_complete(conn):
    parent_id, (child_id,) = _parent_with_open_children(conn)
    assert _status(conn, child_id) == "todo"

    with pytest.raises(kb.OpenChildrenEvidenceError) as excinfo:
        kb.complete_task(conn, parent_id)

    message = str(excinfo.value)
    assert child_id in message
    assert f"{child_id} (todo)" in message
    assert "allow_open_children" in message
    assert "superseded_by" in message
    assert excinfo.value.parent_id == parent_id
    assert excinfo.value.children == [{"id": child_id, "status": "todo"}]
    # Pre-txn gate: the parent was NOT mutated.
    assert _status(conn, parent_id) == "ready"
    assert kb.get_task(conn, parent_id).result is None


# 2. Explicit override -> completes; reason lands on the completed event.
def test_allow_open_children_override_completes(conn):
    parent_id, _ = _parent_with_open_children(conn)
    reason = "child deliberately deferred to next round"

    assert kb.complete_task(conn, parent_id, metadata={"allow_open_children": reason})
    assert _status(conn, parent_id) == "done"

    completed = [e for e in kb.list_events(conn, parent_id) if e.kind == "completed"][-1]
    assert completed.payload["allow_open_children_reason"] == reason


# 3. Supersede carve-out -> completes.
def test_superseded_by_override_completes(conn):
    parent_id, _ = _parent_with_open_children(conn)

    assert kb.complete_task(conn, parent_id, metadata={"superseded_by": "t_ffffffff"})
    assert _status(conn, parent_id) == "done"


# 4. All children done + empty parent result -> legitimate empty close passes.
def test_all_children_done_empty_parent_completes(conn):
    child_id = _done_child(conn, "Child step 1")
    parent_id = kb.create_task(conn, title="Parent slice", assignee="builder")
    kb.link_tasks(conn, parent_id, child_id)
    assert _status(conn, child_id) == "done"

    assert kb.complete_task(conn, parent_id)
    assert _status(conn, parent_id) == "done"


# 5. All children done + non-empty parent result -> unchanged behavior.
def test_all_children_done_nonempty_parent_completes_as_before(conn):
    child_id = _done_child(conn, "Child step 1")
    parent_id = kb.create_task(conn, title="Parent slice", assignee="builder")
    kb.link_tasks(conn, parent_id, child_id)

    assert kb.complete_task(conn, parent_id, result="Slice shipped with all children green")
    parent = kb.get_task(conn, parent_id)
    assert parent is not None
    assert parent.status == "done"
    assert parent.result == "Slice shipped with all children green"


# 6. Open child that DOES have evidence -> parent completes (never reject
#    a child that carries real evidence, matching "never reject non-empty").
def test_open_child_with_evidence_does_not_block(conn):
    child_id = kb.create_task(conn, title="Child step 1", assignee="builder")
    claimed = kb.claim_task(conn, child_id, claimer="builder:test")
    assert claimed is not None
    assert kb.request_review(
        conn, child_id,
        summary="Implemented and verified; awaiting independent review",
        reviewer="reviewer",
        expected_run_id=claimed.current_run_id,
    )
    parent_id = kb.create_task(conn, title="Parent slice", assignee="builder")
    kb.link_tasks(conn, parent_id, child_id)
    assert _status(conn, child_id) == "review"  # open, but evidence-bearing

    assert kb.complete_task(conn, parent_id)
    assert _status(conn, parent_id) == "done"


# 7. Multiple open evidence-less children -> error names EVERY child.
def test_multiple_open_children_all_named_in_error(conn):
    parent_id, children = _parent_with_open_children(conn, count=3)

    with pytest.raises(kb.OpenChildrenEvidenceError) as excinfo:
        kb.complete_task(conn, parent_id)

    message = str(excinfo.value)
    for child_id in children:
        assert child_id in message
    assert excinfo.value.parent_id == parent_id
    assert len(excinfo.value.children) == 3
    assert _status(conn, parent_id) == "ready"


# 8. Integration: rejection leaves the scratch DB file untouched on reopen.
def test_rejected_complete_leaves_scratch_db_unchanged(conn, tmp_path):
    db_path = tmp_path / DB_PATH_NAME
    parent_id, (child_id,) = _parent_with_open_children(conn)

    with pytest.raises(kb.OpenChildrenEvidenceError):
        kb.complete_task(conn, parent_id, result="")

    assert db_path.exists()  # the scratch file is the only DB in play
    reopened = kbc.connect(db_path)
    try:
        parent = kb.get_task(reopened, parent_id)
        assert parent is not None
        assert parent.status == "ready"  # untouched, still in flight
        assert parent.result is None
        assert parent.completed_at is None
        assert _status(reopened, child_id) == "todo"
    finally:
        reopened.close()


# 9. Whitespace-only evidence is still "empty" — the gate strips before judging.
def test_whitespace_only_result_and_summary_still_blocked(conn):
    parent_id, _ = _parent_with_open_children(conn)

    with pytest.raises(kb.OpenChildrenEvidenceError):
        kb.complete_task(conn, parent_id, result="   ", summary="   ")
    assert _status(conn, parent_id) == "ready"


# 10. The override must be a non-empty STRING: "" / 123 do not unlock.
def test_allow_open_children_override_must_be_nonempty_string(conn):
    parent_id, _ = _parent_with_open_children(conn)

    with pytest.raises(kb.OpenChildrenEvidenceError):
        kb.complete_task(conn, parent_id, metadata={"allow_open_children": ""})
    with pytest.raises(kb.OpenChildrenEvidenceError):
        kb.complete_task(conn, parent_id, metadata={"allow_open_children": 123})
    assert _status(conn, parent_id) == "ready"


# 11. Same for the supersede carve-out: "" is absent, not a supersede.
def test_superseded_by_empty_string_does_not_pass(conn):
    parent_id, _ = _parent_with_open_children(conn)

    with pytest.raises(kb.OpenChildrenEvidenceError):
        kb.complete_task(conn, parent_id, metadata={"superseded_by": ""})
    assert _status(conn, parent_id) == "ready"


# 12. The supersede carve-out must be a non-empty STRING, like
#     ``allow_open_children``: falsy non-strings (0, False, [], {}) do not
#     unlock. A non-empty string still passes (test 3).
def test_superseded_by_non_string_values_do_not_pass(conn):
    parent_id, _ = _parent_with_open_children(conn)

    for bogus in (0, False, [], {}):
        with pytest.raises(kb.OpenChildrenEvidenceError):
            kb.complete_task(conn, parent_id, metadata={"superseded_by": bogus})
    assert _status(conn, parent_id) == "ready"


# 13. Archived children are terminal — an empty parent close passes.
def test_archived_child_does_not_block_empty_complete(conn):
    parent_id = kb.create_task(conn, title="Parent slice", assignee="builder")
    child_id = kb.create_task(conn, title="Child step 1", assignee="builder", parents=[parent_id])
    assert kb.archive_task(conn, child_id)
    assert _status(conn, child_id) == "archived"

    assert kb.complete_task(conn, parent_id)
    assert _status(conn, parent_id) == "done"


# 14. A parent with real evidence completes even while children are open
#     (the gate only ever judges EMPTY evidence).
def test_parent_with_real_result_completes_despite_open_child(conn):
    parent_id, _ = _parent_with_open_children(conn)

    assert kb.complete_task(conn, parent_id, result="Shipped: outcome captured on the parent")
    parent = kb.get_task(conn, parent_id)
    assert parent is not None
    assert parent.status == "done"
    assert parent.result == "Shipped: outcome captured on the parent"


# 15. Re-complete of an already-done task returns False — the status guard
#     keeps the gate from ever raising on a terminal task.
def test_recomplete_of_done_parent_returns_false_without_raising(conn):
    parent_id = kb.create_task(conn, title="Parent slice", assignee="builder")
    assert kb.complete_task(conn, parent_id)  # no children yet
    assert _status(conn, parent_id) == "done"
    # Open evidence-less child appears AFTER the parent closed (promoted to
    # ready because its parent is done).
    child_id = kb.create_task(conn, title="Child step 1", assignee="builder", parents=[parent_id])
    assert _status(conn, child_id) == "ready"

    assert kb.complete_task(conn, parent_id) is False  # not a raise
    assert _status(conn, parent_id) == "done"


# 16. A dangling link (child row missing) is skipped, not fatal.
def test_dangling_child_link_is_skipped(conn):
    parent_id = kb.create_task(conn, title="Parent slice", assignee="builder")
    kb._link(conn, parent_id, "t_dangling0000ff")  # no tasks row for this id

    assert kb.complete_task(conn, parent_id)
    assert _status(conn, parent_id) == "done"


# 17. Audit accuracy: allow_open_children_reason rides ONLY the override path.
def test_completed_event_payload_carries_reason_only_for_override(conn):
    parent_id, _ = _parent_with_open_children(conn)
    reason = "deferred to follow-up card"
    assert kb.complete_task(conn, parent_id, metadata={"allow_open_children": reason})
    overridden = [e for e in kb.list_events(conn, parent_id) if e.kind == "completed"][-1]
    assert overridden.payload["allow_open_children_reason"] == reason

    child_id = _done_child(conn, "Child step 2")
    plain_id = kb.create_task(conn, title="Plain slice", assignee="builder")
    kb.link_tasks(conn, plain_id, child_id)
    assert kb.complete_task(conn, plain_id)  # normal empty close, all children done
    plain = [e for e in kb.list_events(conn, plain_id) if e.kind == "completed"][-1]
    assert "allow_open_children_reason" not in plain.payload
