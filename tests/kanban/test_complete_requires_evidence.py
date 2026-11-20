"""``complete_task`` refuses an EMPTY-EVIDENCE parent completion while a
child is neither terminal nor carrying evidence (loop-gate-noop, narrowed).

Locked spec lines: "empty-evidence parent complete" and "never reject
non-empty evidence" — a completion with a substantive ``result``/``summary``
always goes through (worker/spawn handoffs, swarm stages, deferred sweeps);
only an evidence-less completion of a parent with open evidence-less
children is refused, with ``superseded_by`` / ``allow_open_children``
carve-outs."""

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


_STORED_EVIDENCE = "prior run recorded this outcome"


def _claimed_parent_with_stored_evidence(conn, title: str = "parent") -> str:
    """Claimed (``running``) parent carrying stored evidence from a prior
    run: the sibling empty-evidence gate stays quiet, so a test completing
    it with no ``result``/``summary`` isolates the open-children gate."""
    parent = kb.create_task(conn, title=title, assignee="coder")
    assert kb.claim_task(conn, parent, claimer=kb._claimer_id()) is not None
    conn.execute("UPDATE tasks SET result = ? WHERE id = ?", (_STORED_EVIDENCE, parent))
    conn.commit()
    return parent


def _parent_with_open_children(conn, *, stored_evidence: bool = True) -> tuple[str, dict[str, str]]:
    """Claimed (``running``) parent with one ``todo`` and one ``blocked``
    child, neither carrying evidence.

    With ``stored_evidence`` (default) the parent also carries a substantive
    ``tasks.result`` from a prior run, so the sibling empty-evidence gate
    stays quiet and the tests isolate THIS gate: the completion call itself
    brings no ``result``/``summary`` unless a test explicitly passes one."""
    if stored_evidence:
        parent = _claimed_parent_with_stored_evidence(conn)
    else:
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


def test_open_child_empty_completion_rejects_and_names_children(conn):
    """Reject: NO result and NO summary on the completion + open evidence-less
    children. Every child is named ``id (status)``, sorted by id; the task is
    unmutated and the refusal is auditable."""
    parent, kids = _parent_with_open_children(conn)
    with pytest.raises(kb.OpenChildrenWithoutEvidenceError) as ei:
        kb.complete_task(conn, parent)
    assert not isinstance(ei.value, kb.EmptyCompletionError)
    msg = str(ei.value)
    for child_id, status in kids.items():
        assert f"{child_id} ({status})" in msg
    assert ei.value.children == sorted(kids.items())
    row = conn.execute("SELECT status, result FROM tasks WHERE id = ?", (parent,)).fetchone()
    assert row["status"] == "running"
    assert row["result"] == _STORED_EVIDENCE
    kinds = _event_kinds(conn, parent)
    assert "completion_blocked_open_children" in kinds
    assert "completed" not in kinds


