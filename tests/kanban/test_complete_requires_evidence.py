"""Open-children gate on ``complete_task`` (loop-gate-noop, t_ca1c4a33).

A parent may not complete with NO evidence while linked children that never
started (not done/archived, empty result, empty latest-run summary) are open —
unless ``metadata.superseded_by`` or a non-empty string
``metadata.allow_open_children`` carries an explicit carve-out.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


ROOT = Path(__file__).parents[2]


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB (mirrors test_kanban_db.py)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _make_parent_with_todo_child(conn) -> tuple[str, str]:
    """Parent (ready) with one linked child parked in todo, no evidence."""
    parent = kb.create_task(conn, title="parent task")
    child = kb.create_task(conn, title="child task", parents=[parent])
    return parent, child


def _make_parent_with_done_children(conn, n: int = 2) -> tuple[str, list[str]]:
    """Parent whose children were all completed before being linked.

    Children complete standalone (no children of their own -> no gate), then
    link to the parent; linking never demotes a done child.
    """
    parent = kb.create_task(conn, title="parent task")
    kids = [kb.create_task(conn, title=f"child task {i}") for i in range(n)]
    for kid in kids:
        assert kb.complete_task(conn, kid)
    for kid in kids:
        kb.link_tasks(conn, parent, kid)
    return parent, kids


# ---------------------------------------------------------------------------
# A: open child, empty evidence everywhere -> refuse, name the child
# ---------------------------------------------------------------------------


def test_open_child_with_empty_evidence_blocks_completion(kanban_home):
    with kbc.connect() as conn:
        parent, child = _make_parent_with_todo_child(conn)
        with pytest.raises(kb.OpenChildrenError) as exc_info:
            kb.complete_task(conn, parent)
        assert isinstance(exc_info.value, ValueError)
        msg = str(exc_info.value)
        assert f"{child} (todo)" in msg
        assert parent in msg
        # Refused before any write txn: nothing changed.
        assert kb.get_task(conn, parent).status == "ready"
        assert kb.get_task(conn, child).status == "todo"


def test_multiple_open_children_all_named_and_nothing_changes(kanban_home):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent task")
        kids = [
            kb.create_task(conn, title=f"child task {i}", parents=[parent])
            for i in range(2)
        ]
        with pytest.raises(kb.OpenChildrenError) as exc_info:
            kb.complete_task(conn, parent)
        msg = str(exc_info.value)
        for kid in kids:
            assert f"{kid} (todo)" in msg
        assert "2 open child task(s)" in msg
        assert kb.get_task(conn, parent).status == "ready"
        assert all(kb.get_task(conn, kid).status == "todo" for kid in kids)


def test_terminal_parent_with_open_child_noops_instead_of_refusing(kanban_home):
    """Completing an already-done id is a plain noop (False) even when an open
    evidence-less child was linked after it closed — the CAS accepts only
    running/ready/blocked/review, so the gate must not fire for it."""
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent task")
        assert kb.complete_task(conn, parent)  # done before any child exists
        kid = kb.create_task(conn, title="late child task", parents=[parent])
        kid_status = kb.get_task(conn, kid).status  # ready: its parent is done
        assert kb.complete_task(conn, parent) is False
        assert kb.get_task(conn, parent).status == "done"
        assert kb.get_task(conn, kid).status == kid_status


def test_blank_allow_open_children_is_not_an_override(kanban_home):
    with kbc.connect() as conn:
        parent, _child = _make_parent_with_todo_child(conn)
        for value in ("", "   "):
            with pytest.raises(kb.OpenChildrenError):
                kb.complete_task(conn, parent, metadata={"allow_open_children": value})


def test_nonempty_evidence_with_open_child_completes(kanban_home):
    with kbc.connect() as conn:
        parent, child = _make_parent_with_todo_child(conn)
        assert kb.complete_task(conn, parent, result="evidence that stands alone")
        assert kb.get_task(conn, parent).status == "done"
        # Child untouched by the gate itself; recompute_ready promotes it
        # todo -> ready now that its parent is done.
        assert kb.get_task(conn, child).status == "ready"


# ---------------------------------------------------------------------------
# B: explicit allow_open_children override
# ---------------------------------------------------------------------------


def test_allow_open_children_override_completes(kanban_home):
    with kbc.connect() as conn:
        parent, _child = _make_parent_with_todo_child(conn)
        assert kb.complete_task(
            conn, parent, metadata={"allow_open_children": "waiving for test"},
        )
        assert kb.get_task(conn, parent).status == "done"


# ---------------------------------------------------------------------------
# C: supersede carve-out
# ---------------------------------------------------------------------------


def test_superseded_by_override_completes(kanban_home):
    with kbc.connect() as conn:
        parent, _child = _make_parent_with_todo_child(conn)
        assert kb.complete_task(conn, parent, metadata={"superseded_by": "t_super"})
        assert kb.get_task(conn, parent).status == "done"


# ---------------------------------------------------------------------------
# D: all children done + empty parent result -> legitimate empty close
# ---------------------------------------------------------------------------


def test_all_children_done_empty_parent_completes(kanban_home):
    with kbc.connect() as conn:
        parent, kids = _make_parent_with_done_children(conn)
        assert kb.complete_task(conn, parent)
        assert kb.get_task(conn, parent).status == "done"
        assert all(kb.get_task(conn, kid).status == "done" for kid in kids)


# ---------------------------------------------------------------------------
# E: children done + non-empty evidence -> unchanged behavior, result persists
# ---------------------------------------------------------------------------


def test_all_children_done_with_evidence_persists_result(kanban_home):
    with kbc.connect() as conn:
        parent, _kids = _make_parent_with_done_children(conn)
        assert kb.complete_task(conn, parent, result="evidence")
        assert kb.get_task(conn, parent).status == "done"
        assert kb.get_task(conn, parent).result == "evidence"


# ---------------------------------------------------------------------------
# F: CLI integration on a scratch kanban.db
# ---------------------------------------------------------------------------


def _run_hermes(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["HERMES_KANBAN_HOME"] = str(home)
    for name in (
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_DELEGATED_CHILD_CONTEXT",
    ):
        env.pop(name, None)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_cli_complete_open_child_nonzero_then_override_zero(kanban_home):
    with kbc.connect() as conn:
        parent, child = _make_parent_with_todo_child(conn)

    refused = _run_hermes(kanban_home, "kanban", "complete", parent)
    assert refused.returncode != 0
    assert child in refused.stderr
    assert "(todo)" in refused.stderr
    assert "no state change" in refused.stderr

    overridden = _run_hermes(
        kanban_home, "kanban", "complete", parent,
        "--metadata", '{"allow_open_children":"reason"}',
    )
    assert overridden.returncode == 0, overridden.stderr
