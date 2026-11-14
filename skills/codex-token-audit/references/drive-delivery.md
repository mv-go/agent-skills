# Google Drive delivery

Use the installed Google Workspace CLI (`gws`) first for Drive reads, setup,
and delivery. Default `transport=auto` checks for `gws` on `PATH` and selects it
when present. If it is absent or does not work, use the `connector` transport:
the Google Drive integration connected to ChatGPT/Codex. Explicit transport
settings or flags select the initial method. CLI authentication/request errors,
invalid responses, incomplete listing, or a timeout trigger connector handoff.
Continue that workflow during the same audit; do not stop at the CLI error.
Validation, identity, integrity, and sharing failures remain blocked.

The `connector` transport is the Google Drive plugin connected to ChatGPT/Codex.
It uses existing plugin authentication. The Python helper has no network access:
the agent executes its plans through the actual Drive tools and supplies real
readback. The `gws` transport uses the existing CLI authorization.

## One-time setup

An installation/update request includes completing the
[host installation procedure](installation.md), including reuse/setup and a
verified synthetic delivery. Ordinary audit runs do not authorize setup.
If the user
asks for a new folder in My Drive root, check for an existing setup first; do
not create duplicates after an interrupted setup. When `gws` is installed, use
its `drive files` get/list/create operations and verify the returned folder
type, sharing, parent, and write capability before saving its ID. When `gws`
is absent or fails, reconcile any uncertain creation result and use the plugin's
`create_folder(name="codex-token-audit", parent_folder="root")`. Read the
returned ID with `get_file_metadata`, verify folder type and `shared=false`,
and compare its parent with `get_file_metadata(fileId="root")` when root
placement was requested. Never change sharing. Save the verified ID:

```sh
python3 /path/to/codex-token-audit/scripts/delivery_config.py configure \
  --drive-folder-id VERIFIED_FOLDER_ID --transport auto
python3 /path/to/codex-token-audit/scripts/delivery_config.py show
```

Settings are private JSON at `$CODEX_HOME/codex-token-audit.json`, defaulting to
`~/.codex/codex-token-audit.json`. They survive skill replacement. Never put
destination IDs or account settings into a repository. To switch an explicitly
chosen destination, configure the new ID or pass `--drive-folder-id` for a run.
The default transport without configuration is `auto`. Use `--transport auto`
to update a previous saved `connector` or `gws` preference while keeping the
same verified folder ID. Do not install or authenticate a CLI during an audit.

Verify setup with a clearly labeled synthetic valid bundle containing no real
session data. For `gws`, run `drive_export.py` and verify its confirmed receipt.
For the integration, upload its archive using the workflow below and compare
every archived member after download. Keep the local proof and confirmed receipt
private. Ordinary audits
collect only the user-selected completed task, then follow the same delivery
workflow for its validated package.

## Deliver a validated bundle through the plugin

1. Validate the immutable package. Read the configured destination metadata via
   `get_file_metadata` and save its exact `structuredContent` object as a local
   JSON file. This plugin normalizes fields to `id`, `title`, `mime_type`,
   `parent_ids`, `shared`, `drive_id`, and `url`; requested checksum/capability
   fields may be absent. Both destination and run folder must be unshared
   (`shared` is `false`), outside a shared drive (`drive_id` null or absent)
   and not trashed.

2. Ask the helper for one operation:

   ```sh
   python3 /path/to/codex-token-audit/scripts/connector_export.py next \
     --package-dir PACKAGE_DIR --drive-folder-id VERIFIED_FOLDER_ID \
     --destination-metadata DESTINATION_JSON
   ```

   The helper prepares a deterministic ZIP containing exactly the immutable
   inventory and `manifest.json`, preserving empty JSONL files. This avoids the
   plugin's rejection of zero-byte file uploads. Receipts, settings, and locks
   are excluded. After the run folder exists, also pass
   `--folder-metadata RUN_FOLDER_JSON`.
   Refresh destination and run-folder metadata before every new write. Save
   tool-derived JSON without interpolating it into shell commands; preserve
   ordinary file permissions and keep metadata/receipts outside the payload.

