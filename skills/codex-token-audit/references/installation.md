# Install and verify Token Audit on an execution host

An installation or update request covers package replacement and the local
setup needed to use it. Complete this procedure in the same task on every
requested host, including after Codex's generic skill-installer copies files.
That installer has no post-install hook; the installing agent performs the
connected operations here. Do not finish with only a file-count/test result.
An explicit local-only installation skips Drive setup and is reported as such.

## Package and local state

1. Identify the actual execution host, active Codex home and discovered skill
   path. For SSH, inspect the remote installation. Runtime initialization is
   authoritative; otherwise use that host's `CODEX_HOME` or `~/.codex`.
2. Install/update the complete `skills/codex-token-audit` package from the
   reviewed source, using skill-installer/staging and a retained backup. Keep
   unrelated settings and the existing destination; do not run the repository's
   Swarm installer to install Token Audit. When both skills were requested,
   complete each skill's own installation procedure.
3. Read the saved destination and check setup on that host:

   ```sh
   python3 /path/to/codex-token-audit/scripts/delivery_config.py show
   python3 /path/to/codex-token-audit/scripts/setup_delivery.py status
   ```

   `destination_required` and `delivery_check_required` both mean setup has
   more work. The latter deliberately exits 2 even when a folder is configured.
   Neither is an end-to-end success result. A malformed/unsafe config is a
   blocker to resolve without overwriting its evidence.

## Resolve the destination and transport

Use an existing authorized `gws` first; if missing or failing, use the Google
Drive integration already connected to the installing agent. The Python helper
cannot call MCP. Follow the [delivery reference](drive-delivery.md) for actual
tool operations and uncertainty/retry handling. Do not install another CLI,
transfer credentials between hosts, or build an OAuth flow.

Reuse the saved folder ID. If none is saved, recover the user's previously
selected ID from available task context, setup receipts or earlier chats before
asking them to repeat it. Verify that exact folder through the connected
account; a folder name alone does not replace an already selected destination.

If there is no selected destination, inspect My Drive root for an exact
`codex-token-audit` folder. Reuse one unambiguous private match. A request to
install this skill under turnkey setup authorizes creating that private root
folder when none exists; first reconcile an interrupted creation and do not
create duplicates. Multiple matches, an inaccessible selected folder, or an
account mismatch need clarification; do not silently choose another account or
destination. Verify folder type, non-trashed state, private sharing and parent
placement with metadata; verify write access where exposed and through the
synthetic delivery below. Preserve a deliberately selected different name.

For new setup, save the verified ID on the execution host, outside the
package/repository:

```sh
python3 /path/to/codex-token-audit/scripts/delivery_config.py configure \
  --drive-folder-id VERIFIED_FOLDER_ID --transport auto
python3 /path/to/codex-token-audit/scripts/delivery_config.py show
```

When a valid config already exists, keep its bytes unchanged after verifying
the saved folder; do not rerun the new-setup command. If the user requested a
destination change, pass the existing transport value instead of replacing it
with `auto`. For new setup,
`auto` gives gws priority and permits connector fallback without a separate
login. Folder IDs and credentials never go into Git or generic instructions.
Existing configuration is rechecked, not replaced just because this is an
update. Ordinary audit runs still use only supplied/configured destinations;
they do not create folders or reconfigure the host.

## Prove delivery with synthetic data

Create a new private output directory name and prepare a probe:

```sh
python3 /path/to/codex-token-audit/scripts/setup_delivery.py probe \
  --output-dir /path/to/private/new-install-probe
```

The helper generates an explicitly synthetic v2 bundle, reads no session logs
and refuses to overwrite an existing package. It reports the actual destination,
transport and package path. Deliver that package using the existing
`drive_export.py` workflow. For connector transport, execute and finish the
[connector delivery plan](drive-delivery.md#deliver-a-validated-bundle-through-the-plugin),
including downloading independent bytes and recording verified readback.
For gws failures, continue through connector fallback in the same task.
If gws completed using metadata-only verification, retain that completed receipt
unchanged. The connector does not upgrade a completed gws receipt: create a
fresh synthetic probe and run
`drive_export.py --package-dir NEW_PROBE --drive-transport connector` to get
its pending connector instruction (it uploads nothing and exits 1 by design),
then finish that probe's connector plan and independent byte readback.
Retain an uncertain receipt and reconcile it before retrying. Missing
authentication/tools is a concrete setup blocker, not a reason to claim ready.

After delivery finishes, run on the host that produced the probe:

```sh
python3 /path/to/codex-token-audit/scripts/setup_delivery.py verify \
  --package-dir /path/to/private/successfully-delivered-probe
```

Use the probe whose delivery succeeded, including `NEW_PROBE` after the
metadata-only gws fallback above.

Only `status=ready` with exit 0 completes delivery setup. It requires a complete
receipt for the exact synthetic manifest, current configured destination and
all immutable files, with independent byte verification. A queued plan, saved
folder ID, local validation or metadata-only upload is insufficient.

Finally check skill discovery and source/package identity. Report package and
delivery outcomes per host, the existing destination link and any blocker.
Keep synthetic packages/receipts private and outside Git; do not run or upload
a real task audit as an installation test. Replacing files does not prove an
already-running turn reloaded its instructions; check the next turn before
requiring a new chat or restart.