def test_open_child_no_stored_evidence_still_names_children(conn):
    """Parent with NO evidence anywhere (call or stored): the open-children
    gate runs first, so the child-naming refusal wins over the generic
    empty-evidence one."""
    parent, kids = _parent_with_open_children(conn, stored_evidence=False)
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
    sibling empty-evidence gate; the narrowed children gate must not
    swallow it."""
    tid = kb.create_task(conn, title="leaf", assignee="coder")
    assert kb.claim_task(conn, tid, claimer=kb._claimer_id()) is not None
    with pytest.raises(kb.EmptyCompletionError):
        kb.complete_task(conn, tid)
    assert kb.get_task(conn, tid).status == "running"
    kinds = _event_kinds(conn, tid)
    assert "completion_blocked_empty_result" in kinds
    assert "completed" not in kinds


@pytest.mark.parametrize("field", ["result", "summary"])
def test_substantive_parent_evidence_completes_with_open_child(conn, field):
    """Never reject non-empty evidence: an open evidence-less child must NOT
    block a parent whose completion carries a substantive result or summary —
    the normal worker/spawn and stage-handoff flow."""
    parent, _kids = _parent_with_open_children(conn)
    evidence = {field: "shipped the slice; follow-up child tracks the rest"}
    assert kb.complete_task(conn, parent, **evidence) is True
    assert kb.get_task(conn, parent).status == "done"
    kinds = _event_kinds(conn, parent)
    assert "completed" in kinds
    assert "completion_blocked_open_children" not in kinds


def test_whitespace_only_parent_evidence_still_gated(conn):
    """Whitespace-only result/summary counts as empty: the gate still fires."""
    parent, _kids = _parent_with_open_children(conn)
    with pytest.raises(kb.OpenChildrenWithoutEvidenceError):
        kb.complete_task(conn, parent, result="   ", summary="  \n\t ")
    assert kb.get_task(conn, parent).status == "running"


def test_open_child_allow_open_children_override_passes(conn):
    parent, _kids = _parent_with_open_children(conn)
    assert kb.complete_task(
        conn, parent,
        metadata={"allow_open_children": "children tracked on the follow-up card"},
    ) is True
    assert kb.get_task(conn, parent).status == "done"
    assert "completion_blocked_open_children" not in _event_kinds(conn, parent)


def test_open_child_superseded_by_passes(conn):
    parent, _kids = _parent_with_open_children(conn)
    assert kb.complete_task(
        conn, parent,
        metadata={"superseded_by": "t_17e0d3e2"},
    ) is True
    assert kb.get_task(conn, parent).status == "done"
    assert "completion_blocked_open_children" not in _event_kinds(conn, parent)


@pytest.mark.parametrize(
    "how", ["call_summary", "no_call_evidence"],
)
def test_all_children_done_empty_parent_completes(conn, how):
    """Legitimate empty close: with every child terminal the parent completes
    with an empty ``tasks.result`` — via a summary, or with no call evidence
    at all when the stored result already vouches."""
    parent = kb.create_task(conn, title="parent", assignee="coder")
    for i in (1, 2):
        child = kb.create_task(conn, title=f"child {i}", assignee="coder")
        assert kb.claim_task(conn, child, claimer=kb._claimer_id()) is not None
        assert kb.complete_task(conn, child, result=f"child {i} shipped") is True
        assert kb.link_tasks(conn, parent, child) is not None
    assert kb.claim_task(conn, parent, claimer=kb._claimer_id()) is not None
    if how == "no_call_evidence":
        conn.execute("UPDATE tasks SET result = ? WHERE id = ?", (_STORED_EVIDENCE, parent))
        conn.commit()
        assert kb.complete_task(conn, parent) is True
    else:
        assert kb.complete_task(conn, parent, summary="children covered everything") is True
    row = conn.execute("SELECT status, result FROM tasks WHERE id = ?", (parent,)).fetchone()
    assert row["status"] == "done"
    assert not row["result"]
    assert "completion_blocked_open_children" not in _event_kinds(conn, parent)


def test_integration_scratch_db(tmp_path, monkeypatch):
    """End-to-end on a scratch kanban.db under tmp_path (never the live DB):
    reject, then the ``allow_open_children`` override completes the same
    parent."""
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
        parent, kids = _parent_with_open_children(conn)
        with pytest.raises(kb.OpenChildrenWithoutEvidenceError) as ei:
            kb.complete_task(conn, parent)
        for child_id, status in kids.items():
            assert f"{child_id} ({status})" in str(ei.value)
        assert kb.get_task(conn, parent).status == "running"
        assert kb.complete_task(
            conn, parent,
            metadata={"allow_open_children": "children moved to the follow-up card"},
        ) is True
        assert kb.get_task(conn, parent).status == "done"
        kinds = _event_kinds(conn, parent)
        assert "completion_blocked_open_children" in kinds
        assert "completed" in kinds


def test_open_child_with_result_evidence_not_gated(conn):
    """An open child carrying a substantive ``tasks.result`` exempts it from
    the gate (parent completes with no call evidence, so only the child's
    evidence can save it)."""
    parent = _claimed_parent_with_stored_evidence(conn)
    blocker = kb.create_task(conn, title="child with findings", assignee="coder")
    conn.execute(
        "UPDATE tasks SET result = ? WHERE id = ?", ("child shipped its slice", blocker)
    )
    conn.commit()
    assert kb.link_tasks(conn, parent, blocker) is not None
    assert kb.complete_task(conn, parent) is True
    assert kb.get_task(conn, parent).status == "done"
    assert "completion_blocked_open_children" not in _event_kinds(conn, parent)


def test_open_child_run_summary_evidence_not_gated(conn):
    """A closed run summary counts as evidence even with an empty task
    result; the parent completes with no call evidence of its own."""
    parent = _claimed_parent_with_stored_evidence(conn)
    evidence_child = kb.create_task(conn, title="investigated child", assignee="coder")
    assert kb.claim_task(conn, evidence_child, claimer=kb._claimer_id()) is not None
    assert kb.complete_task(conn, evidence_child, summary="evidence: reviewed") is True
    conn.execute(
        "UPDATE tasks SET status = 'todo', result = '' WHERE id = ?", (evidence_child,)
    )
    conn.commit()
    assert kb.link_tasks(conn, parent, evidence_child) is not None
    assert kb.complete_task(conn, parent) is True
    assert kb.get_task(conn, parent).status == "done"


def test_archived_child_not_gated(conn):
    parent = _claimed_parent_with_stored_evidence(conn)
    archived = kb.create_task(conn, title="archived child", assignee="coder")
    assert kb.archive_task(conn, archived)
    assert kb.link_tasks(conn, parent, archived) is not None
    assert kb.complete_task(conn, parent) is True
    assert kb.get_task(conn, parent).status == "done"


@pytest.mark.parametrize("bad_override", ["", "   ", 0, False, ["reason"]])
def test_allow_open_children_requires_nonempty_string(conn, bad_override):
    """Override is opt-in and must be an explicit non-empty string reason."""
    parent, _kids = _parent_with_open_children(conn)
    with pytest.raises(kb.OpenChildrenWithoutEvidenceError):
        kb.complete_task(
            conn, parent, metadata={"allow_open_children": bad_override},
        )
    assert kb.get_task(conn, parent).status == "running"


@pytest.mark.parametrize("blank", ["", "   "])
def test_superseded_by_blank_is_not_an_override(conn, blank):
    """``superseded_by`` must be a non-empty string; blank values must not
    bypass the gate (the real-string pass case is pinned above)."""
    parent, _kids = _parent_with_open_children(conn)
    with pytest.raises(kb.OpenChildrenWithoutEvidenceError):
        kb.complete_task(conn, parent, metadata={"superseded_by": blank})
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


def test_review_claimed_parent_with_open_child_completes(conn):
    """Regression: a reviewer that CLAIMED the review completes from
    ``running`` with an evidence-less call — the run's ``claimed`` event
    (``source_status=review``) keeps the approval exemption, the same
    evidence ``_retry_status_for_run`` reads. A plain implementation run of
    the same shape (stored evidence, open child, no call evidence) stays
    gated."""
    parent = _claimed_parent_with_stored_evidence(conn)
    kb.create_task(conn, title="child", assignee="coder", parents=[parent])
    assert kb.request_review(conn, parent, summary="implementation finished") is not None
    assert kb.get_task(conn, parent).status == "review"
    review = kb.claim_review_task(conn, parent)
    assert review is not None
    assert review.status == "running"
    assert kb.complete_task(
        conn, parent, expected_run_id=review.current_run_id,
    ) is True
    assert kb.get_task(conn, parent).status == "done"
    assert "completion_blocked_open_children" not in _event_kinds(conn, parent)

    # Control: same shape, but the run was claimed from ready — still gated.
    plain = _claimed_parent_with_stored_evidence(conn, title="plain parent")
    kb.create_task(conn, title="plain child", assignee="coder", parents=[plain])
    with pytest.raises(kb.OpenChildrenWithoutEvidenceError):
        kb.complete_task(conn, plain)
    assert kb.get_task(conn, plain).status == "running"


def test_already_done_parent_returns_false_without_raising(conn):
    """Re-complete of an already-done parent: terminal statuses are not
    gated — ``complete_task`` keeps its bare-False contract."""
    parent, _kids = _parent_with_open_children(conn)
    # Complete via ``result`` so the stored evidence survives the first
    # completion (a ``summary``-only completion leaves result NULL).
    assert kb.complete_task(conn, parent, result="done once") is True
    assert kb.complete_task(conn, parent) is False
    assert kb.get_task(conn, parent).status == "done"
    assert "completion_blocked_open_children" not in _event_kinds(conn, parent)


def test_todo_parent_returns_false_without_raising(conn):
    """A ``todo`` parent (waiting on its own parent) with an open child is
    out of the gate's scope: no raise, bare False (the unsatisfied-parent
    refusal answers first), no blocked event."""
    gate = kb.create_task(conn, title="gate", assignee="coder")
    parent = kb.create_task(conn, title="parent", assignee="coder", parents=[gate])
    kb.create_task(conn, title="child", assignee="coder", parents=[parent])
    conn.execute("UPDATE tasks SET result = ? WHERE id = ?", (_STORED_EVIDENCE, parent))
    conn.commit()
    assert kb.get_task(conn, parent).status == "todo"
    assert kb.complete_task(conn, parent) is False
    assert kb.get_task(conn, parent).status == "todo"
    assert "completion_blocked_open_children" not in _event_kinds(conn, parent)


@pytest.mark.parametrize("child_status", ["blocked", "ready", "running", "scheduled"])
def test_open_child_rejects_for_every_non_terminal_status(conn, child_status):
    """Every non-terminal child status trips the gate, and the refusal names
    each child as ``id (status)``. ``blocked`` comes from initial_status,
    ``running`` via a direct status write (a real claim is impossible under a
    running parent and ``link_tasks`` refuses running children),
    ``scheduled`` via ``schedule_task`` without a reason (a reason becomes the
    run summary — evidence — and would rightly pass)."""
    parent = _claimed_parent_with_stored_evidence(conn)
    if child_status == "blocked":
        child = kb.create_task(
            conn, title="child", assignee="coder", parents=[parent],
            initial_status="blocked",
        )
    else:
        child = kb.create_task(conn, title="child", assignee="coder", parents=[parent])
        if child_status == "scheduled":
            assert kb.schedule_task(conn, child)
        elif child_status == "running":
            conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (child,))
            conn.commit()
        elif child_status == "ready":
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child,))
            conn.commit()
    assert kb.get_task(conn, child).status == child_status
    with pytest.raises(kb.OpenChildrenWithoutEvidenceError) as ei:
        kb.complete_task(conn, parent)
    assert f"{child} ({child_status})" in str(ei.value)
    assert ei.value.children == [(child, child_status)]
    assert kb.get_task(conn, parent).status == "running"


def test_whitespace_only_child_evidence_still_blocks(conn):
    """Whitespace is not evidence: a child whose ``result`` is blank-only AND
    whose only run summary is blank-only still trips the gate. The run is
    opened by a claim, then the card is parked back on ``todo`` to link under
    the parent (``link_tasks`` refuses running children)."""
    parent = _claimed_parent_with_stored_evidence(conn)
    child = kb.create_task(conn, title="child", assignee="coder")
    assert kb.claim_task(conn, child, claimer=kb._claimer_id()) is not None
    conn.execute("UPDATE tasks SET status = 'todo', result = ? WHERE id = ?", ("   \n", child))
    conn.execute("UPDATE task_runs SET summary = '  ' WHERE task_id = ?", (child,))
    conn.commit()
    assert kb.link_tasks(conn, parent, child) is not None
    with pytest.raises(kb.OpenChildrenWithoutEvidenceError):
        kb.complete_task(conn, parent)
    assert kb.get_task(conn, parent).status == "running"
