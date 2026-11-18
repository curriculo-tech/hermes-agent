"""Intake gate: a card must name a dispatcher profile or a registered persona.

The dispatcher only claims cards carrying its own profile, and personas are the
fleet-wake lanes — so an empty or unknown assignee parks in ``ready`` forever
(``skipped_nonspawnable``). Both intake surfaces (the CLI and the
``kanban_create`` tool) reject such cards at creation instead. Storage
(``create_task``/``assign_task``) and claim wiring stay untouched, so the
tests reach the board through the real command paths only.
"""
import argparse
import json
from pathlib import Path

import pytest


@pytest.fixture
def gate_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb
    kb.init_db()
    return home


def _run_cli(monkeypatch, capsys, *argv):
    """Drive the real ``hermes kanban`` parser; return ``(rc, stdout, stderr)``."""
    from hermes_cli import kanban as kc
    parser = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(parser.add_subparsers())
    rc = kc.kanban_command(parser.parse_args(["kanban", *argv]))
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _board_tasks():
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        return kb.list_tasks(conn, limit=100)


def test_cli_create_rejects_empty_assignee(gate_home, monkeypatch, capsys):
    rc, _out, err = _run_cli(monkeypatch, capsys, "create", "probe", "--json")
    assert rc == 2
    assert "assignee is required" in err
    assert "default" in err  # the message names at least one valid target
    assert _board_tasks() == []


def test_cli_create_rejects_unknown_assignee(gate_home, monkeypatch, capsys):
    rc, _out, err = _run_cli(monkeypatch, capsys, "create", "probe", "--assignee", "ghost-lane")
    assert rc == 2
    assert "'ghost-lane'" in err
    assert "would never dispatch" in err
    assert "default" in err  # a known-good name shows the caller what works
    assert _board_tasks() == []


def test_cli_create_accepts_registered_persona(gate_home, monkeypatch, capsys):
    persona = gate_home / "personas" / "on-call"
    persona.mkdir(parents=True)
    (persona / "SOUL.md").write_text("# on-call\n", encoding="utf-8")
    rc, out, err = _run_cli(monkeypatch, capsys, "create", "wake the lane",
                            "--assignee", "on-call", "--json")
    assert rc == 0, err
    assert json.loads(out)["assignee"] == "on-call"


def test_cli_create_accepts_dispatcher_profile(gate_home, monkeypatch, capsys):
    profile = gate_home / "profiles" / "nixclaw"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("# profile\n", encoding="utf-8")
    rc, out, err = _run_cli(monkeypatch, capsys, "create", "dispatch me",
                            "--assignee", "nixclaw", "--json")
    assert rc == 0, err
    assert json.loads(out)["assignee"] == "nixclaw"


def test_tool_create_rejects_unknown_assignee(gate_home):
    """The agent-tool surface shares the gate: an undead card never reaches the board."""
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_create({"title": "probe", "assignee": "ghost-lane"}))
    assert "error" in out, out
    assert "'ghost-lane'" in out["error"]
    assert "would never dispatch" in out["error"]
    assert _board_tasks() == []


def _seed_task(assignee):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        return kb.create_task(conn, title="gate probe", assignee=assignee)


def test_cli_assign_rejects_unknown_assignee(gate_home, monkeypatch, capsys):
    tid = _seed_task("default")
    rc, _out, err = _run_cli(monkeypatch, capsys, "assign", tid, "ghost-lane")
    assert rc == 2
    assert "would never dispatch" in err
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).assignee == "default"


def test_cli_assign_none_still_unassigns(gate_home, monkeypatch, capsys):
    tid = _seed_task("default")
    rc, _out, err = _run_cli(monkeypatch, capsys, "assign", tid, "none")
    assert rc == 0, err
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).assignee is None


def test_cli_reassign_rejects_unknown_assignee(gate_home, monkeypatch, capsys):
    tid = _seed_task("default")
    rc, _out, err = _run_cli(monkeypatch, capsys, "reassign", tid, "ghost-lane")
    assert rc == 2
    assert "would never dispatch" in err
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).assignee == "default"
