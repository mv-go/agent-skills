# Claude Code runtime adapter

Documentation checked 2026-11-13. Inspect the installed CLI and live tool schema
when using this policy; this package does not configure the host. The routing
matrix is a local policy, not an Anthropic model recommendation or equivalence
claim about the Codex models it replaces.

## Runtime checks

Use `claude --version` and the session's actual Agent tool and available agent
types. The package is validated against CLI 2.1.321; model execution still needs
verification on the user's actual task. Complete its acceptance checks as part
of that work; no separate trial is required. Provider aliases can differ and change.

Record the current session's selected model and effort from available runtime
evidence. A user can check `/status` for the model and `/effort` or `/model` for
the selected effort, then declare what the session cannot read; runtime
evidence wins over a conflicting declaration. Those UI commands are user actions,
not shell commands or guaranteed agent tools. A setting can precede the turn
that applies it. Do not infer current settings from an unrelated CLI process,
the newest transcript, config defaults, or a model's self-description. The
skill does not use the Codex JSONL reader.

The `opus`, `sonnet`, and `haiku` aliases are requests. Provider mappings, forced
subagent models, fallback, and model allowlists can change their resolution.
`CLAUDE_CODE_EFFORT_LEVEL` overrides a worker's requested effort; unsupported
levels can be lowered. If a substitution breaks the policy, stop dispatch of
that unit and resolve it with the parent. Missing effective-value telemetry
alone is reported as `unverified`, not invented as a receipt. A substitution
may execute before its warning is observable. This instruction policy cannot
guarantee prevention or detection of runtime substitutions.

Sources: [model selection and effort](https://code.claude.com/docs/en/model-config),
[status-line session telemetry](https://code.claude.com/docs/en/statusline).

## Delegation interface

Use the loaded plugin-scoped types, normally
`budget-aware-swarm:swarm-opus`, `budget-aware-swarm:swarm-sonnet`, and
`budget-aware-swarm:swarm-haiku`. Without the plugin, use `general-purpose`
with the `model` alias and `effort` fields of the live Agent schema. Pass the
contract in the Agent prompt. Normal custom subagents start with fresh
context; the package runs its coordinator in the main conversation. It does
not use conversation forks or a forked skill.

Agent invocation accepts model aliases; definition files also support full IDs.
Per-invocation `effort` requires 2.1.292+; otherwise use a compatible profile.
Never translate Codex's `fork_turns` or `reasoning_effort` into invented Claude
parameters. The Haiku profile omits effort to accommodate models without it.
Request a supported level when the live schema and model allow one.

Current nesting defaults to three layers. At the depth limit, ask the parent
to route further work. `Agent(type)` filters are not enforced for a subagent.
Haiku denies Agent entirely. Other workers inherit available tools; plugin
agents ignore `permissionMode`, `hooks`, and `mcpServers` frontmatter. File
ownership and model edges remain instructions, not filesystem isolation.

Source: [subagent configuration](https://code.claude.com/docs/en/sub-agents).

## Cloud sessions

Cloud sessions (claude.ai/code, the mobile app; `CLAUDE_CODE_REMOTE=true`)
differ from a local CLI. A session observed on 2026-11-13 had these properties;
read the live environment rather than assuming they still hold:

- `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=1`: subagents cannot spawn, so the
  skill runs in flat mode.
- `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS` unset: the documented default of 20
  applies unless the runtime reports otherwise.
- `CLAUDE_EFFORT` held the selected root effort, and `get_session` reported the
  configured and last served model.
- A small container (4 CPUs). Many workers that only call the model are fine;
  many concurrent builds or test runs compete for CPU, so batch them.

Cloud sessions load skills and agents committed under the repository's
`.claude/` directory and skills enabled on claude.ai. They do not install
plugins, including ones a repository enables in `.claude/settings.json`, so
the plugin's worker profiles are usually absent there. Usage counts against the
same plan limits as local use.

Source: [cloud environments](https://code.claude.com/docs/en/cloud-environments).

## Haiku pricing

Current Haiku has two rate cards selected by prompt length: requests whose
prompt exceeds 100K tokens cost five times more per token. Each agent turn is
a request that carries the whole history, so the budget covers a worker's
entire run, not only its first message. Cache reads cost a fraction of the
input rate on every model, but caches are model-scoped: a new Haiku worker
cannot reuse the root's Opus cache and starts cold.

## Capacity and accounting

The policy ceiling of 50 includes the root; it is not a Claude runtime promise.
Size allocations to useful work and honor lower observed runtime limits. Reserve subtree
capacity before dispatch so concurrent coordinators cannot spend the same
slots. A child's reservation includes the child itself. A parent owns its
ledger fragment; it reports reservations to its parent instead of racing on a
shared file. Reclaim only after its descendants have stopped and returned.

In the documented CLI, `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS` controls ordinary
Agent spawns (default 20, since 2.1.217), and
`CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` controls nesting. Forks and resumes can
exceed the ordinary spawn check; Ultracode, teams, and workflows have different
accounting. Count known outside work, and never treat the native limit as
enforcement of this package's complete policy. Read current-session capacity
metadata and relevant settings when available. If capacity remains unknown,
record `unknown` and plan useful parallel batches within the policy matrix and
50-thread ceiling. Missing telemetry adds no one- or two-worker limit. On a
capacity rejection, release the failed reservation and wait for completion or
a confirmed capacity change before reassessing. Do not silently change settings.

For a user-chosen 50-thread setup, launch with
`CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS=49 claude --model opus --effort high`.
The 49 slots exclude the root. This command sets the requested limit for that
process; settings layers can override it, so inspect the active environment
and report evidence accurately. It is capacity, not a requirement to fill it.

Source: [environment-variable reference](https://code.claude.com/docs/en/env-vars).

## Attempts and usage

`maxTurns` bounds an individual worker run. A partial return is not success;
resuming it must fit the unit's remaining attempt and time allocation. It does
not allocate dollars, tokens, total descendants, or a lifetime spawn budget.
The ledger's timebox and quota are advisory unless an actual runtime control
exists. When measurement is missing, record `unavailable`.

For an independently authorized headless run, `--max-turns` and
`--max-budget-usd` are print-mode controls. The latter includes subagent usage,
uses an estimated API dollar figure, can overshoot, and is not a subscription
quota cap. This plugin neither starts such a run nor wraps the CLI.

`/usage` can show session/model token counts and cost estimates. Subscription
allowance and API list-price estimates are different measurements. Include
dispatch, context, retries, review, and rework before comparing approaches;
report no percentage saving without comparable measured workloads.

Sources: [CLI reference](https://code.claude.com/docs/en/cli-reference),
[cost and usage](https://code.claude.com/docs/en/costs).
