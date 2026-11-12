# acceptance.md — loop-gate-noop

All four grep-able checks pass on the slice worktree:
1. OPEN-CHILD+EMPTY -> ERROR: complete of a parent with >=1 un-done child having empty summary+result is rejected; error text lists each child id + status.
2. OVERRIDE -> PASS: same setup + metadata.allow_open_children="<reason>" completes.
3. SUPERSEDE -> PASS: same setup + metadata.superseded_by present completes.
4. ALL-DONE+EMPTY -> PASS: parent with every child done/archived and empty result completes (legitimate empty close not broken).

Plus:
- no schema migration (kanban.db structurally unchanged)
- stdlib-only diff, no new deps
- unit test per branch above + 1 integration test on a scratch kanban.db fixture (never the live db)
- complete path only: dispatcher untouched

Evidence on the card before complete: test run output (branch names + results), gate code path (file:line).
