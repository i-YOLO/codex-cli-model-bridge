# Provider onboarding

## Presets

| Preset | Credential | Adapter | Notes |
|---|---|---|---|
| `gemini-api` | API key | `gemini-api-key` | Official Gemini API; `--base-url` may select a user-provided HTTPS relay. |
| `antigravity-oauth` | OAuth | Antigravity | Explicit choice and terms acknowledgement; friendly aliases use `fork: true`. |
| `deepseek` | API key | `codex-api-key` | Native Responses; includes Flash, Pro, and Vision manifests. |
| `deepseek-chat-compat` | API key | `openai-compatibility` | Explicit fallback when native Responses returns 200 but drops Multi-Agent task content. Must be probed independently. |
| `glm-standard` | API key | `openai-compatibility` | Ordinary pay-as-you-go API, not Coding Plan. |
| `glm-coding-plan` | API key | `openai-compatibility` | Separate URL and policy; also read `glm-coding-plan.md`. |
| `codex-oauth` | OAuth | Codex | Separate proxy session required for native GPT passthrough in mixed Desktop mode. |
| `grok-oauth` | OAuth | Grok | Verify Multi-Agent compatibility. |

## API-key flow

Never ask the user to paste a key into chat. `credential set` uses a hidden prompt and writes a temporary owner-only staging file. `provider add --apply` consumes it into the owner-only proxy config and removes the staging file. The key never enters a manifest, transaction JSON, output, or archive.

Provider additions preserve unrelated YAML and refuse partial alias collisions. Repeating an already complete definition is idempotent. Remote HTTP endpoints are rejected unless loopback.

## OAuth flow

`provider add` writes only required aliases. `credential login` starts CLIProxyAPI's documented browser flow after acknowledgement. OAuth files stay in CLIProxyAPI's configured auth directory. Never reuse or transform another application's credentials, and never print browser codes or auth contents into chat.

## Generic ProviderSpec

Start from `presets/generic-openai.example.json` and pass it with `--spec`.

Required top-level fields are `schema_version`, `id`, `adapter`, `auth_mode`, `base_url`, `routing_prefix`, and `models`.

Known bundled slugs inherit their maintained manifest. Each unknown model must additionally declare display name, description, native template, context window, effective percentage, default and supported reasoning levels, modalities, and priority. Add transport overrides only with evidence.

The generated manifest is metadata, not proof. Require `/v1/models`, text, real Shell, multi-tool, Multi-Agent when claimed, image when claimed, and compaction/fallback validation.

Multi-Agent validation must assert that the model returns the exact marker embedded in the delegated task. A 200 response with “no task received” is a failure. Do not describe the duration of this private protocol probe as normal model response speed.

## Model identity

Never accept model self-description as routing evidence. Verify request slug, alias mapping, selected proxy Provider/credential class, upstream status, response model, and Codex rollout events.
