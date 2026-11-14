# Context and coordination efficiency

Use this guide when choosing work units or when a task's history grows. Token
counts and cache hits describe usage, not a measured fraction of subscription
quota. Optimize total work, including dispatch, review and rework, and preserve
the quality and scope the user requested.

## Plan useful work units

Group several small related file checks into one bounded assignment. Split
work when it has independent deliverables or needs a different capability;
file count alone is not a reason to start more agents. Compare the extra
dispatch and integration effort with the work being moved out of the root.
The routing matrix still decides permitted models and efforts.

For each child, fill the contract's `context` and `returns` fields. Context is
a short brief plus source-of-truth paths, relevant sections or ranges, and the
interfaces required for the assignment. The return is a verdict, changed files
or artifact paths, the checks performed, actionable findings and blockers,
within a task-appropriate output budget. Put large results in a local artifact;
the root reads the summary and opens the evidence needed for its decision.
The contract does not require reproducing the full policy or task history:
include the applicable routing constraints and rights once, concisely.

Example: one worker checks eight related configuration files and returns a
pass/fail verdict, any findings with file/line references, and a path to its
full evidence. It does not need eight workers or eight reports pasted into
the root. A security-sensitive file can justify its own scoped review.

## Control root history

Batch independent reads, searches and status checks in a single tool round.
Select names, counts, ranges and summaries before printing large data. Read a
skill body or reference when it is relevant, then reuse the recorded evidence;
re-read when the source changes or a decision needs details. Scope MCP/tool
discovery to the capabilities needed for the task. Ordinary Swarm use does
not authorize changing installed skills, MCP connections or runtime settings.

At meaningful stage boundaries, write a short recovery record: objective and
scope, settled decisions and constraints, changed paths and artifact pointers,
completed verification, outstanding findings, next action and open agents.
Use host compaction or a subsequent stage's fresh context when appropriate.
Keep the underlying evidence available and account for the cold-context and
handoff cost. There is no universal token threshold or automatic context reset.

Keep the selected root model and effort. High is the usual recommendation for
ordinary future work; XHigh needs a task-specific reason. A high effort label
alone does not prove waste or quantify the saving from changing it.

## Wait and finish on evidence

While a worker runs, do independent useful work. When genuinely idle, use the
native interruptible wait within the host's progress-update limits. A status
poll or duplicate worker is not progress. Reconcile missing completion reports
with the existing agent rather than assigning its work again.

Use existing verification evidence for unchanged code. A changed implementation,
failed check, conflicting result or unresolved risk can justify more tests or
review. Required project checks still apply. Once the agreed acceptance criteria
are met and material findings are resolved, deliver; optional improvements go
to a concise follow-up note. Do not introduce a second review cycle solely to
repeat an already reviewed, unchanged result.
