# LOOP.md — loop-gate-noop (t_ca1c4a33, P1)

Mandate: kanban kernel rejects empty-evidence parent `complete` while un-started children exist; supersede carve-out passes.

Locked decisions (round 552, grok-planner; no lock gaps):
1. REJECT RULE — `complete` -> reject with error listing child ids when >=1 child has summary+result both empty (equivalently result_len==0 with empty summary) AND status != done.
2. OVERRIDE — metadata.allow_open_children: "<reason>" (explicit non-empty string; never default-on).
3. SUPERSEDE CARVE-OUT — metadata.superseded_by present -> pass (covers the t_17e0d3e2 supersede-close case).
4. SCOPE — complete path only; no dispatcher changes.

Forbidden: no gating of normal parents when children are done; never reject non-empty results; no gating at child-done time.

Acceptance (grep-able): open-child+empty -> error; allow_open_children -> pass; superseded_by -> pass; all children done + empty parent result -> pass (legitimate empty close survives).