3. `create_folder` and `upload_file` plans include exact `name`, `parent_id`,
   and, for the ZIP, `local_path`. Before a mutation, search for that exact name
   under that parent using Drive search's raw `q` filter and follow every page.
   If a match already exists, reconcile the original creation result or stop;
   never adopt by name, overwrite, or create a second object. Then execute the
   single planned tool call: `create_folder` uses `parent_folder`, while
   `upload_file` uses `parent_folder_id`, `file_uri=local_path`, and
   `mime_type="application/zip"`. Retain the returned creation ID. The helper
   already persisted pending state before
   emitting the plan.

4. Read that exact created ID through `get_file_metadata` and save the returned
   normalized object. For the ZIP, also call `fetch` with its observed Drive
   URL, `download_raw_file=true`, and `include_base64=false`. Use the resulting
   authenticated local download (or materialize the returned file reference
   using the runtime's supported mechanism). Never use the original local
   payload or generated ZIP as the download, and never print raw bytes or
   inline base64.

5. Acknowledge the independently verified result:

   ```sh
   python3 /path/to/codex-token-audit/scripts/connector_export.py record \
     --package-dir PACKAGE_DIR --created-id TOOL_CREATION_ID \
     --metadata READBACK_JSON --downloaded-file RAW_DOWNLOAD_PATH
   ```

   Omit `--downloaded-file` for the folder. The helper verifies the creation ID
   against readback, expected name/parent/type/sharing, and the downloaded
   archive's byte count and SHA-256 against the archive prepared from the
   unchanged validated package. The receipt enumerates every original member
   with its source hash and size, bound to the verified archive ID. Record only
   actual tool-derived IDs, metadata, and downloaded paths.

6. Repeat `next`, execute, read back, and `record` until `next` returns
   `status=complete`. Completion includes every inventory payload and
   `manifest.json` inside the downloaded ZIP. Keep local
   `delivery-receipt.json`; use only confirmed provider links in the response.
   Report coverage and delivery separately.

## Interrupted or blocked delivery

For a gws failure, run connector `next` with current destination metadata.
If gws failed before any remote write, the helper safely converts its empty
receipt and emits the normal connector plan. A completed gws receipt remains
complete. A connector receipt resumes its existing workflow without retrying
gws.

If a gws run folder ID is already recorded, `read_gws_folder` names the exact
`folder_id` to fetch. Read it through the integration, save its normalized
metadata, and rerun `next` with `--folder-metadata`. After checking its original
ID, exact name, parent, type, and unshared state, the helper preserves the
original gws receipt under `previous_gws_delivery` and continues in the same
run folder. Existing raw files and uncertain raw-file writes are left alone;
the connector uploads one complete ZIP instead of repeating those writes.

If gws may have created the folder but did not record its ID,
`reconcile_gws_folder` is a read-only plan containing `parent_id`, `name`,
`run_id`, and `property`. Search all pages with the exact parent, name,
folder MIME type, non-trashed state, and the original run property:

```text
'PARENT_ID' in parents and name = 'EXACT_NAME' and trashed = false
and mimeType = 'application/vnd.google-apps.folder'
and properties has { key='tokenAuditRunId' and value='RUN_ID' }
```

New gws run folders carry this cross-application `properties` marker alongside
their existing private `appProperties` marker. The latter may be invisible to
the connector's OAuth app; use the public custom-property field for the query
([Google Drive custom properties](https://developers.google.com/workspace/drive/api/guides/properties)).
Legacy folders with only private properties can remain unresolved unless the
original creation result proves their ID. Never substitute a name-only match.

Only a unique result from this full query establishes the original creation
identity. Read that result's exact ID independently, then supply its normalized
metadata using `--folder-metadata` to continue. A name-only search is
insufficient. With no match or multiple matches, keep reconciliation pending;
do not create another folder or delete/edit the original receipt. The helper
validates supplied evidence, while the agent must perform the actual property
query and readback. Invalid evidence leaves the original receipt unchanged.

When `next` returns status `in progress` with action `reconcile_pending`, perform read-only reconciliation of
that original operation. Use its creation ID from the tool result, read it
back, fetch bytes for a file, and call `record`. Do not execute another create
or upload. If the original ID cannot be established, stop and report the
pending operation; an empty search is not proof that a write did not occur.
Never delete/edit the receipt to force a retry. Unknown sharing, mismatched
metadata/bytes, changed local payloads, and conflicting state block delivery.

The helper verifies supplied evidence; the actual plugin calls remain the
agent's responsibility. CLI output saying `pending` is a handoff to this
workflow, never a completed upload.
