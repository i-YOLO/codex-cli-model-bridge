# Windows notes

Isolated profile is the default Windows path. Homebrew, LaunchAgents, and Codex Router are optional.

## What to install

1. Codex CLI for Windows, already logged in if Desktop history should stay on ChatGPT.
2. Python 3.11 or newer. Prefer `py -3` when `python3` is missing.
3. CLIProxyAPI on loopback. Practical sources:
   - [CLIProxyAPI GitHub Releases](https://github.com/router-for-me/CLIProxyAPI/releases)
   - [EasyCLIProxyAPI](https://github.com/router-for-me/EasyCLIProxyAPI) if a tray app is easier

Keep the proxy on `127.0.0.1`. Do not enable remote management.

## Paths

| Role | Location |
| --- | --- |
| Codex home | `%USERPROFILE%\.codex` or `$env:CODEX_HOME` |
| Isolated profile | `%USERPROFILE%\.codex\cli-proxy.config.toml` |
| Bridge state | `%USERPROFILE%\.config\codex-cli-model-bridge` |
| Credential helper | `%USERPROFILE%\.config\codex-cli-proxy\read-client-key.py` |
| CLIProxyAPI config | `CLIPROXYAPI_CONFIG`, `%USERPROFILE%\.cli-proxy-api\config.yaml`, or the EasyCLIProxyAPI `cpa-core\config.yaml` |

Unix mode `0600` is not a Windows ACL. Keep these files inside the current user profile and do not share them.

## Default workflow

```text
<python> <skill-dir>/scripts/bridge.py audit
<python> <skill-dir>/scripts/bridge.py bootstrap
<python> <skill-dir>/scripts/bridge.py bootstrap --apply
<python> <skill-dir>/scripts/bridge.py activate --mode desktop --models <ids> --apply
<python> <skill-dir>/scripts/bridge.py verify --desktop --models <ids> --shell
```

`activate` creates the isolated `cli-proxy` profile before attempting Desktop. If the Desktop guard fails, start Codex with `codex --profile cli-proxy -m <model>`. Do not rewrite root `model_provider` to `cli_proxy` when ChatGPT history should stay visible.

Pass `--proxy-config` and `--proxy-binary` when PATH discovery misses the Windows install.

## Desktop-transparent mode

The transparent proxy is implemented with the Python standard library. V2 registers a limited current-user ONLOGON scheduled task and starts it immediately. Its environment points only to the credential helper and loopback ports; API keys are not placed in the scheduled-task command.

If Task Scheduler registration is unavailable, activation starts a detached process for the current session and reports that persistence was not installed. Do not call the Desktop path fully deployed until persistence is verified.

## GLM Coding Plan

Do not run `npx @z_ai/coding-helper`. Isolated CLIProxyAPI can host GLM only after a Responses probe passes. Codex Router remains optional and is a Node process, not a Windows service from this Skill.

## What this Skill does not require

- Ruby
- Homebrew
- macOS LaunchAgents
- Node.js
- a second CLIProxyAPI on the same OAuth directory
