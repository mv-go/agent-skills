# Reviewed cleanup boundaries

Read this when planning a cleanup. These categories were reviewed for repeated
Mac maintenance. An explicit cleanup request covers them after the entrypoint's
current path, owner, activity and open-file checks. A size question does not.
Local retention decisions and current user constraints take precedence.

| Category | Routine action | Boundary |
| --- | --- | --- |
| npm/npx | Remove exact `~/.npm/_cacache` and `~/.npm/_npx` cache directories | No active npm/npx job or open handles; preserve logs, config, auth and installed packages |
| Homebrew downloads | Read `brew --cache`; clear the verified default `~/Library/Caches/Homebrew` | Downloads only; preserve Cellar, Caskroom, applications and installed versions. A custom cache path needs attribution first. Do not substitute broad `brew cleanup` |
| pip | Verify `pip3 cache dir`/`info`, then `pip3 cache purge` | Cache must be a verified user cache; preserve virtualenvs and installed packages |
| Go builds | Verify `go env GOCACHE`, then `go clean -cache` | Build cache only; no `-modcache`, source or toolchain removal |
| pnpm | Verify `pnpm store path`, then `pnpm store prune` | No `--force`; preserve referenced packages and every project's `node_modules` |
| Swift repositories | `swift package purge-cache` | Global repository cache only; preserve SDKs, toolchains and projects |
| GitHub/font caches | Clear exact `~/.cache/gh` or `~/.cache/fontconfig` | User-owned caches only; preserve CLI auth/config and installed fonts |
| Known browser/media caches | Clear exact `~/Library/Caches/Google/Chrome`, `~/Library/Caches/Firefox`, or `~/Library/Caches/com.colliderli.iina` | Owning app and helpers must be closed; preserve profiles, accounts, extensions, saved sessions and app bundles |
| Other app caches | Only exact paths previously recorded in local `approved_app_caches` | Attribution and app inactivity are required; a cache-like name alone is not approval |
| Old app logs | Remove closed regular files under `~/Library/Logs` last modified more than seven days ago | Preserve open/recent files, crash evidence currently being investigated, chat/session history and logs elsewhere |

Native commands should resolve the expected paths on this Mac before running.
If a tool is missing, its location is unfamiliar, the command fails, or an activity
check is incomplete, skip that category and report it; no installation, force,
global package removal or filesystem workaround is implied.

## Docker

Inspect the actual context and allocated VM size. If the daemon is available,
read `docker --context <verified-local-context> system df -v` and builder state.
An already reviewed **local build cache** may be pruned with that exact context
and builder, after confirming no build is running. Never assume the default
context is local; no SSH connection or remote daemon mutation is authorized.
If Desktop is stopped, report that limitation. Starting it just to reclaim a
small cache is optional and should not be done by an audit child. A failed Docker
query does not authorize file deletion. Do not use `docker system prune`, volume
pruning, or delete `Docker.raw`; containers, images and volumes need separate
ownership/disposability review. Reinstalling Desktop through Homebrew is not a
storage cleanup; runtime replacement is a separate migration.

## Retain or review separately

- Personal Documents/Downloads/media, medical libraries and originals, app
  support/account databases, iCloud/CloudKit, messaging media and database trees.
- Project source, ignored/untracked data, firmware, acquired archives, recovery
  material, backups and derived indexes still needed for continuation. A source
  having been deleted can make an otherwise derived index hard to reproduce.
- Current Codex/ChatGPT state, auth/config, sessions, skills/plugins, active
  caches and the bundled dependency/runtime directories. Preserve referenced
  Playwright browsers and installed Node/Python/Swift runtimes and SDKs.
- Whole app bundles and their signed internals. Do not delete a Chrome framework
  version by hand or assume two copies are accidental duplicates.
- Git branches/worktrees and project dependencies. For separately authorized
  worktree cleanup, verify current remote merge state, cleanliness, local-only
  work and recovery scope; a local ancestry check alone misses squash merges.
- Trash contents. Read-only inspection is fine; emptying Trash requires explicit
  user authorization at the action, with normal UI policy still applying.

Do not uninstall an application just because its UI is unused. Docker can be
needed by agent-run builds. Full Xcode and standalone Command Line Tools are
distinct: a separately authorized Xcode removal must leave a working selected
toolchain. Current evidence, not an old uninstall receipt, determines presence.
For admin operations, use only authorized `sudo -n`; if a password is needed,
leave a concrete scoped command/script for the user to run locally. Never ask
for a password in chat or route around a Computer Use denial for Terminal.

Retention decisions are host-local. Do not add a user's absolute project paths
or private file inventory to this repository. A disconnected destination makes
a relocation deferred; do not create an empty mount-point directory or delete
the source based on an old receipt. If a future relocation is explicitly
requested, verify the actual destination and source-preservation scope then.
