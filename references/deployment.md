# Zero-to-one deployment

## Deployment contract

```text
Codex Desktop / CLI (model_provider = openai)
  -> 127.0.0.1:8318 transparent Authorization rewriter
  -> 127.0.0.1:8317 authenticated CLIProxyAPI
  -> native GPT, API-key routes, and explicitly authorized OAuth routes
```

The isolated `cli-proxy` profile talks to 8317 with command-backed authentication. It is created before Desktop activation and remains the fallback.

## Bootstrap behavior

`bootstrap` first discovers an existing CLIProxyAPI binary, config, and service. It adopts them without creating a second instance or auth directory.

For a new installation it resolves the official GitHub Release, selects the matching platform archive, verifies `checksums.txt`, rejects unsafe archive paths/links/devices, writes one user-local executable and owner-only loopback config, and creates a per-user service.

Do not install over an existing binary unless the user explicitly requested an upgrade. Never start two proxy processes against the same OAuth directory.

## Per-user services

- macOS: LaunchAgent with `RunAtLoad` and `KeepAlive`.
- Linux: `systemd --user`, `Restart=always`, enabled for `default.target`.
- Windows: limited current-user ONLOGON scheduled task.

The 8318 proxy uses the same service family. A service file is part of its deployment transaction.

## Transaction rules

Every apply operation records exact affected paths, before/after SHA-256 values, owner-only backups, and services created by the transaction. Rollback checks the after hash before restoring. `--force` is for a user-confirmed overwrite only.

Provider configs and their backups may contain API keys and must remain owner-only; transaction JSON never includes secret values.

## Desktop failure behavior

Desktop activation is a second transaction after profile activation. If its guard or health check fails, restore root `config.toml`, stop the newly created 8318 service, retain the working profile/catalog/helper, and report `codex --profile cli-proxy -m <model>`.
