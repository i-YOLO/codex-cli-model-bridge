# Codex CLI Model Bridge V2.1.1

在保留 Codex ChatGPT 登录和既有任务历史的前提下，把经过真实验收的 Gemini、DeepSeek、GLM、Grok 或自定义兼容模型接入 Codex CLI 与 Codex Desktop 原生模型选择器。

正式仓库：[i-YOLO/codex-cli-model-bridge](https://github.com/i-YOLO/codex-cli-model-bridge)

本项目基于 [Zhijian AI 的 MIT `codex-cli-model-bridge`](https://github.com/zjp1997720/zhijian-skills/tree/main/skills/codex-cli-model-bridge) 升级。V2 新增从零安装、三端用户级常驻、API Key/OAuth Provider、事务回滚、Python 透明代理、通用 ProviderSpec 和完整部署验收；V2.1 补齐旧／新双协议上下文压缩、跨路由切换回放和安全服务切换；V2.1.1 增加 zstd 请求体支持、未知编码安全透传、异步服务重启加固、comp_hash 对齐指引和请求捕获诊断。详见 [NOTICE](NOTICE)。

## 能做什么

- 自动审计 Codex 登录、Provider、任务历史、模型目录和本机代理状态。
- 采用或校验安装 CLIProxyAPI，不执行浮动的远程 Shell 安装器。
- 支持官方 API Key、用户指定的 HTTPS 中转站，以及 CLIProxyAPI 已支持的 OAuth 通道。
- 默认先建隔离 Profile，再安全启用 Desktop 混合模型菜单。
- 每次写入都有预览、哈希保护、私有备份和可执行回滚。
- 用 Codex 自身验证文本、Shell、连续工具、多 Agent 和压缩兼容，而不是只看模型名称。

“支持任何模型”是有边界的：模型必须原生支持 Responses，或能由 CLIProxyAPI 的已支持适配器完成可靠转换，并通过端到端探针。

## 架构

```text
Codex Desktop / CLI
  model_provider = openai
          │
          ▼
127.0.0.1:8318  按模型分流的透明网关
  ├── 原生模型（GPT-5.6／GPT-6 Astra…）
  │     └── 原样直连 chatgpt.com 官方后端（使用 Desktop 自己的登录与原始请求头）
  └── 第三方模型（Gemini／DeepSeek／GLM／Grok…）
        └── 127.0.0.1:8317  CLIProxyAPI
              ├── Gemini API Key / Antigravity OAuth
              ├── DeepSeek Responses
              ├── GLM 普通 API / Coding Plan
              └── 其他已验证兼容 Provider
```

- **原生模型直连**：官方后端按客户端版本门控新模型，直连转发使用 Desktop 真实的登录与请求头，新模型永远以最新客户端身份出现；原生额度跟随 Desktop 登录的账号，CPA 重启或故障不影响原生模型。
- **第三方隔离**：第三方流量进入 CLIProxyAPI 凭据池，8318 负责双协议压缩适配、`ocx1:` 回放与 reasoning 清理。
- **无 WebSocket**：8318 按请求体里的 `model` 分流，而 WebSocket 连接建立时无法得知模型，因此目录内所有条目 `prefer_websockets=false`，全部走 HTTPS 流式（SSE）。
- 模型目录只决定“菜单里显示什么”，不会决定每个模型走哪个 Provider。混合菜单依赖 8318 的按模型分流与 CLIProxyAPI 的第三方路由。

## 安装 Skill

将整个目录放到：

```text
~/.codex/skills/codex-cli-model-bridge
```

重新打开 Codex，然后直接说：

```text
使用 $codex-cli-model-bridge，保留我的 ChatGPT 登录，把 DeepSeek V4 Pro 加到 Codex Desktop。
```

也可以直接调用脚本：

```bash
python3 ~/.codex/skills/codex-cli-model-bridge/scripts/bridge.py --help
```

Windows 没有 `python3` 时使用 `py -3`。

## 最短部署流程

先审计并预览底层代理部署：

```bash
python3 scripts/bridge.py audit
python3 scripts/bridge.py bootstrap
python3 scripts/bridge.py bootstrap --apply
```

API Key 不要发给 Agent。在你自己的终端执行隐藏输入：

```bash
python3 scripts/bridge.py credential set --provider deepseek
python3 scripts/bridge.py provider add --preset deepseek
python3 scripts/bridge.py provider add --preset deepseek \
  --expected-sha256 <预览返回的哈希> --apply
```

创建隔离 Profile 并启用 Desktop：

```bash
python3 scripts/bridge.py activate \
  --mode desktop \
  --models deepseek-v4-flash,deepseek-v4-pro \
  --apply
```

完整验收：

```bash
python3 scripts/bridge.py verify \
  --desktop \
  --models deepseek-v4-flash,deepseek-v4-pro \
  --shell --tool-sequence --multi-agent
```

## 上下文压缩与跨路由切换

当前 Codex 中，`remote_compaction_v2=false` 会选择已退役的 `/responses/compact`，并不代表“本地压缩”。跨 Provider／路由切换还可能在远低于上下文上限时立即做一次兼容性压缩；同一路由内换模型则可能不触发。

先审计，再 dry-run：

```bash
python3 scripts/bridge.py compaction audit --models all
python3 scripts/bridge.py compaction configure
```

dry-run 会返回完整配置 diff、`config_sha256`、事务 ID、精确备份路径和回滚命令。确认 8318 当前健康并由用户明确批准后，才使用同一组保护值执行：

```bash
python3 scripts/bridge.py compaction configure \
  --expected-sha256 <已批准的哈希> \
  --transaction-id <已批准的事务ID> \
  --apply
python3 scripts/bridge.py compaction verify --models all
```

原生 GPT 的 v2 压缩保持透传。Gemini、GLM、DeepSeek 等路由由当前目标模型自己生成摘要，8318 将其封装为唯一 `ocx1:` compaction item，并在下一轮解码回放；失败不会偷偷把第三方历史发送给 GPT。请求体支持 identity、gzip、deflate 和 Python 3.14 的 zstd；无法识别的编码会原样透传，避免让普通 Codex 请求统一报 415，但仍须通过真实 zstd 压缩／回放探针才算兼容。旧 `local-compaction` 只保留为兼容别名，不会再写入危险的 `false`。

跨路由切换的压缩触发与上下文用量无关：Codex 比较模型目录条目的 `comp_hash`，新旧不同即在回合开始前触发远程压缩（`reason: comp_hash_changed`）。该请求是**不带对话内容的 GET**，只有仍持有线程状态的服务端才能应答；CLIProxyAPI 是无状态转发，第三方路由必然失败并报 `expected exactly one compaction output item`。因此本 Skill 的模型目录让所有第三方模型共用同一个 `comp_hash`，从源头避免该触发；原生 GPT 条目保持上游原值，重建目录时必须保持这一对齐。

完整协议、跨路由触发依据和安全上线闸门见 [上下文压缩说明](references/compaction.md)。

## 请求捕获诊断

排查压缩或路由的疑难请求形状时，创建 `~/.config/codex-cli-model-bridge/capture.enabled` 并重现一次；代理会把请求行、白名单请求头与解码后的请求体写入同目录的 `capture/` 子文件夹（绝不写入 `Authorization` 或其他凭据头）。诊断结束删除哨兵文件与 `capture/` 目录即可关闭。

## 官方模型目录自动刷新

`model_catalog_json` 是静态文件，Codex 设置它之后就不再刷新官方目录缓存，官方新模型不会自己出现。Skill 提供两层机制：

- 手动：`python3 scripts/bridge.py catalog-refresh`（预览）→ 加 `--apply` 落盘。命令用本机 `~/.codex/auth.json` 的 ChatGPT 登录直接读取官方目录（凭据只在本进程内存中使用，绝不打印或写日志），随后复用 `sync` 重建目录：原生层取官方最新，第三方受管条目保留。
- 自动（macOS）：`python3 scripts/bridge.py catalog-timer --apply` 安装用户级 LaunchAgent，每 6 小时自动执行一次刷新（`--interval-seconds` 可调，`--remove --apply` 卸载）。

两点行为要知道：

1. **官方按客户端版本下发新模型**。实测新模型只在足够新的 `client_version` 下返回；`catalog-refresh` 自动探测本机 `codex --version` 作为版本号，所以升级 Codex 之后第一次刷新，新模型就会自动进目录，不需要任何手工步骤。
2. **第三方模型的 `comp_hash` 由 policy 的 `managed_comp_hash` 显式固定**（默认 `3000`），每次刷新自动覆盖，不随官方模板漂移；原生条目一律保持官方原值。刷新结果与最近一次失败原因记录在 `~/.config/codex-cli-model-bridge/catalog-refresh-status.json`。

刷新若发现新增模型，macOS 会弹一条系统通知提醒重启 Codex Desktop。第三方新模型的接入仍然走 manifest + verify 的人工验收门槛，不自动化。

## Gemini

Skill 不会自动猜认证方式。每次应先选择：

- `gemini-api`：官方 API Key；
- `gemini-api --base-url https://你的中转地址`：API Key + HTTPS 中转；
- `antigravity-oauth`：用户明确选择并确认 Provider 条款后使用浏览器授权。

OAuth 示例：

```bash
python3 scripts/bridge.py provider add --preset antigravity-oauth
python3 scripts/bridge.py provider add --preset antigravity-oauth \
  --expected-sha256 <预览返回的哈希> --apply
python3 scripts/bridge.py credential login \
  --preset antigravity-oauth \
  --ack-provider-terms --apply
```

本 Skill 不复制 Gemini CLI 或 Antigravity IDE 的 token。

## 自定义 Provider

复制 [通用 ProviderSpec 示例](presets/generic-openai.example.json)，填写真实端点、上游模型 ID 和模型能力，然后：

```bash
python3 scripts/bridge.py provider add --spec /absolute/path/provider.json
```

未知模型必须完整声明上下文、推理档、模态和模板。声明不等于可用；必须继续运行 `sync` 和 `verify`。

## 故障恢复

| 现象 | 先判断 | 不要做 |
|---|---|---|
| 模型自称 GPT，但菜单选的是 Gemini | 查代理路由、映射和响应 model | 不依据模型自报身份下结论 |
| 模型显示“停用”并自动迁移 | 检查是否继承 native `upgrade` | 不重新登录 |
| `auth_unavailable` | 找最初的 401/403/404/429/EOF | 不把所有情况都当 token 失效 |
| 能聊天但 Shell 不执行 | 查 `tool_mode` 与真实 rollout | 不接受模型口头模拟命令 |
| Subagent 422 `ModelInput` | 检查 `agent_message` 转换 | 不在 8318 重写全部协议 |
| Subagent 返回 200 但没收到任务 | 检查严格 marker；必要时显式改用 Chat 兼容预设 | 不把 200 或耗时当多 Agent 通过 |
| 压缩请求 `/responses/compact` 返回 404/501 | 检查 `remote_compaction_v2` 与 8318 协议版本 | 不把 `false` 当本地压缩 |
| 跨路由换模型立即压缩并报 `expected exactly one compaction output item` | 检查模型目录第三方条目的 `comp_hash` 是否不再一致 | 不删除目录条目，也不伪造更大的 context window |
| 全部模型请求统一 415 | 查 8318 health 的 `last_error_type`（常见为新版请求压缩编码） | 不重装 Codex，也不重登账号 |
| 新官方模型报 `unknown provider for model` | 后端按客户端版本门控新模型，而 CLIProxyAPI 伪装层带旧版本标识；升级 Codex 后确认 `config.yaml` 的 `disable-codex-cloaking: true`（热加载，免重启） | 不回退 Codex，也不删除目录条目 |
| 跨路由换模型后立即压缩 | 这是路由边界 checkpoint；验证 v2 与回放 marker | 不伪造更大的 context window，也不关闭压缩 |
| `compaction_trigger` 只返回普通 message | 启用同模型 `ocx1:` 适配并要求唯一 compaction item | 不把普通 assistant 摘要当压缩成功 |
| 历史“消失” | 对比根 Provider 与任务 Provider 分布 | 不修改 SQLite 任务行 |

回滚任何 V2 事务：

```bash
python3 scripts/bridge.py rollback --transaction <id>
python3 scripts/bridge.py rollback --transaction <id> --apply
```

## 安全边界

- 8317 和 8318 只允许 loopback；远程 Provider 只接受 HTTPS。
- API Key 只写入 owner-only CLIProxyAPI 配置；不会写进模型 manifest、JSON 状态、聊天或安装包。
- 事务备份可能包含原代理配置，因此同样按私有凭据文件保护。
- OAuth 必须由账号所有者在浏览器完成；不要收集一次性 code。
- 不改 Codex Desktop `auth.json`，不直接修改任务数据库，不覆盖无关 MCP、Hooks、Skills 或信任配置。
- 8318 服务或根配置的 apply 不得与 dry-run 同轮执行；必须复用用户批准的哈希和事务 ID。若变更前 8318 已无监听，立即停止，不自行修复。

## 开发与验证

```bash
python3 -m py_compile scripts/bridge.py scripts/deployment.py scripts/compaction.py scripts/transparent_proxy.py
python3 -m unittest discover -s tests -p 'test_*.py'
```

测试全部使用临时目录或本机 mock 服务；真实 Provider 探针需要用户自己的已授权路由。

## 版本记录

- **V2**：从零安装、三端用户级常驻（macOS LaunchAgent / Linux systemd / Windows 任务计划）、API Key 与 OAuth Provider、事务回滚、Python 透明代理、通用 ProviderSpec、完整部署验收门槛。
- **V2.1**：旧／新双协议上下文压缩、跨路由切换回放（唯一 `ocx1:` compaction item）、安全服务切换（异步 `bootout` 等待与 bootstrap 有界重试）。
- **V2.1.1**：请求体 zstd 支持与未知编码安全透传（修复全模型 415）、受管模型 `comp_hash` 显式对齐（修复跨路由切换即压缩 fatal）、官方模型目录自动刷新（`catalog-refresh` + 6 小时 LaunchAgent 定时器）、请求捕获诊断、CLIProxyAPI 伪装层排障指引。

## 许可

MIT。原作者与 V2 修改说明见 [LICENSE](LICENSE) 和 [NOTICE](NOTICE)。
