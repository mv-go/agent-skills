# Codex Cloud skill discovery

Cloud task environments do not inherit local personal skills or Codex settings.
Install a pinned [skill release](releases.md) in the environment where it will
run. This downloads one package instead of cloning this repository.
It does not synchronize environments automatically.

## Install or update in the target environment

After the first release is published, give the setup agent the repository
`mv-go/agent-skills`, desired skill(s), host and optionally a reviewed
version. Follow the [direct artifact procedure](releases.md); no custom
installer or consumer-side Python is required for downloading/extracting.
Use available authorized GitHub tooling or API access and the environment's
archive/hash utilities.

1. Find the actual Codex discovery directory on this host and any existing
   package. Read its `release-info.json`; a package without release metadata
   has an unknown/source version and must be identified before replacement.
2. Query GitHub Latest, or the requested `vX.Y.Z` tag, then keep that exact
   release for the whole operation. Read `release.json`, select the platform
   archive and compare the installed SemVer with the available version.
3. Download the selected ZIP and verify its hash. Extract to a staging directory,
   inspect the full package, and replace only the identified skill-owned files.
   Preserve local edits, native settings and host-specific Token Audit delivery
   configuration. A Codex all-skills ZIP contains one directory per skill;
   select only the requested packages.
4. Re-read installed `release-info.json` and verify actual skill discovery and
   native capabilities. A file appearing on disk does not prove the running
   chat has loaded it; discovery may require the next task/session.

The usual personal discovery directory is `~/.agents/skills/<skill>`; some
hosts use `$CODEX_HOME/skills/<skill>`. Inspect the actual host rather than
installing duplicate copies at both paths. Configure a pinned artifact download
in reusable setup only when that behavior is requested. Ordinary skill usage
adds no background updater. Offline setup can extract the same reviewed
archives with local tools.

## Replacing an existing source refresh helper

Some environments were provisioned with `/workspace/shared/private-skills-install.sh`
and `/workspace/shared/private-skills-refresh.py`. That helper clones `main`,
copies the whole repository into `private-skills/releases/<commit>`, and exposes
multiple skills through `private-skills/.agents/skills`. It is a source refresh,
not a per-skill release installation.

After publishing the release, replace that helper's invocation in the reusable
environment setup/maintenance settings with direct artifact delivery. First
record its existing discovery links, native settings and Token Audit delivery
configuration. Identify existing skill links before replacement: keep their
targets and backups, then replace only the selected skill package. Remove the old helper's discovery root from the
environment's configured skill search paths as well; do not leave duplicate
versions discoverable. Install the other desired skills explicitly, either by name or from the
compatible host bundle. Keep old source data until discovery and runtime checks pass;
removing historical repository copies is a separate cleanup operation.

Editing repository code does not update published environment settings. Do not
claim a running or future environment is migrated until its reusable setup has
actually changed and a fresh task has passed acceptance. Never embed tokens,
SSH keys, Codex auth files or other secrets in setup scripts or packages.

## Acceptance in the actual task

1. Read the installed `release-info.json` for version and source commit; verify
   package files against release metadata and inspect readable `SKILL.md` and references.
2. Confirm `$budget-aware-swarm` is actually discoverable in the task. Complete
   the [bundled host-readiness procedure](../skills/budget-aware-swarm/references/installation.md).
   A local file or a successful copy does not establish active-session readiness.
3. Inspect the actual model/effort, collaboration schema, supported child model
   choices and advertised total capacity. A Cloud service limit can be smaller
   than the scheme's 50-thread policy ceiling. Do not try to increase it by
   changing an unrelated local config or nesting workers around the limit.
4. Use normal bounded delegation when supported; requested child model/effort
   values are not receipts proving effective execution. Check separate project
   context requirements independently; Swarm itself requires no WikiLayer.

For source development **inside this repository**, the tracked
`.agents/skills/budget-aware-swarm` link still resolves to
`../../skills/budget-aware-swarm`. Verify it at the reviewed commit. That path
is intentionally tied to a checkout and is not an artifact deployment.

See [Cloud environments](https://learn.chatgpt.com/docs/environments/cloud-environments)
and [workspace model availability](https://learn.chatgpt.com/docs/enterprise/workspace-model-availability).
