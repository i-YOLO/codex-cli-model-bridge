---
name: codex-cli-model-bridge
description: Install, deploy, audit, repair, and roll back third-party models in Codex CLI or the Codex Desktop model picker through a loopback CLIProxyAPI bridge. Use for Gemini, DeepSeek, GLM, Grok, provider API keys or relays, subscription OAuth, custom model catalogs, missing model-picker entries, provider/history mismatches, Fast mode, tool-call incompatibility, multi-agent failures, or compaction failures. Preserve ChatGPT login, existing task history, unrelated Codex configuration, and credentials.
---

# Codex CLI Model Bridge

Deploy verified third-party models without replacing a healthy ChatGPT login or hiding the user's dominant task history.

Codex chooses one Provider for a task; catalog entries do not carry their own Provider routing. The default Desktop design therefore keeps `model_provider = "openai"`, points the built-in Provider at an owner-only loopback authorization and compaction adapter on `127.0.0.1:8318`, and lets CLIProxyAPI on `127.0.0.1:8317` route native GPT and third-party models. Always create the isolated `cli-proxy` profile first so it remains available for validation and rollback.

This Skill supports a model only when it has a native Responses route or a CLIProxyAPI adapter that passes the Codex completion gate. A name in `/v1/models` is not proof of compatibility.

## Resolve the entry point

Resolve this Skill directory as `<skill-dir>`. Resolve `<python>` as the first available of `python3`, `py -3`, and `python`.

```bash
<python> <skill-dir>/scripts/bridge.py <command>
```

All mutating commands preview by default and require `--apply`. Never put API keys or tokens in command arguments. For any command that changes the 8318 service or root Codex config, do not apply in the same turn as the preview: report the exact diff, config SHA-256, transaction ID, backup paths, apply command, and rollback command, then wait for explicit user confirmation.

## Route the request

- New machine or missing CLIProxyAPI: read [deployment.md](references/deployment.md), then use `bootstrap`.
- Add Gemini, DeepSeek, GLM, Grok, an API relay, or an unknown compatible model: read [providers.md](references/providers.md).
- Add or change model metadata: read [model-manifests.md](references/model-manifests.md).
- Windows: also read [windows.md](references/windows.md).
- GLM Coding Plan: also read [glm-coding-plan.md](references/glm-coding-plan.md); never confuse it with ordinary GLM API billing.
- Diagnose or roll back an existing bridge: read [troubleshooting.md](references/troubleshooting.md).
- Diagnose compaction or cross-route model switching, or change the 8318 service: read [compaction.md](references/compaction.md).

## Required workflow

### 1. Audit before mutation

```bash
<python> <skill-dir>/scripts/bridge.py audit
```

Record, without exposing secrets:

- Codex and CLIProxyAPI versions, config validity, file permissions, and loopback health;
- root `model_provider`, indexed task counts by Provider, SQLite integrity, and task-inventory digest;
- ChatGPT login mode and token presence, never token values;
- active bridge mode, catalog ownership, live routes, missing routes, and stale entries.

If the root Provider differs from the dominant indexed-history Provider, restore history before model work. Never rewrite task rows.

### 2. Bootstrap or adopt CLIProxyAPI

```bash
<python> <skill-dir>/scripts/bridge.py bootstrap
<python> <skill-dir>/scripts/bridge.py bootstrap --apply
```

Show the binary, config, service, and release actions before applying. Existing installations are adopted rather than duplicated. New installations download an official GitHub Release for the detected OS/architecture, verify `checksums.txt`, bind only to loopback, disable remote management, generate a local client key, and install a per-user macOS LaunchAgent, Linux systemd unit, or Windows logon task. Do not pipe a remote installer into a shell.

### 3. Choose the Provider and credential method

For Gemini, always ask whether the user wants an official Gemini API key, an API key plus custom HTTPS relay, or Antigravity OAuth. Do not infer the choice from existing files. OAuth requires an explicit provider-terms acknowledgement and a browser action by the user. Never copy OAuth tokens from Codex Desktop, Gemini CLI, Antigravity IDE, or another app.

For an API key, tell the user to run the hidden local prompt themselves:

```bash
<python> <skill-dir>/scripts/bridge.py credential set --provider <provider-id>
```

Then preview and apply the provider definition:

```bash
<python> <skill-dir>/scripts/bridge.py provider add --preset <preset>
<python> <skill-dir>/scripts/bridge.py provider add --preset <preset> \
  --expected-sha256 <approved-sha256> --apply
```

For OAuth, first add required aliases, then let the user authorize locally:

```bash
<python> <skill-dir>/scripts/bridge.py provider add --preset <oauth-preset>
<python> <skill-dir>/scripts/bridge.py provider add --preset <oauth-preset> \
  --expected-sha256 <approved-sha256> --apply
<python> <skill-dir>/scripts/bridge.py credential login --preset <oauth-preset> \
  --ack-provider-terms --apply
```

Use `--base-url <https-url>` only when the user explicitly supplies a relay. Plain HTTP is allowed only for loopback. Treat API keys, OAuth codes, account emails, auth files, and credential-bearing responses as secrets.

### 4. Create the fallback profile and activate Desktop

```bash
<python> <skill-dir>/scripts/bridge.py activate --mode desktop --models <ids>
<python> <skill-dir>/scripts/bridge.py activate --mode desktop --models <ids> --apply
```

Activation always creates `cli-proxy.config.toml` first. Desktop activation is allowed only when `openai` owns the majority of indexed task history, `auth.json` remains a healthy ChatGPT login, CLIProxyAPI can route the selected native/default GPT model, both listeners are loopback-only, and selected models exist in the verified catalog.

