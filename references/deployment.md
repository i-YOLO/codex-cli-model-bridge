# Zero-to-one deployment

## Deployment contract

```text
Codex Desktop / CLI (model_provider = openai)
  -> 127.0.0.1:8318 transparent Authorization rewriter
  -> 127.0.0.1:8317 authenticated CLIProxyAPI
  -> native GPT, API-key routes, and explicitly authorized OAuth routes
```

The isolated `cli-proxy` profile talks to 8317 with command-backed authentication. It is created before Desktop activation and remains the fallback.

For Desktop compaction, prefer Python 3.14 or newer so the standard-library runtime can decode Codex `Content-Encoding: zstd` requests. Older interpreters may keep ordinary traffic alive by forwarding an unknown encoding unchanged, but the third-party synthetic-compaction gate remains unproven until a real zstd v2 and replay request succeeds.

## Bootstrap behavior

`bootstrap` first discovers an existing CLIProxyAPI binary, config, and service. It adopts them without creating a second instance or auth directory.

For a new installation it resolves the official GitHub Release, selects the matching platform archive, verifies `checksums.txt`, rejects unsafe archive paths/links/devices, writes one user-local executable and owner-only loopback config, and creates a per-user service.

Do not install over an existing binary unless the user explicitly requested an upgrade. Never start two proxy processes against the same OAuth directory.

## Per-user services

- macOS: LaunchAgent with `RunAtLoad` and `KeepAlive`.
- Linux: `systemd --user`, `Restart=always`, enabled for `default.target`.
- Windows: limited current-user ONLOGON scheduled task.

The 8318 proxy uses the same service family. A service file is part of its deployment transaction.

### Safe 8318 handoff

Treat a live 8318 replacement as a service migration, not a normal file copy.

1. Finish code changes and pass syntax plus offline tests.
2. Confirm 8318 is listening and `/v1/models` returns 200. If 8318 is absent, stop; do not recover it without a separate user decision.
3. Run `compaction configure` without `--apply`. Report the complete config diff, config SHA-256, transaction ID, exact backups, apply command, and rollback command.
4. Wait for explicit user approval. Apply must reuse the approved SHA and transaction ID.
5. On macOS, wait up to 10 seconds for asynchronous `bootout` removal before bootstrap; retry bootstrap only within the script's fixed bound.
6. Accept the handoff only when the service is running, `/v1/models` is 200, and the actual config diff exactly matches the preview.

Do not restart CLIProxyAPI merely to deploy the 8318 adapter. A CLIProxyAPI restart is a separate operation requiring explicit authorization and its own impact statement.

## Transaction rules

Every apply operation records exact affected paths, before/after SHA-256 values, owner-only backups, and affected services. Rollback checks the after hash before restoring. A compaction apply that replaces an existing service records that prior definition and restarts it after rollback. Failed transactions are persisted as `status=failed` with rollback and service-restoration outcomes. `--force` is for a user-confirmed overwrite only.

Provider configs and their backups may contain API keys and must remain owner-only; transaction JSON never includes secret values.

## Desktop failure behavior

Desktop activation is a second transaction after profile activation. If its guard or health check fails, restore root `config.toml`, stop the newly created 8318 service, retain the working profile/catalog/helper, and report `codex --profile cli-proxy -m <model>`.

## Catalog auto-refresh

`model_catalog_json` freezes the native model list; new upstream releases never appear until the catalog file is rebuilt. `bridge.py catalog-refresh` fetches the official catalog from `{chatgpt_base_url}/backend-api/codex/models?client_version=<installed codex version>` with the ChatGPT login from `auth.json`, caches it as `official-catalog.json` in the state directory, and rebuilds the managed catalog through the `sync` rules: native entries follow the official data, managed third-party entries stay aligned on `catalog-policy.json` → `managed_comp_hash`, and the transaction keeps owner-only backups plus a diff report.

On macOS, `bridge.py catalog-timer --apply` installs a user LaunchAgent (`com.zhijian.codex-cli-model-bridge.catalog-refresh`, default every 6 hours, `RunAtLoad`) so the refresh runs unattended; `--remove --apply` uninstalls it. The backend gates freshly released models by `client_version`, so a new model appears only after Codex itself is upgraded and the next refresh runs. On Windows and Linux, schedule `bridge.py catalog-refresh --apply` with schtasks or a systemd user timer instead. Refresh status and the last failure land in `catalog-refresh-status.json`; the log output of the timer is appended to `catalog-refresh.log` in the state directory.
