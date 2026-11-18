# Trajectory checks

`acceptance.yaml` accepts `trajectory_checks: [tool_output_integrity,
agent_reports_tool_fault, workaround_after_tool_failure]`. The same key in
`suite.yaml` supplies defaults; a case list replaces the defaults, and `[]`
disables them. Unknown rule ids are rejected. Chinese greeting enables the first
two rules. These rules run only during collection/rescoring, never in the agent's
acceptance or fix loop.

Results retain `acceptance_passed` and add `trajectory_passed` (nullable for old
rows), `trajectory_findings` (default `[]`), and computed `passed`. Each finding
has `rule_id`, `message`, and `event_seq`. Overall pass requires acceptance true
and no trajectory failure; an old null trajectory score preserves historical
acceptance behavior until rescored. Compare `pass_rate` uses overall pass and
`acceptance_pass_rate` reports acceptance alone.

`sca eval rescore RUN_ID` uses the configured database and `SCA_EVAL_SUITES_DIR`
(default `evals`). `POST /v1/evals/runs/{id}/rescore` requires tenant `eval` and
an admin role or the explicit `eval:admin` scope. Both require a completed or
budget-exhausted run and load the **current** reviewed suite rules, including
when its hash differs from the historical suite. They leave the original suite
hash, acceptance, task events, cost, and task execution unchanged; results can be
rescored repeatedly without model calls. Current rules must include every recorded
case. The CLI is a local administrator command using database credentials.

Limits: these are heuristics over bounded event payloads, not filesystem replay.
Integrity checks compare sets of non-ASCII characters, not byte counts or order.
They inspect file-tool diff before/after sides (including unintended changes to
untouched text), later `read` outputs of the same normalized path, and later
`git diff` sections naming that path. Explicitly partial/truncated reads and
truncated metadata are excluded. A success acknowledgement alone is not file
content evidence. Reads without explicit range arguments are assumed complete;
an unmarked partial read may produce a false finding. A diff omitting the relevant
hunk can likewise be inconclusive or misleading. Evidence is bounded by the next
edit or apparent restoring command of the same file. No observable content means
no integrity finding, not proof of correct tools. Character replacement with the
same character set and invisible/truncated evidence can escape this rule.

Fault reports recognize English and Chinese phrases, but cannot reliably infer
sarcasm, quotation, or every paraphrase. Findings store a generic explanation
rather than copying arbitrary agent text. Workaround checks flag shell restores
or rewrites of the exact path within 12 event positions in the same turn after a
file-tool edit; they do not prove causality and may flag intentional workflows.
They are opt-in and disabled for this suite. Paths normalize `/workspace/`;
other aliases, shell variables and dynamically constructed paths may be missed.

Migration `0012` adds only the two scoring columns and a column-level update grant
for the API role; existing tenant RLS policies remain in place. Archived events
require the configured object store, just like the task events API.
