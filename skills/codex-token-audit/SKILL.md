---
name: codex-token-audit
description: >-
  Use when installing, updating, or setting up Codex Token Audit, or after a
  completed Codex task when the user asks to collect its token usage,
  cache, model and event telemetry into a machine-readable package and optionally
  deliver that package to an explicitly selected Google Drive folder. Not for
  account quota/reset questions alone.
---

# Codex token audit

Collect and package evidence for the explicitly selected completed task. This is
a deterministic collection and export skill, not an analysis or recommendation
workflow. Keep the audit's own usage outside the selected task. It does not
authorize changing the original task, runtime configuration, shared policy, or
installations.

## Install, update, or set up this skill

For an installation/update request, complete the
[host installation procedure](references/installation.md) on each requested
execution host. File copying is the package stage. Finish destination setup
and a verified synthetic delivery in the same task; report any missing access
as incomplete setup. Preserve an explicitly local-only request. The standalone
collector and ordinary audit permissions below are unchanged.

## Select the task

Use the exact root session and completed turn IDs supplied by the user or proved
by current thread metadata. Do not infer the target from the newest journal. If
needed, use `audit_usage.py --session-id ROOT_SESSION --list-turns` to identify
candidate completed turns without displaying prompts. Ask only when identity or
scope remains ambiguous.

Read [the evidence guide](references/evidence.md). Choose one or more completed
turn IDs belonging to the same root session. Keep each selected turn as a
separate window; never include intervening turns just to make a continuous time
range. The collector includes only provable descendants and available archived
records, deduplicates records, and reports gaps. Unsupported or missing
telemetry is unknown, not zero. Exclude this audit task and unrelated turns.

## Collect and validate

Run the local Python collector with an explicit session, each selected turn,
and a new private output directory. The collector is offline and uses the
bundled dated price profile unless another profile is explicitly selected.
Markdown is opt-in compatibility output.

```sh
python3 /path/to/codex-token-audit/scripts/audit_usage.py \
  --session-id ROOT_SESSION \
  --turn-id COMPLETED_TURN_A --turn-id COMPLETED_TURN_B \
  --output-dir /path/to/private-output
```

Optionally provide a caller-written `--task-label` (single line, 1–120
characters); never infer one from prompts, filenames or tool bodies.
Use the implemented CLI help for exact options. The package is a versioned set
of UTF-8 files: immutable `manifest.json`, `audit.json`, complete
`requests.jsonl` and `events.jsonl`, `schema.json`, and the included
`pricing.json` snapshot. Validate the finished bundle with
`bundle.validate_bundle(package_dir)` before any delivery. From the scripts
directory, for example:

```sh
PYTHONPATH=/path/to/codex-token-audit/scripts python3 -c \
  'import bundle, sys; bundle.validate_bundle(sys.argv[1]); print("Bundle valid")' \
  /path/to/package
```

Review its scope, coverage warnings, record counts, hashes, and validation
result; do not inspect or print raw prompts, tool bodies, or full source
journal lines.

The bundle also retains allowlisted runtime quota observations, explicit client
and response service-tier evidence, and source-linked tool and root-input
summaries when available. Quota is account-wide, unverified for account identity
and task attribution, and may cover only part of the task. Missing values are
unknown; a negative quota difference indicates decrease/reset, not consumption.
An `exec` wrapper does not establish nested operations, and tool intervals do not
measure model-billed time.

The bundle preserves exact raw counters, decimal equivalents, provenance,
unknowns, and mechanically computed summaries. Root usage is included in task
totals. Reasoning is included in output. Cache-write, long-context, and
speed-tier charges remain unknown unless the selected dated profile supports
them. API price equivalents are not subscription quota or an invoice. The
collector records evidence and boundaries; do not explain causes, recommend
workflow changes, or create analytical subagents in the ordinary skill run.

## Optional Drive delivery

Prefer the installed Google Workspace CLI (`gws`) for Google Drive operations,
including reads, destination setup, and delivery. If `gws` is absent or fails
to work, use the Google Drive integration already connected to ChatGPT/Codex.

