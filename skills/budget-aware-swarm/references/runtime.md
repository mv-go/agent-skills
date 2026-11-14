# Codex runtime reference

This reference records verified source behavior for the implementation. Check
the current tool schema and sources again when Codex is upgraded; the model
catalog and app/runtime capabilities can change.

The routing policy ships in the skill package. The external links below record
audit evidence; using the bundled scheme does not require fetching them or
connecting to WikiLayer. Separate project-context requirements still apply.

## Native V2 spawning

The V2 `spawn_agent` handler accepts optional `agent_type`, `model`, and
`reasoning_effort` values and passes them into the child thread request. This
supports per-spawn selection where the active runtime exposes those arguments
and allows the selected model/effort. Omit `agent_type` unless the task needs a
role whose config has been checked for fixed model or effort settings.

`fork_turns` defaults to `all`; `none` disables inherited history. Positive
numeric legacy values are accepted but map to full history, so they do not
bound context. `fork_context` is rejected by V2. Use `fork_turns="none"` when
passing dynamic model and effort overrides.

Official implementation, checked at the audited release tags: [V2 spawn handler (0.164.0)](https://github.com/openai/codex/blob/rust-v0.164.0/codex-rs/core/src/tools/handlers/multi_agents_v2/spawn.rs) and [V2 spawn handler (0.162.0)](https://github.com/openai/codex/blob/rust-v0.162.0/codex-rs/core/src/tools/handlers/multi_agents_v2/spawn.rs).

## v2 routing policy

This is the requested orchestration policy; it is not enforced by native Codex
permissions. Every child must stay within the edge matrix below. “Any supported”
means consult the live runtime's advertised model and effort options at spawn
time; it does not assert that every model supports every effort.

| Parent | Allowed child | Maximum open immediate children of this type | Effort |
|---|---|---:|---|
| Sol 6.1 root | Sol 6.1 | 3 | Low |
| Sol 6.1 root | Terra | 5 | Up to XHigh |
| Sol 6.1 root | Luna | 20 | Any currently supported |
| Sol 6.1 child | Sol 6.1 | 3 | Low |
| Sol 6.1 child | Terra | 3 | Up to XHigh |
| Sol 6.1 child | Luna | 10 | Any currently supported |
| Terra child | Luna | 10 | Any currently supported |
| Luna child | — | 0 | Leaf |

All unlisted edges are forbidden. These caps are instruction-level limits,
not native runtime quotas. Each creator counts only its own simultaneously
open immediate children of each model; descendants count toward the shared
global pool, not each ancestor's immediate-child cap. There is no
lifetime creation limit. Normally use High or XHigh for the root; reserve Max
for deliberate complex work. Do not automatically change the root model.

Example shape only, when the active tool schema permits it:

```json
{
  "task_name": "bounded-code-unit",
  "message": "Implement the scoped change and report the files and checks.",
  "model": "<currently supported model slug>",
  "reasoning_effort": "<effort supported by that model>",
  "fork_turns": "none"
}
```

The collaboration tool schema may expose a narrower allowlist than the
repository model catalog. Never send a placeholder literally or assume a
catalog entry is available to the current session.

## Config limits and model support

For installation, upgrades, or host setup, use the bundled
[installation procedure](installation.md) to inspect/apply settings and verify
current-session readiness. Copying the package does not configure the host.

`agents.max_depth` is documented as a maximum nesting depth for V1 threads and
is ignored by V2. V2 simultaneous capacity is a separate setting. These
verified releases resolve `agents.max_concurrent_threads_per_session = 49` to
49 child slots plus the primary, total 50. Explicit
`features.multi_agent_v2.max_concurrent_threads_per_session = 50` takes
precedence and already counts the primary. Do not duplicate the common setting
under its `max_threads` alias: it names the same field. A smaller preserved
configuration can lower the resulting cap.

The exact conversion and precedence are in the pinned native resolver:
[0.164.0](https://github.com/openai/codex/blob/6aaac694803004a63c6f393d153c96d22521a6b9/codex-rs/core/src/config/mod.rs#L2838-L2849),
[0.162.0](https://github.com/openai/codex/blob/c1382380de69521303b416720a52f42d51af6248/codex-rs/core/src/config/mod.rs#L2777-L2788).
Configuration layers resolve from managed overrides, session flags, project,
selected user profile, user, enterprise cloud, then system; see the pinned
[loader order](https://github.com/openai/codex/blob/6aaac694803004a63c6f393d153c96d22521a6b9/codex-rs/config/src/loader/README.md).

Capacity is simultaneous resident children plus pending spawn slots, not a
lifetime number of agents created. At capacity the runtime may unload an idle
completed resident on demand; completion alone does not immediately free a
slot. `wait` observes activity and does not close a child or guarantee slot
release. No automatic queue is promised. Bounded source inspection found no
hidden lifetime cap in these releases; this is not a claim about every external
harness. Idle completion and reclamation are defined by the pinned
[residency controller](https://github.com/openai/codex/blob/6aaac694803004a63c6f393d153c96d22521a6b9/codex-rs/core/src/agent/control/residency.rs#L186-L272).

Official config schema: [config_toml.rs (0.164.0)](https://github.com/openai/codex/blob/rust-v0.164.0/codex-rs/config/src/config_toml.rs) and [config_toml.rs (0.162.0)](https://github.com/openai/codex/blob/rust-v0.162.0/codex-rs/config/src/config_toml.rs).
The available model slugs and each model's `supported_reasoning_levels` are
published in the [Codex model catalog](https://github.com/openai/codex/blob/rust-v0.164.0/codex-rs/models-manager/models.json).
Discover support at use time; do not treat one model's effort list as global.

For config readback, native `config/read` can return effective config and
per-key origins (including selected layers). Use its allowlisted relevant keys,
not a full config dump, and resolve the final V2 total with the rules above.
Layer precedence can cause the value to differ from `~/.codex/config.toml`.
This API reads effective configuration on disk. It is client telemetry, not
server-side attestation or proof that the active turn reloaded the value. A
startup prompt rendering also differs from the actual session's advertised
capacity. In the checked Codex 0.165.0 host, a native write with
`reloadUserConfig: true` left the active turn's four-slot snapshot intact; the
following turn advertised 50 without a new chat. Recheck before requiring a
restart; this observed behavior is not a reload guarantee for other hosts.

## What a parent can verify

The app's collaboration tool schema may constrain model and effort overrides to
its advertised choices. A tool-backed session does not necessarily expose an
authoritative receipt proving the active root model/effort or the effective
child profile. A request is evidence of intent only. Mark effective values
`unverified` unless system telemetry or a runtime receipt confirms them.

The open issue [#33772](https://github.com/openai/codex/issues/33772) reports
this root model/effort observability gap. It is a report of an open issue, not
a guarantee that every current build has identical behavior.

### Current root readback

On local Codex, the current session's JSONL `turn_context` can expose `model`,
`effort`, and `turn_id` even when the prompt and app tools omit them. Resolve
the exact chat/session ID from `CODEX_THREAD_ID` when provided or the current
thread's runtime metadata. Do not select a session by modification time or
infer identity from the working directory, which multiple chats can share.

Run the bundled reader by absolute skill path:

```sh
python3 <skill-directory>/scripts/read_session_context.py --session-id UUID --turn-id TURN_UUID
```

Omit `--turn-id` only when the current turn ID is not exposed; the reader then
requires an active `task_started` event and its matching `turn_context` in
that exact session. A completed turn, missing current context, or absent event
markers cannot silently fall back to historical metadata. Conflicting routing
fields for the same selected turn are rejected. The reader searches `$CODEX_HOME/sessions`
(default `~/.codex/sessions`); `--codex-home PATH` explicitly overrides it.
It validates the session's metadata and emits only routing fields and source
location, never message bodies, prompts, credentials, or configuration files.
It neither changes the root model nor starts agents.

Exit zero with `status=ok` requires both model and effort. Missing, ambiguous,
unreadable, incomplete, or conflicting data must lead to the skill's user
question gate. Do not substitute config defaults or another session. If a
file is being written and is temporarily incomplete, one bounded reread is
reasonable; do not loop indefinitely. Cloud/SSH availability varies: check
the current host, not a local file belonging to another execution host.

Record this as `client_telemetry`, not a server-side attestation. A user-supplied
current model/effort is `user-declared` and leaves effective values unverified.
If clarification is needed, use the native question UI with model/effort
choices when available and wait for the response. A tool returning immediately
or a preselected choice does not authorize continuing. In a plain chat, ask
the same question and stop the turn until answered.

## Per-child permissions and skills

The official [subagent documentation](https://learn.chatgpt.com/docs/agent-configuration/subagents)
says children inherit the parent's sandbox, approval settings, permissions,
provider, and MCP servers. Custom-agent instructions cannot create a
read-only sandbox, and a custom agent cannot replace the inherited MCP server
configuration. The documentation does not define a per-child read-only filter
for selected tools from an inherited MCP server. Disabling the WikiLayer MCP
connection would also remove its read tools. Treat child read-only behavior as
an instruction boundary, not technical enforcement.

This implementation uses a native Codex skill as the workflow adapter:
skills provide reusable instructions and references, while MCP tools provide
live data, authentication, and controlled actions. See the official
[Codex skill guide](https://learn.chatgpt.com/docs/build-skills).

## Cost evidence

Do not claim that model routing saves a particular amount without comparable
workload accounting across all models: input/context, output, reasoning,
cached usage, retries, latency, review, and rework. Published API prices do not
establish actual ChatGPT/Codex quota consumption. Report measured usage and
quality evidence separately from API price estimates.
