# Codex CLI Model Bridge V2.1

Deploy verified Gemini, DeepSeek, GLM, Grok, or compatible custom models into Codex CLI and the Codex Desktop model picker while preserving ChatGPT login and existing task history.

Official repository: [i-YOLO/codex-cli-model-bridge](https://github.com/i-YOLO/codex-cli-model-bridge)

This is an MIT-licensed derivative of [Zhijian AI's `codex-cli-model-bridge`](https://github.com/zjp1997720/zhijian-skills/tree/main/skills/codex-cli-model-bridge). V2 adds zero-to-one installation, per-user services for macOS/Windows/Linux, API-key and OAuth provider onboarding, transactional rollback, a dependency-free Python transparent proxy, generic ProviderSpec support, and end-to-end Codex verification. V2.1 adds legacy/v2 compaction compatibility, cross-route checkpoint replay, and guarded service handoff. See [NOTICE](NOTICE).

## Architecture

```text
Codex Desktop / CLI (model_provider = openai)
  -> 127.0.0.1:8318 Authorization + compaction compatibility proxy
  -> 127.0.0.1:8317 CLIProxyAPI
  -> native GPT plus explicitly configured third-party routes
```

The model catalog controls picker metadata, not per-model Provider routing. Mixed Desktop mode therefore requires every selected route to be available through the single local Responses endpoint.

## Install the Skill

Copy this complete directory to:

```text
~/.codex/skills/codex-cli-model-bridge
```

Restart Codex and ask:

```text
Use $codex-cli-model-bridge to preserve my ChatGPT login and add DeepSeek V4 Pro to Codex Desktop.
```

Windows users may replace `python3` with `py -3`.

## Quick start

```bash
python3 scripts/bridge.py audit
python3 scripts/bridge.py bootstrap
python3 scripts/bridge.py bootstrap --apply
```

Never paste API keys into agent chat. Enter them in your own terminal using the hidden prompt:

```bash
python3 scripts/bridge.py credential set --provider deepseek
python3 scripts/bridge.py provider add --preset deepseek
python3 scripts/bridge.py provider add --preset deepseek \
  --expected-sha256 <approved-sha256> --apply
```

Activate the fallback profile and Desktop picker:

```bash
python3 scripts/bridge.py activate \
  --mode desktop \
  --models deepseek-v4-flash,deepseek-v4-pro \
  --apply
```

Verify the real Codex path:

```bash
python3 scripts/bridge.py verify \
  --desktop \
  --models deepseek-v4-flash,deepseek-v4-pro \
  --shell --tool-sequence --multi-agent
```

## Context compaction and route switches

In current Codex builds, `remote_compaction_v2=false` selects the retired `/responses/compact` endpoint; it does not mean local compaction. A cross-Provider or cross-route model switch may checkpoint immediately even when the thread is far below its context limit, while a same-route model switch may not.

Audit and dry-run first:

```bash
python3 scripts/bridge.py compaction audit --models all
python3 scripts/bridge.py compaction configure
```

The dry-run reports the complete config diff, `config_sha256`, transaction ID, exact backup paths, and rollback command. Only after confirming that 8318 is healthy and receiving explicit user approval, reuse those guards:

```bash
python3 scripts/bridge.py compaction configure \
  --expected-sha256 <approved-sha256> \
  --transaction-id <approved-transaction-id> \
  --apply
python3 scripts/bridge.py compaction verify --models all
```

Native GPT v2 compaction passes through. Gemini, GLM, DeepSeek, and other routed targets summarize with that same current target model; 8318 wraps the result as exactly one replayable `ocx1:` compaction item. Request bodies support identity, gzip, deflate, and Python 3.14 zstd. Unknown encodings are forwarded unchanged to avoid a blanket 415, but that bypass is not considered synthetic-compaction proof; a real zstd compaction and replay probe is still required. Failure never silently sends third-party history to GPT. The legacy `local-compaction` command is now only a deprecated alias and never writes the unsafe `false` setting.

See [Compaction and cross-route checkpoints](references/compaction.md) for the full protocol and rollout gates.

## Provider presets

- `gemini-api`: Gemini API key, with an optional user-supplied HTTPS relay.
- `antigravity-oauth`: explicit Antigravity OAuth with a provider-terms acknowledgement.
- `deepseek`: native Responses models including Flash, Pro, and Vision.
- `deepseek-chat-compat`: explicit fallback when native Responses accepts `agent_message` but drops its task body.
- `glm-standard`: ordinary usage-billed GLM API.
- `glm-coding-plan`: separate Coding Plan route; never infer this from key shape.
- `codex-oauth`: separate proxy-side Codex session for native GPT passthrough.
- `grok-oauth`: explicit Grok browser authorization.

For an unknown provider, copy [the ProviderSpec example](presets/generic-openai.example.json), fill in verified metadata, and pass it with `provider add --spec`. A model is supported only after its native Responses route or proxy translation passes the full Codex completion gate.

Multi-Agent verification requires the exact marker carried in the delegated task. HTTP 200 without that marker is a delivery failure and must not be reported as slow model inference.

## Gemini authentication

The Skill always asks the user to choose official API key, API key plus HTTPS relay, or Antigravity OAuth. It never imports tokens from Gemini CLI, Antigravity IDE, Codex Desktop, or another application.

OAuth requires explicit acknowledgement:

```bash
python3 scripts/bridge.py provider add --preset antigravity-oauth
python3 scripts/bridge.py provider add --preset antigravity-oauth \
  --expected-sha256 <approved-sha256> --apply
python3 scripts/bridge.py credential login \
  --preset antigravity-oauth \
  --ack-provider-terms --apply
```

## Rollback

Every V2 mutation records exact paths, hashes, private backups, and affected services. Compaction apply requires the SHA and transaction ID returned by an approved dry-run.

```bash
python3 scripts/bridge.py rollback --transaction <id>
python3 scripts/bridge.py rollback --transaction <id> --apply
```

Rollback refuses files changed after the transaction unless the user explicitly chooses `--force`.

## Security

- Ports 8317 and 8318 are loopback-only; remote upstreams must use HTTPS.
- Secrets never enter manifests, JSON state, stdout, chat, or release archives.
- Provider config backups may contain credentials and remain owner-only.
- OAuth is completed by the account owner in the browser.
- The Skill does not replace Codex Desktop `auth.json`, edit task rows, or overwrite unrelated Codex settings.
- Never apply an 8318 service or root-config change in the same turn as its dry-run. If 8318 is already not listening, stop and ask the user instead of attempting recovery.

## Development

```bash
python3 -m py_compile scripts/bridge.py scripts/deployment.py scripts/compaction.py scripts/transparent_proxy.py
python3 -m unittest discover -s tests -p 'test_*.py'
```

## License

MIT. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