Read the machine-local delivery setting with `python3
/path/to/codex-token-audit/scripts/delivery_config.py show`. Its default location
is `$CODEX_HOME/codex-token-audit.json` (normally
`~/.codex/codex-token-audit.json`), separate from the installed skill. It stores
the explicitly selected folder ID and transport, never credentials. CLI
`--drive-folder-id` and `--drive-transport` override their respective settings.
Malformed or unsafe settings block delivery instead of choosing another folder.

The default `transport=auto` selects `gws` when its executable is available on
`PATH`, otherwise `connector` (the Google Drive integration). Save `auto` for
this priority to adapt when `gws` is installed or removed. Explicit `gws` or
`connector` settings/CLI flags select the initial method. If the selected
`gws` fails authentication, a request, response parsing, or a timeout, the CLI
hands delivery to the connector workflow. Continue that workflow in the same
run; a CLI authorization error alone is not a delivery blocker when the
integration works. An existing connector receipt also resumes through the
integration. Before continuing an interrupted gws upload, reconcile the
original run folder through connector readback as described in the reference.
Local validation, identity, integrity, and sharing failures remain blocked.

For `transport=gws`, use the existing authorized Google Workspace CLI setup:
run `drive_export.py --package-dir PACKAGE_DIR` after validation, or let the
collector deliver. Add `--allow-shared` only when the user explicitly requests
a shared destination.

For `transport=connector`, use the Google Drive plugin already connected to
ChatGPT/Codex. No `gws` installation or separate Google login is needed. The
collector reports delivery as pending: finish the
[connector delivery workflow](references/drive-delivery.md) before replying.
Its helper packages the immutable files and manifest into one ZIP, preserving
empty JSONL files, then verifies independently downloaded archive bytes. It
does not call the plugin itself. A pending message is not delivery.

Connector delivery currently requires an unshared folder.
Both paths keep mutable state in local `delivery-receipt.json`, outside the
immutable inventory. A receipt records delivery, not source completeness.

During an ordinary audit, use only a supplied or configured destination ID;
never guess by name, change sharing, overwrite files, install tools, or configure
authentication. With no destination or usable transport, keep the
validated package local and report the blocker. When the user explicitly asks
to set up delivery and create a root folder, that separate request authorizes
creating it with the selected method, verifying its metadata, and saving its ID
with `delivery_config.py configure --drive-folder-id FOLDER_ID --transport
auto`. See the reference for the one-time setup and verification steps.

## Runtime reply

Reply in 2–4 short lines: selected scope, collection/coverage status, and either
the confirmed folder/manifest link or the local package path and delivery
blocker. Do not include tables, token interpretations, causal hypotheses, or
recommendations. Keep raw telemetry, prompts, tool output, private paths, and
package artifacts local. Never commit telemetry or copy it to Issues/WikiLayer.

## Standalone update and rollback

Update only the `codex-token-audit` skill directory; do not run the repository's
Swarm installer or alter Swarm files/settings. Set `AUDIT_SOURCE` to the reviewed
`skills/codex-token-audit` package and `AUDIT_TARGET` to its installed skill
directory. Stage a complete replacement beside the target, then retain the
previous directory or symlink as a backup:

```sh
AUDIT_SOURCE=/path/to/reviewed/skills/codex-token-audit
AUDIT_TARGET=/path/to/installed/skills/codex-token-audit
AUDIT_BACKUP="${AUDIT_TARGET}.backup-$(date -u +%Y%m%dT%H%M%SZ)"
AUDIT_STAGE=$(mktemp -d "${AUDIT_TARGET}.stage-XXXXXX")
cp -a "$AUDIT_SOURCE/." "$AUDIT_STAGE/"
test ! -e "$AUDIT_BACKUP" && test ! -L "$AUDIT_BACKUP" && \
  mv "$AUDIT_TARGET" "$AUDIT_BACKUP" && mv "$AUDIT_STAGE" "$AUDIT_TARGET"
```

After the updated skill loads and the host installation procedure completes,
retain or remove the backup according to local policy. To roll back, restore the
backup to the same target, retaining the failed package for inspection:

```sh
mv "$AUDIT_TARGET" "${AUDIT_TARGET}.failed-$(date -u +%Y%m%dT%H%M%SZ)" && \
  mv "$AUDIT_BACKUP" "$AUDIT_TARGET"
```

Do not include session logs or generated audit packages in the update.
