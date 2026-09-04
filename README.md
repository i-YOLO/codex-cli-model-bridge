# Codex CLI Model Bridge V2

在保留 Codex ChatGPT 登录和既有任务历史的前提下，把经过真实验收的 Gemini、DeepSeek、GLM、Grok 或自定义兼容模型接入 Codex CLI 与 Codex Desktop 原生模型选择器。

正式仓库：[i-YOLO/codex-cli-model-bridge](https://github.com/i-YOLO/codex-cli-model-bridge)

本项目基于 [Zhijian AI 的 MIT `codex-cli-model-bridge`](https://github.com/zjp1997720/zhijian-skills/tree/main/skills/codex-cli-model-bridge) 升级。V2 新增从零安装、三端用户级常驻、API Key/OAuth Provider、事务回滚、Python 透明代理、通用 ProviderSpec 和完整部署验收。详见 [NOTICE](NOTICE)。

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
127.0.0.1:8318  仅改写本机 Authorization
          │
          ▼
127.0.0.1:8317  CLIProxyAPI
          ├── Codex OAuth → 原生 GPT
          ├── Gemini API Key / Antigravity OAuth
          ├── DeepSeek Responses
          ├── GLM 普通 API / Coding Plan
          └── 其他已验证兼容 Provider
```

模型目录只决定“菜单里显示什么”，不会决定每个模型走哪个 Provider。混合菜单依赖 CLIProxyAPI 在同一 Responses 入口完成真实路由。

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

如果报告某个模型不能返回 Codex 专用 `compaction` item，先预览再启用本地压缩：

```bash
python3 scripts/bridge.py local-compaction
python3 scripts/bridge.py local-compaction \
  --expected-sha256 <预览返回的哈希> --apply
```

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
| 长对话压缩失败 | 检查是否返回 `compaction` item | 不把普通 assistant 摘要当压缩成功 |
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

## 开发与验证

```bash
python3 -m py_compile scripts/bridge.py scripts/deployment.py scripts/transparent_proxy.py
python3 -m unittest discover -s tests -p 'test_*.py'
```

测试全部使用临时目录或本机 mock 服务；真实 Provider 探针需要用户自己的已授权路由。

## 许可

MIT。原作者与 V2 修改说明见 [LICENSE](LICENSE) 和 [NOTICE](NOTICE)。