If Desktop activation fails, restore the root configuration and keep the isolated profile. Report the exact profile command instead of claiming the Desktop picker is ready.

The transparent proxy is dependency-free Python and supports HTTP/SSE and WebSocket Upgrade. Ordinary traffic changes only the downstream Authorization/Host headers; compaction-related Responses bodies are parsed under the bounded compatibility contract. It never logs credentials, request bodies, or summaries.

### 5. Repair and verify compaction

`remote_compaction_v2=false` is not local compaction in current Codex Desktop/Core builds. It selects the retired `POST /responses/compact` path and can kill a task with 404 or 501. Cross-route model changes may trigger compaction immediately even when the thread is far below its token limit; same-route model changes may not. Do not try to fix this by inflating model context metadata or disabling compaction.

Audit both protocols and every selected route:

```bash
<python> <skill-dir>/scripts/bridge.py compaction audit --models all
```

Before any service change, verify that 8318 is currently listening. If it is absent, stop and ask the user how to recover it. Do not improvise a restart. Preview the exact change:

```bash
<python> <skill-dir>/scripts/bridge.py compaction configure
```

After the user explicitly approves that preview, reuse both returned guards:

```bash
<python> <skill-dir>/scripts/bridge.py compaction configure \
  --expected-sha256 <approved-sha256> \
  --transaction-id <approved-transaction-id> \
  --apply
```

The apply path must preserve the active default model, `model_provider`, ChatGPT auth file, and task inventory. On macOS it waits until asynchronous LaunchAgent removal is complete before bootstrap and bounds bootstrap retries. A failed transaction restores the previous files and previous service definition, records `status=failed`, and stops; do not keep trying different service commands.

Then fully restart Codex and verify legacy v1, remote v2, replay, and an ordinary response for every route:

```bash
<python> <skill-dir>/scripts/bridge.py compaction verify --models all
```

The 8318 compatibility layer passes native OpenAI compaction through. For a third-party target route it asks that same current model to create the checkpoint, wraps it as one `ocx1:` compaction item, and decodes it on replay. It handles identity, gzip/x-gzip, deflate, and Python 3.14 zstd bodies; an unknown encoding is forwarded unchanged instead of causing a blanket 415, but that bypass is not proof that synthetic compaction ran. It never sends third-party task history to GPT as an automatic fallback. The `local-compaction` command is retained only as a deprecated alias to `compaction configure`; it must never write `remote_compaction_v2=false`.

### 6. Verify the actual Codex path

```bash
<python> <skill-dir>/scripts/bridge.py verify \
  --desktop --models <comma-separated-ids> \
  --shell --tool-sequence --multi-agent
```

Completion requires a live route, an ephemeral Codex text response, a recorded real `pwd`, ordered `pwd` then `git --version` tool events, Multi-Agent task delivery when claimed, image input when claimed, a unique compaction item plus successful replay, a second catalog sync with no changes, and unchanged ChatGPT login/task inventory. HTTP 200 alone does not prove Multi-Agent: the response must contain the exact marker carried inside the `agent_message` task body.

`probe` hashes the active root/profile config before and after execution. If it changes during the probe, fail the gate and report the concurrent change; never silently restore over a possible user edit.

Never identify the upstream model from its self-description. Use the requested slug, CLIProxyAPI Provider selection, route mapping, HTTP result, response model, and Codex rollout events.

## Known compatibility rules

- Codex custom Providers use the Responses wire API. A Chat Completions-only route needs a proven CLIProxyAPI translation.
- External manifests must clear inherited native lifecycle metadata by setting generated `upgrade` to `null`.
- Fast is `service_tier = "fast"`, not a cosmetic `*-fast` model alias.
- If a third-party model inherited `tool_mode = "code_mode_only"` and Shell payloads are empty, set `tool_mode: null` only for that manifest and retest.
- Multi-Agent v2 may send private `agent_message` items. Use CLIProxyAPI's official compatibility transform; do not rewrite it in the 8318 proxy.
- DeepSeek native Responses may return HTTP 200 while dropping the delegated `agent_message` body. If strict delivery fails and Multi-Agent is required, use the separately declared Chat-compatibility preset and rerun every probe; do not silently switch adapters.
- A model that returns ordinary reasoning/message items for `compaction_trigger` needs the 8318 synthetic v2 adapter. Do not call ordinary assistant text successful compaction.
- A cross-route switch can trigger a pre-turn checkpoint at low token usage. Verify route-boundary switches separately from threshold-driven auto-compaction.
- Synthetic checkpoints use the current target model only. Retry transient transport failures on that route within a small bound; never cross Provider boundaries silently.
- `auth_unavailable` can mean a credential failure or temporary cooldown after EOF/HTTP2 failure. Classify the original upstream status before reauthorizing or changing retry policy.
- Keep Codex CLI and Desktop catalog formats compatible. A CLI parse failure is not evidence that the upstream model failed.

## Rollback

Every V2 mutation records exact before/after hashes and private backups.

```bash
<python> <skill-dir>/scripts/bridge.py rollback --transaction <id>
<python> <skill-dir>/scripts/bridge.py rollback --transaction <id> --apply
```

Rollback refuses files changed after the transaction unless the user explicitly chooses `--force`. Stop only the service recorded by that transaction; when a service existed before the change, restore and restart its previous definition. Do not delete unrelated credentials, catalogs, or services. The legacy `restore-default` remains available for Provider/history repair.

## Report

Report active mode, Provider identity, versions, loopback endpoints, models added or preserved, probes, compaction policy, task-inventory digest, transaction IDs, reload action, and rollback command. Omit secrets and account identifiers.
