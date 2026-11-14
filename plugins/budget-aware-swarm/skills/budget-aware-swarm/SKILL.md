---
name: budget-aware-swarm
description: >-
  Use only when the user explicitly invokes /budget-aware-swarm or
  /budget-aware-swarm:budget-aware-swarm, or names the Budget-Aware Swarm
  scheme in their request. Never select it on your own initiative, even for
  tasks that seem to benefit from parallel work or subagents. Applies only to
  that named scheme.
argument-hint: '[task]'
---

# Budget-Aware Swarm for Claude Code

Run this workflow only after explicit user invocation: the slash command or
the user naming the Budget-Aware Swarm scheme. Treat `$ARGUMENTS` as
the task. If it is empty, continue the clear task already in the conversation;
otherwise ask what task to swarm. This coordinator skill runs in the main
conversation. Its policy applies through completion of this task and its
descendants, not to unrelated work.

Apply the scheme directly to the user's actual task after the root gate.
Choose useful parallel work from that task; no separate demonstration or
preliminary trial is required. Size delegation by the routing matrix, available
capacity and task dependencies, without an extra one- or two-worker trial cap.

## Root gate

Before substantive work or delegation, establish the selected root model and
effort. Resolve each value on its own:

1. Current-session runtime metadata or already available status telemetry is
   the source of truth. In a cloud session (`CLAUDE_CODE_REMOTE=true`),
   `get_session` without a session id reports `session_context.model` and
   `external_metadata.last_served_model`, and the `CLAUDE_EFFORT` environment
   variable reports the selected effort. If the user declared a different
   value, say so in one line naming both values and continue with the runtime
   value; the user corrects the selection if needed.
2. If the session cannot read a value, accept the user's current-session
   declaration of it, recorded as `user-declared` with the effective value
   `unverified`.
3. If neither the session nor the user gives a value, ask one concise question
   for the missing model and/or effort, then pause task work and delegation
   until the user answers.

Keep requested and effective values separate. Do not rely on config defaults,
self-description, or another session. Do not repeat a declaration already
given for this session.

The root must be Opus and its selected effort must be supported. If either
does not match, ask the user to correct the selection and pause the task.
The user selects the root; never switch it automatically.
Use High usually, XHigh when needed, and Max only as a deliberate supported
choice. Do not use Codex telemetry or scripts to establish Claude settings.

Before dispatch, read the [runtime reference](references/runtime.md), inspect
the live Agent invocation schema and available agent types, and choose the
delegation mode below. Use only supported model aliases (`opus`, `sonnet`,
`haiku`) and effort fields/values. Invocation effort is usable only when the
live schema supports it; otherwise select a predefined profile. Never send
invented fields such as `reasoning_effort` or `fork_turns`. A request is not
evidence of the effective model. If an observed substitution or unavailable
model breaks the route, stop that unit and escalate to its parent. Runtime
substitution can occur before an observable warning; this policy cannot
guarantee its prevention or detection. Report missing telemetry and ask for
relevant UI evidence only when it is needed to resolve a concrete conflict.

## Delegation mode

Read `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` from the environment before the
first dispatch. Unset means the documented default of three layers.

**Tree mode** (two or more layers): the full routing matrix applies, including
child coordinators.

**Flat mode** (one layer, or the live runtime withholds Agent from subagents):
only the root dispatches and every child is a leaf. Cloud sessions currently
run in flat mode. Work goes to the cheapest model that can do it:

1. Haiku is the default worker. Before considering a stronger model, the root
   splits the work itself into Haiku-sized units: one bounded goal, named
   inputs, a checkable result, and a whole run within the Haiku context budget.
2. Use Sonnet only for a unit that cannot be split that way without losing
   coherence, such as one change that must stay consistent across many files,
   or after a Haiku unit failed its initial attempt and one targeted retry.
   Never hand Sonnet work so that it can split it further: it cannot delegate.
3. Use an Opus child, at Low, only after a Sonnet unit failed the same way.
   Architecture, risk decisions and final review stay at the root.

Root caps from the matrix still apply in flat mode, within native concurrency.
Every child uses a root slot; there are no subtrees to partition.

**Agent types.** Use `budget-aware-swarm:swarm-opus`,
`budget-aware-swarm:swarm-sonnet` or `budget-aware-swarm:swarm-haiku` when the
live agent list shows them. Cloud sessions load repository and claude.ai skills
but not plugins; when the profiles are absent, use `general-purpose` with the
`model` alias and an explicit `effort`: Haiku Low for mechanical work and
Medium otherwise, Sonnet Medium, Opus Low.

## Routing policy

Only the listed edges are allowed. Limits are open immediate children per
parent and child family; global capacity counts root and every descendant.

| Parent | Child | Open-child cap | Effort |
|---|---|---:|---|
| Opus root | Opus | 3 | Low |
| Opus root | Sonnet | 5 | Low, Medium, High, XHigh when supported |
| Opus root | Haiku | 20 | Any supported; omit if unsupported |
| Opus child | Opus | 3 | Low |
| Opus child | Sonnet | 3 | Low, Medium, High, XHigh when supported |
| Opus child | Haiku | 10 | Any supported; omit if unsupported |
| Sonnet child | Haiku | 10 | Any supported; omit if unsupported |
| Haiku child | — | 0 | Leaf |

