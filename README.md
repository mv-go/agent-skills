# Agent skills

Codex skills and Claude Code plugins. Git versions the source;
SemVer releases distribute reviewed packages without a source checkout.

## Install a released skill (recommended)

Pushes to `main` run [semantic-release](.releaserc.json): Conventional Commits
select a shared SemVer release (`fix` → PATCH, `feat` → MINOR, breaking changes
→ MAJOR). The release is published in this same repository. Changes
only to docs/tests/chores do not create a release; no version-bump commit or
manual release tag is needed.

Each release provides one universal ZIP per skill, platform-specific ZIPs,
and three all-skills bundles: universal, Codex, and Claude Code. The Claude
bundle includes the skills configured for Claude Code. Instructions differ by
platform and stay separate inside universal archives.

Agents install directly from release assets using the tools available on the
host: read an installed package's `release-info.json`, query GitHub Latest or a
pinned `vX.Y.Z` tag, select the skill/platform archive from `release.json`, verify
its checksum and extract it into the host's skill directory. There is no custom
release installer, package database, or background update process. Claude
bundles also include a ready native marketplace manifest.

See [release usage and publishing](docs/releases.md), [Cloud setup](docs/cloud.md),
and the explicit [publication file allowlist](releases/catalog.json). The
source-based commands below remain available for development and existing
installations. They fetch source and are not the artifact installation path.

## Budget-Aware Swarm for Claude Code

