# Troubleshooting and rollback

## Failure classes

- **Invalid TOML**: restore the newest `config.toml.backup-*`, then repair only the Provider block.
- **Non-loopback Provider**: stop before rebinding; changing an existing shared endpoint can break other clients.
- **Credential helper failure**: verify the helper is owner-only and CLIProxyAPI contains a client key. Do not print helper output.
- **Bootstrap download rejected**: require an official Release asset and a matching `checksums.txt` entry. Do not bypass checksum validation or fall back to piping an installer into a shell.
- **Provider already exists**: a complete repeated definition is idempotent. A partial slug or alias collision is blocked; inspect ownership before using an explicit adoption path.
- **Route absent**: repair or authorize the upstream Provider before touching the Codex catalog.
- **`unknown provider for model gpt-5.6-sol`**: CLIProxyAPI returned HTTP 400 because WorkBuddy Fast aliases replaced the native Codex ID. Keep `oauth-model-alias` entries for `gpt-5.6-sol-standard` / `gpt-5.6-sol-fast` with `fork: true` so the original `gpt-5.6-sol` route stays listed. Do not point Codex at `gpt-5.6-sol-standard`; App Thread and `create_thread` still need the native slug.
- **`unknown provider for model grok-4.6` during a live Grok repair**: this is usually a mid-session CLIProxyAPI restart, not a missing Grok alias. Do not restart the proxy again from a Grok session; wait for `/v1/models` to list `grok-4.6` and continue.
- **Catalog collision**: preserve the manual entry. Use `--adopt` only after the user confirms this Skill should own that exact slug.
- **Listed but `codex exec` fails**: the route is not proven Responses-compatible; inspect proxy errors and do not declare success.
- **Shell fails with empty arguments, `missing field cmd`, or `incompatible payload`**: inspect the rollout for `{}` function arguments and the model catalog for an inherited `tool_mode = "code_mode_only"`. Compatibility proxies commonly translate the freeform `exec` tool into an empty-schema function for non-OpenAI models. Set `"tool_mode": null` only in the affected third-party manifest, resync, and require `probe --desktop --shell`; do not alter Shell permissions or disable code mode globally.
- **History disappeared after Provider work**: compare `state_5.sqlite` Provider counts with the root `model_provider`. Restore the dominant Provider with `restore-default`; do not rewrite thread rows.
- **Native GPT labels show Custom**: `model_provider = "cli_proxy"` is active globally. Restore the OpenAI Provider identity before enabling Desktop-transparent mode.
- **Third-party model absent from the normal desktop picker**: root `model_catalog_json` or Desktop-transparent mode is missing/stale. If `openai_base_url` is Codex Router on `127.0.0.1:4202`, fix the Router catalog instead of `configure-desktop`. Otherwise run `configure-desktop`, verify `127.0.0.1:8318`, and fully restart Codex. Use `codex --profile cli-proxy` only as a fallback.
- **GLM-5.3 missing while GPT history must stay**: follow [glm-coding-plan.md](glm-coding-plan.md). Do not overwrite `model_provider` with `ZAI`. Coding Plan keys fail on `/api/paas/v4` with `1113`; use the coding `/paas/v4` URL.
- **`bin/refresh-catalog` left Desktop on native catalog**: that command disables routing, and `--help` still runs it. Restore with `src/config-manager.mjs enable` after removing an unmanaged leftover `model_catalog_json` pointing at `native-catalog-pre-router.json`.
- **Transparent route returns 401**: Codex was pointed directly at authenticated port `8317`, or the header-rewriting proxy is down. Keep `openai_base_url` on `127.0.0.1:8318`; do not change `auth.json` from ChatGPT to API-key mode. Check the macOS LaunchAgent, Linux user unit, or Windows logon task for `transparent_proxy.py`.
- **ChatGPT plugins/account features disappear**: the root credential was switched to API-key auth. Restore the ChatGPT `auth.json` before continuing; never use `forced_login_method = "api"` as a probe against the shared Codex home.
- **WebSocket retries on a third-party model**: set the managed catalog entry's `prefer_websockets` to `false` and resync. HTTP Responses is the compatibility baseline.
- **Subagent fails with HTTP 422 and `ModelInput`**: Codex Multi-Agent v2 sent a private `agent_message` item that the third-party Responses endpoint does not deserialize. Enable CLIProxyAPI's official `codex.optimize-multi-agent-v2` compatibility transform and verify with `probe-multi-agent`. Keep the 8318 transparent proxy limited to header rewriting.
- **Multi-Agent probe returns 200 but not the marker**: the transport accepted `agent_message`, but the task body was dropped. This is a failure, not a slow model. DeepSeek native Responses has exhibited this shape; use the explicit Chat-compatibility preset only when the user needs Multi-Agent, then rerun text, tools, image, and task-delivery probes.
- **Profile list stale**: start a new profile-backed CLI task after the catalog is valid. Do not edit SQLite or app resources.
- **Bare `sync` reports missing command-backed auth**: V2 defaults `sync` to the isolated `cli-proxy.config.toml`. For an older fork, pass that profile explicitly instead of reading the root Desktop `openai` Provider.
- **Fast rejected**: remove `service_tier = "fast"` or use the default tier. Do not rename the model to imply Fast.
- **Model claims to be another vendor**: self-description is not routing evidence. Correlate the requested slug, alias, chosen CLIProxyAPI Provider, upstream result, response model, and Codex rollout.
- **Automatic compaction calls `/v1/responses/compact` and returns 404/501**: `features.remote_compaction_v2=false` selected Codex's retired legacy remote endpoint; it did not enable local compaction. Run `compaction configure` as a dry-run, review the complete diff and transaction, obtain explicit approval, and enable v2 through the 8318 compatibility proxy. Fully restart Codex afterward.
- **Changing models compacts immediately at low token usage**: compare the old and new route, not only their context-window sizes. Codex may checkpoint at a cross-route boundary so incompatible reasoning/encrypted state is not replayed directly. Same-route model changes may continue without compaction. Keep the boundary checkpoint and fix its v1/v2 transport; do not inflate catalog context limits or disable compaction.
- **`compaction_trigger` returns ordinary reasoning/message output**: the target route lacks native v2 output. The 8318 adapter must summarize with that same target model and return exactly one replayable `ocx1:` compaction item. Do not accept ordinary assistant text as success and do not send the history to GPT without explicit user authorization.
- **Switching back to native GPT fails on a prior reasoning item**: remove only the invalid third-party reasoning `content` field while preserving legal summary/encrypted state, then re-run compaction and replay probes. Do not rewrite rollout JSONL.
- **macOS 8318 disappears during service reload**: `launchctl bootout` is asynchronous. Wait until `launchctl print gui/<uid>/<label>` no longer finds the old job before bootstrap; bound bootstrap retries and confirm the service is running plus `/v1/models` is 200. If 8318 is already absent before an approved change, stop and ask the user instead of attempting recovery.

## Rollback

Backups stay next to their source:

- `~/.codex/config.toml.backup-<timestamp>`
- `~/.codex/cli-proxy.config.toml.backup-<timestamp>`
- `~/.codex/model-catalog-cli-proxy.json.backup-<timestamp>`

To roll back, copy the chosen backup over its source, preserve mode `0600`, and start a new Codex task. Restore both files when a Provider and catalog change were applied together.

`restore-default` also removes `openai_base_url`. The transparent proxy process or LaunchAgent may remain running harmlessly; stop it only through an explicit cleanup request after the root config has been restored.

On Windows, if `python3` is missing, retry with `py -3` or `python`. If `cliproxyapi` is not on PATH, pass `--proxy-binary` and `--proxy-config`. Isolated profile plus `codex --profile cli-proxy` is enough; do not block on LaunchAgents or Homebrew.

The bridge state file tracks ownership only. Removing it does not restore configuration; use the backups.

V2 commands additionally record transactions under the private state directory. Compaction service changes require the config SHA-256 and transaction ID returned by the approved dry-run. Their backup directory is reported before apply. Preview `rollback --transaction <id>` before applying it. Rollback refuses later edits by default; when the 8318 service existed before the transaction, rollback restores and restarts that previous definition rather than leaving the port down.