Every other edge is denied, including Fable and aliases with unknown family mappings. Usually
use the least costly suitable model. Haiku is the first tier for suitable bulk
generation; allow initial attempt plus one targeted retry, then Sonnet and only
then Opus production. Keep architecture, risk decisions, and final review at
the root. Workers needing a stronger model ask their parent; they never create
an unlisted upward edge. Do not delegate trivial sequential or shared-file work.

**Haiku context budget.** Current Haiku pricing switches to a rate card five
times higher once a request's prompt exceeds 100K tokens, and every turn of an
agent resends its whole history. Size each Haiku unit so its entire run
(contract, files read, tool output and its own replies) stays under about 100K
tokens. Size by estimated tokens, not by file count: about four characters per
token, so `wc -c` of the inputs divided by four. A run's last prompt is roughly
the agent's fixed overhead (system prompt and tool definitions) plus all it
read, all tool output and its own replies. Keep the material a unit must read
to about half the budget, leaving the rest for overhead and output, and have
workers prefer `--help`, grep and line ranges to whole large files. Calibrate
with the token totals finished workers report in this session: when units of
one shape return near or above 100K, make the next ones smaller. Split a unit
that cannot fit, or route it to Sonnet.

## Capacity and bounded work

Claude Code's documented default nesting is three subagent layers and default
concurrency is 20. The policy ceiling is 50 open threads including the root,
subject to a lower actual runtime limit. Inspect relevant current-session
runtime metadata and capacity settings when available. Accept an explicit
user declaration of available capacity as `user-declared`, with effective
capacity `unverified`. If capacity remains unknown, record `unknown` and use
task-sized parallel batches within the policy ceiling and per-model caps;
missing capacity telemetry alone does not impose serial execution or require
a trial. Do not report a documented default as a verified active setting.
Honor smaller user allocations and observed runtime limits. Count externally
known active agents. In tree mode, root partitions subtree slots among active
child coordinators; each allocation includes that child's own slot and all its
descendants. Parents recursively partition before spawning. Keep unused slots
reserved until the child and descendants finish or an explicit transfer is
made. Use one coordinator for allocation records; children report allocations
to parents instead of editing a shared ledger concurrently. Do not double-use
slots. On capacity error, release the rejected spawn's reservation, wait for
native completion or a confirmed capacity change, then reassess before another
dispatch; do not retry-loop or claim an automatic queue or lifetime spawn limit.

Use the smallest relevant context and work units that can be checked. Default
to two attempts per unit (initial plus one retry), bounded time and turn
allocations. A Haiku worker should not spawn. If the runtime lacks nested
Agent support, a child asks its parent to dispatch permitted work; never launch
CLI or remote sessions, teams, forks, or another bypass. Stop at allocation
limits and return partial work with blockers. Complete when acceptance
criteria pass.

## Worker contracts

Keep a durable task ledger outside tracked project files unless the task calls
for a committed record. Record decisions, evidence, reservations, attempts,
changed paths, verification and next actions at stage boundaries. Use it to
recover after compaction. The first content in every Agent message is one of
two contracts. The ledger, not the contract, carries the parent's bookkeeping.

**Leaf contract**, for every child that will not spawn: every Haiku worker,
every child in flat mode, and any tree-mode child given no child slots.

```text
scope: bounded task; files it may write, or read-only
context: exact paths or sections to read; Haiku: whole run under ~100K tokens
returns: artifact paths, findings, verification; output limit
checks: acceptance criteria to verify before returning
rights: permitted actions; on ambiguity, scope growth or exhausted budget, stop and return a blocker
```

**Coordinator contract**, for a tree-mode child that may spawn:

```text
root: selected/requested model+effort; effective model+effort and evidence
parent: role, model+effort, task id
child: profile/model/effort requested; effective values (unverified until observed)
scope: bounded task; allowed write files
context: smallest relevant source paths and what to read
returns: artifact paths, concise decisions, verification, blockers; output limit
checks: acceptance criteria
rights: permitted actions, instruction-only write boundary, escalation route
allocation: attempts (default 2), timebox, maxTurns, subtree slots, immediate child caps, global slots, runtime depth/cap
quota: measured allocation or “unmetered/unavailable”
policy: applicable routing edges/caps, no bypasses, relevant sources actually read
```

`maxTurns` limits one run, not token or lifetime budget; resuming consumes the
same attempt, time, and turn allocations. Never claim a percentage of quota
saved without measurement. Preserve inherited permissions; instructions do
not create technical isolation. No mandatory memory integration: workers do
not write WikiLayer. Root owns final review and any separately required final
write. Keep secrets out of contracts.

For version-specific model, effort, nesting, and cost caveats, read
[runtime reference](references/runtime.md).
