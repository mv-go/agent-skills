# Install released skills

Skill source and release assets live in the same public `mv-go/agent-skills` repository. A push to `main` runs tests and semantic-release; Conventional Commits select the shared SemVer release: `fix` increments PATCH, `feat` increments MINOR, and `BREAKING CHANGE` or `!` increments MAJOR. Documentation, test, or chore changes alone do not release. With no prior tag, the first stable release is `v1.0.0`; there are no version bump commits. The release workflow includes archives, `release.json`, and `SHA256SUMS`.

## Find and pin a release

Use an authorized GitHub API connection to request `GET /repos/mv-go/agent-skills/releases/latest`. Read its `tag_name` and assets, then use that exact tag for every download and check in the operation. If `gh` is available, the equivalent lookup is:

```sh
gh release view --repo mv-go/agent-skills --json tagName,assets
```

The release's `release.json` maps each skill and host to an archive and SHA-256; `SHA256SUMS` provides the published checksums. Verify the selected archive against the manifest/checksum with a trusted local tool before extraction. Treat versions as numeric SemVer components, not lexical strings: for example, `1.10.0` is newer than `1.9.0`. An explicit version request uses its `vX.Y.Z` tag; do not re-resolve “latest” midway through an install or update.

| Asset | Contents |
| --- | --- |
| `<skill>-<version>.zip` | Universal skill archive with `codex/` and/or `claude/` trees |
| `<skill>-<version>-<host>.zip` | One host package at the archive root |
| `all-skills-<version>.zip` | Every skill and host under `<skill>/<host>/` |
| `all-skills-<version>-<host>.zip` | Every skill for one host under `<skill>/` |

For one skill, choose its host archive when available; the universal archive is a fallback. For several skills, choose the host bundle; the universal bundle can supply a host when needed. Inspect archive paths before extraction and reject unexpected paths or files.

Clients that can download Actions artifacts but cannot download private release
assets directly may use the `skill-release-<source-commit>` artifact from the
successful release workflow. It mirrors the published files for seven days.
Match its commit, version and checksums to the GitHub Release before installation;
the release remains the source of version and package metadata.

## Codex

Install the complete skill directory in the personal discovery location `~/.agents/skills/<skill>/` (or the corresponding `CODEX_HOME/skills/<skill>/` when that environment variable selects the Codex home). Verify the actual host's discovery path. Extract to a staging directory first, confirm `release-info.json` names the expected skill, host, version, and source commit, and check the complete package files are present. Swarm includes its entrypoint, agent metadata, references, and script; copying only `SKILL.md` is incomplete. Verify the skill is discoverable and readable in Codex after placing the package.

To update, compare the installed `release-info.json` version with the latest release using numeric SemVer, then repeat the download, verification, and staged replacement for the chosen version. If the metadata is missing, treat the install as a source or unknown-version copy and inspect it before replacing. Preserve local edits and unrelated configuration: compare existing package files, back up any changed files, and replace only the selected skill directory. No background update or installer state is provided.

Codex Cloud does not inherit a local installation. Repeat discovery and installation in the task environment, verify the skill is readable there, and check actual tools, model, and delegation behavior in that runtime. Follow any additional setup in the skill's packaged installation reference; see [Cloud setup and verification](cloud.md).

## Claude Code

For a persistent Swarm install, download `all-skills-<version>-claude.zip` and extract it to a stable local directory. It contains a ready `.claude-plugin/marketplace.json` and `budget-aware-swarm/` plugin. Add and install it with Claude's native CLI:

```sh
claude plugin marketplace add /absolute/path/to/extracted/bundle
claude plugin install budget-aware-swarm@tired-tools-releases --scope user
```

The marketplace uses the extracted plugin files; keep the directory in place. For an update, replace the extracted bundle with the verified new version, then run `claude plugin marketplace update tired-tools-releases` and `claude plugin update budget-aware-swarm@tired-tools-releases`. Restart Claude Code when requested because it maintains a plugin cache. Confirm the loaded plugin version after restart. A single `budget-aware-swarm-<version>-claude.zip` is also usable for a session with `claude --plugin-dir /absolute/path/to/extracted/plugin`. For `claude.ai/code` skill discovery, see [the Claude guide](claude.md).

## Maintainers

Review explicit package file lists in `releases/catalog.json`. Preview assets with `python3 scripts/build_release.py --version 0.0.0 --output dist/preview`. Python and Node dependencies are for maintainers and release CI only; individual skills may have their own runtime requirements. CI publishes from `main` through semantic-release. The generated archive contains `release-info.json` in each host package, recording skill, host, version, and source commit; check it to verify an installed version. Release assets remain in the same private repository and use the same access as source.
