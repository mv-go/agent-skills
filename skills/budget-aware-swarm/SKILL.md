---
name: budget-aware-swarm
description: >-
  Use when the user explicitly invokes $budget-aware-swarm or says
  “следуй схеме работы Budget-Aware Swarm”. Applies
  only to that named scheme, not generic coding tasks.
---

# Budget-Aware Swarm

Use this skill only when the user explicitly names the Budget-Aware Swarm scheme.
This is a self-contained policy for Codex-native subagents. The installed skill
package contains the routing matrix, budgets, escalation rules and runtime
reference needed to use the scheme. Apply this bundled policy and explicit
current user instructions; policy changes reach the skill through a versioned
package update, not a live external-policy fetch.

## Resolve the current root before substantive work

Missing model information in the prompt is not proof that runtime metadata is
unavailable. Before delegating or doing the task yourself, establish **both**
the current root model and reasoning effort:

1. Use an authoritative current-turn runtime receipt when exposed. Otherwise,
   resolve this chat's exact session ID from the runtime/thread metadata and read
   its local `turn_context` using
   `python3 <skill-directory>/scripts/read_session_context.py --session-id UUID`.
   Pass `--turn-id UUID` when the current turn ID is available. See the
   [runtime reference](references/runtime.md#current-root-readback) for discovery
   and evidence limits. Do not use another chat, the newest session on disk,
   config defaults, or your model self-description as a substitute.
2. Record model, effort, evidence source and session/turn in the working ledger.
   Client telemetry confirms the client's applied configuration, not independent
   server-side model identity. Choose child effort separately.
3. If either field is missing, the session/turn is ambiguous, or evidence
   conflicts, **pause the task and ask the user** which root model and effort
   they selected. Prefer the available question UI with selectable model/effort
   choices from the runtime, plus free text. If no question tool is available,
   ask in chat and end the turn. Explain which read failed in one sentence.
   An asynchronous question must be answered before proceeding: no children,
   substantive task execution, or silent serial fallback while it is pending.
   Elapsed time and a preselected option are not an answer.

An explicit current-session user declaration of **both** fields can resolve
missing telemetry for routing; label it `user-declared`, keeping effective
values `unverified`. Do not ask the user to repeat a declaration already given
for this session. Recheck after a model/effort change or resumed session; the
check does not change the selected root or expand its allowed child models.
Once resolved, apply the fixed root policy below. If the verified root is not
GPT-6.1 Sol, or its selected effort is not supported by the active runtime,
pause and ask the user to correct the selection. Do not automatically switch
the root model. For GPT-6.1 Sol, use High usually, XHigh when needed, and Max
only as a deliberate choice for exceptionally difficult work. A metadata check
is not a reason to keep all execution at root.

## Installation and host readiness

When installing, updating, or configuring this scheme on a machine, follow the
[installation procedure](references/installation.md). Copying the package does
not complete native Codex setup. Check and apply the required host settings
within the installation request's authorization, then verify the running
session; do not declare readiness from file installation alone.

Before first delegation in a new or resumed session, check the live spawn
schema, supported model/effort choices, and advertised total capacity. Record
the actual capacity in the ledger. The policy ceiling is 50 including the
root; a smaller runtime limit also applies. Ordinary Swarm work does not
authorize silently rewriting host settings to reach that ceiling.

## Use the bundled policy

No WikiLayer connection is required to plan, delegate, or complete Swarm work.
An unavailable external service alone does not force serial execution: the
matrix below establishes spawn authority, subject to confirmed runtime support.

Separate user or project instructions (including `AGENTS.md`) may require reading
project context or synchronizing results with a memory service. Follow those
instructions in their own scope; distinguish a missing project input from a
Swarm prerequisite. This skill adds no external bootstrap or mandatory memory
write. Keep repository code and current execution configuration as the source
of technical facts.

Before delegating, pass the child a concise summary of this bundled policy and
the task constraints in the spawn contract below. Include external source IDs
only for context actually read and relevant to the child's unit. A named source
is not evidence that the child read it.

## Route work by authority and cost

Use the exact policy matrix below. Model names identify model families; only
select model IDs and effort values that the current spawn tool supports. Every
parent/child pair not listed is denied. In particular, Astra is not an allowed
root or child choice in this scheme, Terra cannot spawn Sol or Terra, and Luna
is a leaf. A child that needs stronger capability asks its parent to take over
or route the work upward; it never invents a new edge. Do not automatically
change the selected root model.

```json SWARM_POLICY_V2
{
  "models": {
    "sol_root": {"model": "GPT-6.1 Sol", "root_effort_guidance": ["usually-high", "xhigh-when-needed", "max-deliberate-exceptionally-difficult"]},
    "sol_child": {"model": "GPT-6.1 Sol", "efforts": ["low"]},
    "terra": {"model": "GPT-6.1 Terra", "efforts": "runtime-supported"},
    "luna": {"model": "GPT-6 Luna", "efforts": "runtime-supported", "leaf": true},
    "astra": {"model": "GPT-6 Astra", "allowed": false}
  },
  "edges": [
    {"parent": "sol_root", "child": "sol", "max_open_children": 3, "efforts": ["low"]},
    {"parent": "sol_root", "child": "terra", "max_open_children": 5, "efforts": ["low", "medium", "high", "xhigh"]},
    {"parent": "sol_root", "child": "luna", "max_open_children": 20, "efforts": "runtime-supported"},
    {"parent": "sol_child", "child": "sol", "max_open_children": 3, "efforts": ["low"]},
    {"parent": "sol_child", "child": "terra", "max_open_children": 3, "efforts": ["low", "medium", "high", "xhigh"]},
    {"parent": "sol_child", "child": "luna", "max_open_children": 10, "efforts": "runtime-supported"},
    {"parent": "terra", "child": "luna", "max_open_children": 10, "efforts": "runtime-supported"}
  ],
  "global_open_threads_including_root": 50,
  "descendant_open_threads": 49,
  "lifetime_spawn_cap": null
}
```

Use Luna first for output-heavy work when a suitable supported combination is
available. For a Luna generation failure, diagnose the brief, context, and
check; make one reasonable targeted retry. If Luna still cannot produce a
checkable result, move the generation to a permitted Terra child before asking
Sol to produce bulk output. A Terra or Luna child that needs stronger capability
asks its parent. The parent owns architecture, risk decisions, and final review.
Do not delegate trivial, sequential, or shared-file work when coordination
costs more than it saves.

Set a reasonable attempt allocation per work unit in the ledger; use two total
attempts (initial plus one retry) by default. An attempt allocation limits
retries for that work unit, not the number of children over the task lifetime.

## Keep execution bounded

Before dispatch, group related small checks into a useful work unit and choose
the smallest relevant source set and a bounded return. Keep bulk extraction and
generation with suitable workers; the root receives findings, artifact paths,
verification and blockers. Batch independent tool reads and inspect compact
results. Read the [context-efficiency guide](references/context-efficiency.md)
when planning delegation or managing a growing task.

At a completed stage, update the durable ledger with decisions, changed paths,
verification and next actions. Use that handoff to reduce carried history when
the host supports it. Retain the current root effort selected by the user;
recommend High for ordinary future tasks and XHigh when deeper work warrants
it. Once acceptance checks pass, finish the agreed scope; repeat checks or add
review only for changed code, failures, conflicting evidence or unresolved risk.

## Spawn and budget contract

Choose model and effort independently from the parent's settings, following the
matrix above and the runtime's supported catalog. Sol children always use Low;
Terra children may use at most XHigh; Luna children may use any effort supported
by the runtime. When selecting model/effort overrides, pass
`fork_turns="none"`. If the requested combination is unavailable, report the
limit and use only a permitted alternative.

The child-count rule means simultaneous open immediate children, grouped by
creator and child model, against that edge's limit. Before each spawn, reserve
one open child slot in the shared ledger, record its complete contract, and put
the exact same contract as the first content in the child's `message`; task
instructions follow it. Release the reservation when the runtime frees that
open slot. There is no lifetime spawn cap and no fixed depth cap. This policy
allows at most 50 open threads including the root (49 descendants), subject to
the actual runtime's possibly smaller capacity. The per-parent/model caps are
instruction-level policy; native Codex controls global
concurrency. This skill does not add an orchestrator, queue, or runtime
enforcement.

When the actual descendant capacity (at most 49) is occupied or a spawn reports
capacity, inspect the existing agents and wait for a completion using the native runtime. Reuse a
released slot for a new child, or send a follow-up to a suitable idle child.
Native V2 may unload eligible idle completed contexts on demand. Do not manually
close a context unless the runtime requires and provides that operation. Do not
busy-loop or claim there is an automatic queue. If capacity remains occupied
after waiting and the runtime provides no release path, report that runtime
limitation. A wait is not itself a spawn attempt or budget reservation.

```text
root: requested model+effort; effective model+effort
parent: model+effort; path
child: requested model+reasoning_effort; effective model+reasoning_effort
scope: bounded task
files: allowed write set
context: relevant artifact paths and ranges; what the child must read
returns: verdict, artifacts, verification, blockers; output budget
checks: acceptance criteria
rights: allowed actions and escalation route
allocation: this child’s descendant slots; open immediate-child slots by model; global open slots; attempts; concurrency; no lifetime spawn cap
timebox: task-appropriate limit and stop/escalate condition
quota: assigned usage limit, or explicitly “unmetered/unavailable”
policy: bundled SWARM_POLICY_V2 summary; relevant task constraints and sources actually read
```

Mark each effective model/effort `unverified` unless an authoritative runtime
receipt or system telemetry confirms it. A request, config file, role, or model
self-report is not a receipt. The root owns and tracks the shared totals. A
child follows its assigned attempt allocation, stops at its timebox or usage
limit, and reports or requests
escalation; it does not extend its own allocation.

There is no lifetime total spawn limit. The root maintains the open-tree ledger
and observes native global concurrency. Do not spawn for duplicated work or
status polling. Model-pair limits and the global 50-thread pool are policy and
runtime constraints respectively, not lifetime budgets.

## Permissions and WikiLayer

Codex subagents inherit the parent's sandbox, approval settings, permissions,
provider, and MCP servers. A custom agent's instructions cannot create a
read-only sandbox or replace MCP server configuration. This is an instruction
adapter, not technical isolation or runtime enforcement. Children never write
WikiLayer; they may read relevant context when the task requires it. When
separate user or project instructions require WikiLayer synchronization, the
root agent alone writes the verified final state, once, in the completion phase;
the root does not make interim WikiLayer writes. Without that separate
requirement, finish with the task's normal deliverables. Never send secrets or
private production evidence to children unless strictly needed and authorized.

For verified Codex V2 spawn fields, `fork_turns` semantics, model catalog,
effective-value caveats, and source links, see [runtime reference](references/runtime.md).
Cloud availability and skill discovery must be checked in the actual runtime;
an installed skill does not establish parity with a different host.
