# Installation and host readiness

Use this procedure when installing, updating, or setting up Budget-Aware Swarm
on a machine. A copied skill package and a configured, usable runtime are
separate acceptance checks. An installation/setup request covers the relevant
local configuration changes below, subject to explicit user and managed
constraints. During ordinary skill use, report a host limitation instead of
silently reconfiguring the machine.

## Inspect the machine that will run the skill

1. Identify the execution host, Codex version, active Codex home, and discovered
   skill location. For SSH, inspect the remote host; hosted tasks do not inherit
   local settings. Use runtime metadata or app-server initialization to locate
   the active Codex home, then `CODEX_HOME` when applicable; the usual fallback
   is `~/.codex`. Do not change an unrelated local installation.
2. Confirm that the complete package is discoverable, including `SKILL.md`,
   `agents/openai.yaml`, `references/`, and `scripts/`. A generic skill installer
   may only copy these files; finish the native setup below as well. Preserve
   the package's explicit invocation policy.
3. Resolve the current root model and effort as required by `SKILL.md`. The
   scheme requires GPT-6.1 Sol, normally High or XHigh. Keep the user's selected
   root; an unsupported selection follows the existing root clarification gate.
4. Inspect the live collaboration schema and model catalog. Required spawning
   capabilities are per-child `model`, `reasoning_effort`, and an isolated
   `fork_turns="none"`. Check supported Sol Low, Terra efforts through XHigh,
   and Luna efforts, plus native follow-up and wait tools. Available model names
   alone do not establish support for every effort or spawn override.
5. Read the applicable configuration layers and origins through native
   `config/read`, using the working directory where the skill will run and
   `includeLayers: true` when supported. Inspect only the agent settings below,
   `agents.max_threads`, and relevant higher-priority overrides. Keep full
   configuration, prompts, and credentials out of reports.

Record the current runtime's advertised total slots separately from the
configuration readback. A runtime may already expose V2 through its model
preset without an explicit V2 entry in the user file.

## Apply native settings

For full configured capacity, the target is **50 total threads, including the
root**, with V2 available. On a runtime supporting these fields, the relevant
user settings are:

```toml
[agents]
enabled = true
max_concurrent_threads_per_session = 49

[features.multi_agent_v2]
enabled = true
max_concurrent_threads_per_session = 50
```

