# Codex CLI Model Bridge V2

Deploy verified Gemini, DeepSeek, GLM, Grok, or compatible custom models into Codex CLI and the Codex Desktop model picker while preserving ChatGPT login and existing task history.

Official repository: [i-YOLO/codex-cli-model-bridge](https://github.com/i-YOLO/codex-cli-model-bridge)

This is an MIT-licensed derivative of [Zhijian AI's `codex-cli-model-bridge`](https://github.com/zjp1997720/zhijian-skills/tree/main/skills/codex-cli-model-bridge). V2 adds zero-to-one installation, per-user services for macOS/Windows/Linux, API-key and OAuth provider onboarding, transactional rollback, a dependency-free Python transparent proxy, generic ProviderSpec support, and end-to-end Codex verification. See [NOTICE](NOTICE).

## Architecture

```text
Codex Desktop / CLI (model_provider = openai)
  -> 127.0.0.1:8318 Authorization rewriter
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

If a route cannot return Codex's dedicated `compaction` item, preview and enable the local fallback:

```bash
python3 scripts/bridge.py local-compaction
python3 scripts/bridge.py local-compaction \
  --expected-sha256 <approved-sha256> --apply
```

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

Every V2 mutation records exact paths, hashes, private backups, and created services.

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

## Development

```bash
python3 -m py_compile scripts/bridge.py scripts/deployment.py scripts/transparent_proxy.py
python3 -m unittest discover -s tests -p 'test_*.py'
```

## License

MIT. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
