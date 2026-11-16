"""``complete_task`` refuses empty / whitespace-only evidence (#117483)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
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


def _ready_task(conn) -> str:
    tid = kb.create_task(conn, title="demo", assignee="coder")
    assert kb.claim_task(conn, tid, claimer=kb._claimer_id()) is not None
    return tid


def _parent_with_open_child(conn) -> tuple[str, str]:
    parent = _ready_task(conn)
    child = kb.create_task(conn, title="child", assignee="coder")
    kb.link_tasks(conn, parent, child)
    return parent, child


def test_complete_without_evidence_raises(conn):
    tid = _ready_task(conn)
    with pytest.raises(kb.EmptyCompletionError):
        kb.complete_task(conn, tid)
    row = conn.execute("SELECT status, result FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert row["status"] == "running"
    assert not row["result"]
    kinds = [r["kind"] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,)
    ).fetchall()]
    assert "completion_blocked_empty_result" in kinds
    assert "completed" not in kinds


def test_whitespace_result_raises(conn):
    tid = _ready_task(conn)
    with pytest.raises(kb.EmptyCompletionError):
        kb.complete_task(conn, tid, result="   ")
    assert conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()["status"] == "running"


def test_whitespace_stored_result_does_not_count(conn):
    tid = _ready_task(conn)
    conn.execute("UPDATE tasks SET result = ? WHERE id = ?", ("  ", tid))
    conn.commit()
    with pytest.raises(kb.EmptyCompletionError):
        kb.complete_task(conn, tid)
    assert kb.complete_task(conn, tid, result="shipped the gate") is True
    assert conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()["status"] == "done"


def test_review_approval_exempt(conn):
    tid = kb.create_task(conn, title="review-me", assignee="coder")
    assert kb.claim_task(conn, tid, claimer=kb._claimer_id()) is not None
    assert kb.request_review(conn, tid, summary="please look") is True
    assert kb.get_task(conn, tid).status == "review"
    assert kb.complete_task(conn, tid) is True
    assert kb.get_task(conn, tid).status == "done"


def test_reject_then_succeed(conn):
    tid = _ready_task(conn)
    with pytest.raises(kb.EmptyCompletionError):
        kb.complete_task(conn, tid, summary="   ")
    assert kb.complete_task(conn, tid, summary="fixed empty complete") is True
    assert conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()["status"] == "done"


def test_empty_parent_with_open_empty_children_raises_and_lists_each_child(conn):
    parent, first = _parent_with_open_child(conn)
    second = kb.create_task(conn, title="second child", assignee="coder")
    kb.link_tasks(conn, parent, second)

    with pytest.raises(kb.OpenChildrenCompletionError) as exc_info:
        kb.complete_task(conn, parent)

    assert exc_info.value.children == sorted([(first, "todo"), (second, "todo")])
    assert first in str(exc_info.value)
    assert second in str(exc_info.value)
    assert kb.get_task(conn, parent).status == "running"


def test_allow_open_children_reason_allows_empty_parent(conn):
    parent, _ = _parent_with_open_child(conn)
    reason = "children intentionally start after handoff"

    assert kb.complete_task(
        conn,
        parent,
        metadata={"allow_open_children": reason},
    )
    run = conn.execute(
        "SELECT metadata FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (parent,),
    ).fetchone()
    assert reason in run["metadata"]


def test_blank_allow_open_children_reason_does_not_override(conn):
    parent, _ = _parent_with_open_child(conn)

    with pytest.raises(kb.OpenChildrenCompletionError):
        kb.complete_task(conn, parent, metadata={"allow_open_children": "  "})


def test_superseded_by_presence_allows_empty_parent(conn):
    parent, _ = _parent_with_open_child(conn)

    assert kb.complete_task(conn, parent, metadata={"superseded_by": None})


def test_empty_parent_with_all_children_done_passes(conn):
    parent = _ready_task(conn)
    done_child = _ready_task(conn)
    assert kb.request_review(conn, done_child, summary="human review")
    assert kb.complete_task(conn, done_child)
    archived_child = kb.create_task(conn, title="archived child", assignee="coder")
    assert kb.archive_task(conn, archived_child)
    kb.link_tasks(conn, parent, done_child)
    kb.link_tasks(conn, parent, archived_child)

    assert kb.complete_task(conn, parent)
    assert kb.get_task(conn, parent).status == "done"


def test_nonempty_parent_with_open_empty_child_passes(conn):
    parent, _ = _parent_with_open_child(conn)

    assert kb.complete_task(conn, parent, result="implemented parent work")


def test_cli_reports_open_empty_child_from_scratch_db(conn, capsys):
    parent, child = _parent_with_open_child(conn)

    rc = kanban_cli._cmd_complete(
        argparse.Namespace(
            task_ids=[parent], summary=None, result=None, metadata=None, force=False
        )
    )

    assert rc == 1
    output = capsys.readouterr()
    assert child in output.err + output.out
    assert "todo" in output.err + output.out
