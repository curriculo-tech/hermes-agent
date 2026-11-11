# plan.md — loop-gate-noop

Locate code first (read-only): readlink -f $(which hermes); grep the installed package for the kanban complete handler; event payload key `result_len` is a known anchor.

Steps:
1. Find the complete handler + child-listing code in the kanban kernel. No edits outside a worktree.
2. Worktree per ladder law (herdr worktree create). NEVER edit against the live ~/.hermes/kanban.db — tests use a scratch kanban.db fixture in a temp dir.
3. Gate logic in the complete path:
   - list children (existing helpers, or one additive query over the existing schema — no migration).
   - open_empty = children with status not in (done, archived) AND empty summary AND empty result.
   - if open_empty non-empty:
       * metadata.superseded_by present -> pass (carve-out).
       * elif metadata.allow_open_children is a non-empty string -> pass (keep reason in event payload if cheap).
       * else -> reject; error names EVERY open_empty child (id + status) — never dead-lock silently.
   - all children done/archived -> no gating; parent empty result still completes.
   - never reject non-empty evidence.
4. Reuse existing payload keys; result_len computed the same way the kernel already logs it. Any new event kind is additive only; no kanban.db schema migration.
5. Tests (repo's existing runner): unit per branch — children pending + empty -> reject w/ ids; + allow_open_children -> pass; + superseded_by -> pass; all children done + empty parent -> pass; children done + non-empty parent -> unchanged behavior; plus one integration test on a scratch kanban.db fixture.

Constraints: stdlib-only, no new deps. Scope: complete path only — dispatcher, spawnability, plugin fixes untouched (t_ca1c4a33 OUT OF SCOPE list stands).

Deferred (NOT this slice, from t_ca1c4a33 full spec): tracking-only body marker, confirm_no_evidence P1 override, no_evidence_completion event. Follow-up slice if the lead wants them.