This is a target configuration, not a block to append blindly. Merge into
existing tables, preserve unrelated keys/comments, and normalize an unambiguous
legacy `agents.max_threads` alias instead of creating duplicate keys. An
explicit V2 concurrency value counts the root and overrides the common child
count; changing only the common value cannot defeat a smaller V2 override.
See [configuration semantics](runtime.md#config-limits-and-model-support).
These fields and a resulting 50-slot startup prompt were also checked locally
with Codex 0.165.0. Validate the actual installed version's schema/capabilities;
do not write unsupported fields based solely on a version number or this sample.

Use the actual user configuration path found during inspection. Save a private
backup before changing it. Prefer native `config/batchWrite` when available,
with the inspected user layer's `expectedVersion` and edits to the four keys
shown above. Otherwise use a TOML-aware update that preserves unrelated
settings and validates the result. Preserve file permissions. Where supported,
`reloadUserConfig: true` requests application to loaded threads; it does not
prove that the active turn adopted the new capacity.

An existing smaller value is configuration evidence, not by itself a request
to retain reduced capacity. For an authorized full-capacity setup, update the
explicit V2 total to 50 as well as the common child count to 49. If the user
specified a smaller capacity, retain it and report the resulting total.
Managed constraints or a hosted service may prevent the target; record
the actual limit and the unavailable capabilities. Do not claim full 50-slot
readiness or try to bypass the restriction through nested agents. This skill
has no additional requirement to change sandbox/approval policy, credentials,
providers, MCP connections, root model defaults, V1 nesting depth, or a lifetime
spawn budget. WikiLayer is not an installation dependency.

### Versioned release package

Release assets are ordinary archives, not an installer protocol. On the actual
host, locate the existing skill and read its `release-info.json` (skill, version,
host, source commit). Missing release metadata means an unknown/source version;
identify it before replacement. Query the repository's GitHub Latest release or
a reviewed `vX.Y.Z` tag, then pin that exact release for the whole operation.
Read `release.json` to select the Codex archive and its checksum. Compare stable
SemVer components numerically, never lexicographically.

Using the host's available authorized download, hash and extraction tools,
verify the archive, extract to staging and replace only the identified skill
package at the actual discovery path. Preserve unrelated files/settings and
local modifications. A bundle is a collection of skill directories; install
only the requested ones. Re-read installed release metadata, package files and
native discovery after replacement. Restore an older reviewed package the same
way when rollback is requested. No custom package state or background updater
is required, and downloading/extracting does not require Python.

Complete the native settings and live verification in this guide separately.
For managed Cloud runtimes, record the actual service capacity instead of
trying to configure an unrelated local client. Some skill functions may use
Python tools; their runtime requirements are separate from file delivery.

### Source repository installer or copied package

When an authorized source checkout is available and its installer supports the
host's configuration shape, run from that checkout:

```sh
python3 scripts/install.py install --concurrency 49
python3 scripts/install.py verify
```

Use `update --concurrency 49` for the same explicit capacity target during a
managed update. Omitting `--concurrency` preserves smaller existing limits;
that is not full-capacity setup. Honor a user's deliberately smaller target.
The installer creates its discovery link, invocation paragraph, backups, and
common agent settings. Complete the live capability and runtime checks in this
procedure afterward; `verify` checks installer-managed files, not running slots.

The current repository installer deliberately refuses an explicit
`features.multi_agent_v2.max_concurrent_threads_per_session` override and some
unsupported TOML shapes. It also manages `<home>/.codex`, not an arbitrary
separate `CODEX_HOME`. If those cases apply, use the copied-package path and
native configuration procedure above, preserving existing discovery and
unrelated user settings. Update the existing V2 value to the requested total;
its mere presence does not require retaining a smaller number. Do not delete a
valid V2 setting merely to make the helper pass, or
describe a manually configured installation as managed by that helper.

The copied-package path needs no repository checkout or private service:
install the complete package through an available authorized skill installer,
confirm discovery, then inspect/apply/verify native settings using this guide.

## Verify configured capacity and current-session readiness

1. Re-read effective configuration and the origins of the relevant keys from
   the intended working directory. For V2, use the explicit total if present;
   otherwise add one to the common child limit. Confirm there is no overriding
   project/profile/session or managed setting reducing the target.
2. When available, `codex debug prompt-input` can render the startup prompt
   without a model run. Capture it privately and extract only the advertised
   concurrency sentence. A no-override rendering that announces 50 proves the
   startup configuration is being read; it does **not** measure active-session
   capacity or run 50 workers.
3. Check the actual chat's current runtime instructions/receipt for its total
   slots. An already-running turn can retain the old snapshot even after a
   successful write/reload. Finish that turn and recheck on the next one before
   requiring a new chat or restart. If still stale, reload/resume the session
   using the client; use a new session if necessary. Do not promise either
   immediate reload or a mandatory restart without checking the host.
4. Confirm skill discovery and perform a bounded native delegation check when
   model/spawn capabilities have not yet been demonstrated on this host. Use a
   small read-only task, the normal Swarm contract and ledger, supported
   model/effort overrides, and native completion. Do not launch 49 children as
   an installation load test. Mark effective child profiles unverified unless
   exact-thread/turn telemetry confirms them.

Report separately: package installed/discoverable, configuration target and
source, current-session capacity, supported routing, and any unverified smoke
behavior. Once the current runtime advertises 50, the root selection is valid,
and the needed spawn/model capabilities are confirmed, no additional native
settings are required by this scheme. A lower capacity can support reduced
parallelism but must be stated explicitly. On Cloud or other externally managed
runtimes without writable effective settings, report the service limit instead
of claiming that a local file can increase it.