The independent [Claude Code plugin](plugins/budget-aware-swarm) maps the scheme
to an Opus coordinator, Sonnet workers, and Haiku workers. Prefer the
[released Claude package](docs/releases.md#claude-code). For source development,
install the marketplace from this repository:

```sh
claude plugin marketplace add mv-go/agent-skills
claude plugin install budget-aware-swarm@tired-tools --scope user
CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS=49 claude --model opus --effort high
```

Invoke `/budget-aware-swarm:budget-aware-swarm <task>`. See the
[Russian setup and usage guide](docs/claude.md) for repository access, working
directly on a real task, the 49-worker plus coordinator setup, and updates.
The scheme chooses useful parallelism within available capacity.
The Codex installer and native settings are independent of this plugin.

Cloud sessions do not install plugins, so the same skill is also committed in
`.claude/skills/budget-aware-swarm` (invoke `/budget-aware-swarm <task>`).
Without nested subagents it runs flat and Haiku-first; see the guide.

## Mac Storage Cleanup

Use `$mac-storage-cleanup` for periodic local Mac storage maintenance. This
workflow automatically invokes the installed Budget-Aware Swarm skill; the user
does not need to name both skills. It reuses Swarm's policy without copying its
routing matrix or changing native configuration. If Swarm is missing or its
entrypoint is unreadable, report that briefly and continue serially without
installing it. An available Swarm skill still owns its model/evidence gates.

The workflow measures current allocation and free space, lets read-only workers
inspect independent large folders, and keeps cleanup mutations with the root.
An explicit cleanup request covers the reviewed regenerable-cache categories;
new app uninstalls, personal/project data and uncertain items are discussed
separately. A size/audit question stays read-only. Host-specific retention and
deferred decisions live privately under `~/.local/state/mac-storage-cleanup/`,
so another run need not search old chats or commit a personal inventory.

Install the [released Codex archive](docs/releases.md) for `mac-storage-cleanup`. For source development, use the
built-in `skill-installer`, repository `mv-go/agent-skills`, path
`skills/mac-storage-cleanup`, at a reviewed commit. The existing `scripts/install.py` still manages Swarm only. The new skill
neither installs/configures Swarm nor schedules automatic cleanup. See
[the skill](skills/mac-storage-cleanup/SKILL.md) and its
[reviewed boundaries](skills/mac-storage-cleanup/references/cleanup-boundaries.md).

## Repository Cleanup

Use `$repo-cleanup` in Codex or `/repo-cleanup:repo-cleanup` in Claude Code to
audit or implement scoped repository documentation and structure cleanup.
Audits are read-only; implementation stays within the requested scope and
preserves useful behavior, provenance, and evidence. For source development,
install the Codex skill from `mv-go/agent-skills`, path `skills/repo-cleanup`,
or install the Claude Code plugin from the repository's `tired-tools`
marketplace. Released Codex and Claude packages are listed in
[release usage](docs/releases.md).

## Codex Token Audit

Use `$codex-token-audit` to collect and validate a self-contained,
machine-readable package for an explicitly selected completed task. The local
Python collector preserves raw counters, deterministic summaries, evidence
boundaries, source-linked runtime quota and tool observations, and its dated pricing snapshot.
Quota observations are account-wide and cannot be attributed to the selected task. Drive delivery is optional and
requires an explicitly configured folder; without one, the validated package
stays local. Delivery prefers the installed `gws` CLI and falls back to the
Google Drive integration connected to ChatGPT/Codex when `gws` is absent or
fails. Interrupted CLI delivery is reconciled through the integration before
any fallback write. The default `auto` setting starts with available `gws`;
explicit transport settings select the initial method. Its destination is saved
privately outside the
skill installation. This skill does not analyze causes,
recommend changes, or measure
subscription quota. Raw telemetry stays local. See
[the skill](skills/codex-token-audit/SKILL.md) for collection and delivery
instructions.

Install the [released Codex archive](docs/releases.md) for `codex-token-audit`. For source development, install it
separately with Codex's built-in `skill-installer`, from
`mv-go/agent-skills`, path `skills/codex-token-audit`, at the reviewed
commit. Then complete the packaged
[Token Audit installation procedure](skills/codex-token-audit/references/installation.md)
on that execution host: reuse/resolve the existing Drive destination, save its
private local setting, and deliver/verify a synthetic bundle. Installation and
update requests include this setup unless explicitly local-only. The built-in
skill-installer copies files without a post-install hook; the installing agent
must finish the connected setup and report a blocker if it cannot. The existing
installer below manages Budget-Aware Swarm only; installing Token Audit does not
change Swarm/native settings or collect real session telemetry.

## Budget-Aware Swarm

Invoke `$budget-aware-swarm` or `следуй схеме работы Budget-Aware Swarm`. The v2 routing matrix uses Sol 6.1 roots and children, Terra children, and Luna leaf workers; unsupported edges are forbidden. Choose the root model when starting the session. The skill cannot change a running root model or automatically switch it. Child model and effort are requested per task only from options supported by the actual runtime; requested settings are not proof of effective settings. See the [runtime reference](skills/budget-aware-swarm/references/runtime.md) for the exact matrix and verified limits.

The installed skill is self-contained: its policy and references ship in the
package, and it requires no WikiLayer connection or live policy bootstrap.
Separate user/project instructions may still require project context or final
memory synchronization; those do not become dependencies of the Swarm policy.

Swarm also bounds context and coordination: group small related checks, provide
workers only the relevant sources, keep large results in artifacts, and finish
on sufficient verification evidence. See the
[context-efficiency guide](skills/budget-aware-swarm/references/context-efficiency.md).

At startup the skill checks the current session's model and effort, including local `turn_context` metadata when available. If either remains unknown or conflicts, the root pauses and asks you to select or enter both values before continuing the task. It does not silently switch to substantial root-only work. You do not need to repeat settings already established for the current session. This is an instruction-level check, not a runtime permission barrier.

For the legacy source-checkout installation, use `main`. On Mac/Linux with existing Python 3.11+, Git, Codex:

```sh
git clone --branch main https://github.com/mv-go/agent-skills.git ~/.local/share/agent-skills
cd ~/.local/share/agent-skills
python3 scripts/install.py install --concurrency 49
python3 scripts/install.py verify
python3 scripts/install.py update --concurrency 49
```

`install` creates a user skill symlink and adds only the explicit trigger and native agent settings. The commands above explicitly select 49 children (50 total including the root); without `--concurrency`, the installer preserves smaller existing limits. Honor any smaller capacity requested by the user. Native config is not a lifetime spawn counter and does not guarantee the current app session has reloaded it. The installer does not set model/effort, change OAuth/MCP/providers, install Python, or require Python packages. Updates use `git pull --ff-only` and refuse dirty/divergent checkouts.

**Complete the [packaged installation procedure](skills/budget-aware-swarm/references/installation.md) on every execution host**, including when Codex's generic `skill-installer` only copies the package. It covers native V2 settings, supported models/efforts, the actual Codex home and configuration layers, existing V2 overrides, and current-session readback. The repository helper refuses explicit V2 concurrency overrides; that guide provides the native configuration path for such hosts. A successful file copy or installer `verify` alone is not runtime acceptance.

```sh
python3 scripts/install.py rollback
```

Rollback restores original bytes/modes and removes only the owned symlink. If later edits exist, it refuses to overwrite them; `verify` can still pass when only unrelated settings changed. Private originals remain under `~/.local/state/budget-aware-swarm/` (directory0700, backups0600); never commit them. Unsupported config shapes and foreign symlink/file collisions fail before mutation.

Run deterministic tests with `python3 -m unittest discover -s tests -v`. See [Cloud procedure](docs/cloud.md). The skill uses native collaboration; model-pair limits and WikiLayer read-only rules for children are instruction-level boundaries, not technical permission isolation. V2 has no lifetime spawn limit or fixed depth ceiling.
