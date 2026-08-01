# DeepCodex

以 DeepSeek V4 Flash 为默认模型的一套本地 agent 工具。同一份 `scripts/`
实现向外交付**两样东西**，请按需要读对应章节：

| 交付物 | 是什么 | 入口 | 从哪读起 |
| --- | --- | --- | --- |
| **delegate-to-deepseek** 技能 | 把有边界的任务派给 DeepSeek 子进程，由 Codex CLI / Claude Code / WorkBuddy 加载 | `scripts/delegate.py`、`SKILL.md` | [仓库布局](#仓库布局一个仓库三个前端)、[两种委派后端](#两种委派后端) |
| **DeepCodex GUI**（预览版） | 以 Codex App Server 为内核的跨平台本地图形界面，双击即用，不需要 ChatGPT 订阅也不用 TUI | `DeepCodex.command` / `DeepCodex.cmd`、`scripts/web_gui.py` | [已实现](#deepcodex-gui已实现)、[启动](#deepcodex-gui启动) |

两者共用密钥存储、模型配置和 `scripts/` 下的实现，因此放在同一个仓库；但它们
是独立的交付物，只用其中一个不需要装另一个。

> GUI 部分是开发预览版。Codex App Server 仍是实验性协议；请先在测试仓库中使用，
> 并在弹窗中认真检查命令和越界文件修改。

## DeepCodex GUI：已实现

- 本地 GUI 内首次启动时直接弹出遮罩 API Key 输入框
- macOS Keychain / Windows Credential Manager 安全存储
- 独立的 `~/.deepcodex` 配置和会话目录，不读取 ChatGPT 登录状态
- DeepSeek V4 Flash + Responses API 固定为主模型
- 项目文件夹选择、流式对话、停止本轮
- 命令和越界文件修改审批弹窗
- 本轮 diff 和活动日志
- 默认禁用 Codex 插件、远程插件、网页搜索和分析遥测

## DeepCodex GUI：启动

要求：Python 3.9+、现代浏览器，以及 Codex CLI，或已安装 ChatGPT 桌面端。

macOS 双击 `DeepCodex.command`，Windows 双击 `DeepCodex.cmd`。也可以运行：

```bash
python3 scripts/web_gui.py
```

第一次启动会要求粘贴 DeepSeek API Key。页面只监听 `127.0.0.1`，每次启动
还会生成随机请求令牌。Key 不会写进 Codex 配置、命令行、日志或 Git；Codex
通过现有的命令式认证帮助程序从系统密钥库读取它。

## 开发检查

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/*.py
```

## 仓库布局：一个仓库，三个前端

同一份实现被三个 agent 外壳消费，但各自读不同的文件：

| 路径 | 谁读 | 说明 |
| --- | --- | --- |
| `SKILL.md`、`agents/openai.yaml` | Codex CLI | 仓库本身就是 `~/.codex/skills/<name>/` |
| `.claude/skills/delegate-to-deepseek/` | Claude Code | 项目级 skill，同时是复制到 `~/.claude/skills/` 的源 |
| `.codebuddy/models.json` | WorkBuddy | 全局模型目录的源码：直连 DeepSeek Chat Completions |
| `.codebuddy/agents/deepseek.md` | WorkBuddy | 全局 `deepseek` 子 agent 的源码 |
| `AGENTS.md` | 三家 | **仅**放共通内容 |
| `CLAUDE.md` | 仅 Claude Code | `@AGENTS.md` + Claude 专属 |
| `CODEBUDDY.md` | 仅 WorkBuddy | WorkBuddy 专属 |
| `scripts/`、`assets/`、`tests/` | 三家 | 共用实现 |

三份项目指令文件是**按内容归属拆开的，不是按前端复制的**：三家都需要的东西只写一遍在
`AGENTS.md`（复制三份会漂移），只有一家需要的写进那一家自己的文件，别的前端就不会在每个
会话里白读。`tests/test_workbuddy_install.py` 里有测试守着这条分界，防止悄悄退化。

Claude Code 只读自己的 skills 目录，无法像 Codex 那样就地加载本仓库，所以需要复制一次：

```bash
python3 scripts/setup.py install-claude
```

WorkBuddy 会把模型和原生 `deepseek` agent 安装到用户级 `~/.codebuddy/`，再把密钥环境合并进
`~/.workbuddy/settings.json`（保留其余设置不动）：

```bash
python3 scripts/setup.py install-workbuddy
```

安装器会把 WorkBuddy 的全局 `lite` 变体映射到直连的 `deepseek-v4-flash`，因此 Explore
等内置子 agent 自动走 DeepSeek；这是必需的，因为 WorkBuddy 的 Agent 工具不能逐次指定
子 agent 模型。也可以直接要求 WorkBuddy “调用 deepseek agent”进行显式委派。细节和已知
限制见 `CODEBUDDY.md`。

**请修改仓库 `.claude/` 下的文件，不要改已安装的副本**，改完重跑上面的命令。
`setup.py check` 会在两边不一致时提示。两份 `SKILL.md` 是刻意不同的文档而非重复：
Codex 那份默认 `--backend codex`，Claude Code 那份默认 `--backend claude`，
各自只描述本外壳适用的边界。

## 两种委派后端

`scripts/delegate.py` 可以把 DeepSeek 托管在两种 agent 外壳里，用 `--backend` 选择。
DeepSeek 同时提供 Responses 和 Anthropic 两套 API，所以两条路都是原生的：

| | `codex`（默认） | `claude` |
| --- | --- | --- |
| 协议 | Responses，`https://api.deepseek.com` | Messages，`https://api.deepseek.com/anthropic` |
| 依赖 | Codex CLI + `deepseek-flash` profile | 仅 Claude Code CLI |
| macOS TLS | 走 `curl` 回环桥 | 直连，无需桥 |
| `review` 边界 | 操作系统沙箱（`read-only`），可用 shell | 工具白名单，除非 `--shell` 否则无 shell |
| `--structured` | `--output-schema`，JSON 前带散文 | `--json-schema`，纯 JSON |
| 单轮 review 耗时 | 约 60s | 约 15-40s |

```bash
python3 scripts/delegate.py --backend claude --mode review --structured \
  --cwd <项目目录> --task "审查认证流程的正确性缺陷"
```

`claude` 后端的凭证隔离：`--bare` 不读 OAuth 和钥匙串，启动器还会剥掉子进程继承的
所有 `ANTHROPIC_*` 与 `CLAUDE_CODE_*` 变量，API key 经 `apiKeyHelper` 传入而不进
argv 或环境变量。因此子进程只能用 DeepSeek key，不会悄悄消耗 Anthropic 订阅额度 ——
可在子进程的 `modelUsage` 中确认它是 `deepseek-*`。两点注意：该后端的 `write` 模式
会免批准执行 shell 且没有操作系统级工作区隔离，请在 git worktree 或一次性目录里跑；
它报告的 `total_cost_usd` 按 Anthropic 价格计算，对 DeepSeek 无意义。

核心协议适配集中在 `scripts/app_server.py`。升级 Codex 后，应先重新生成
App Server schema 并运行测试：

```bash
codex app-server generate-json-schema --experimental --out /tmp/codex-app-schema
```

## DeepCodex GUI：当前限制

- 尚未生成签名的 `.app`、`.dmg` 或 Windows 安装包。
- 当前界面在本地浏览器中打开；后续可原样装进 Tauri/WebView 桌面壳。
- Windows 逻辑已有单元测试，但这一版仍需要在真实 Windows 10/11 机器验收。
- 尚未恢复历史任务；每次连接会新建 Codex thread。
- 目前只处理命令和文件修改审批，其他实验性反向请求会安全拒绝。
- Codex App Server 是实验性接口，未来 Codex 版本可能要求更新适配层。

该项目不是 OpenAI 官方产品。Codex CLI 由 OpenAI 以 Apache-2.0 许可证开源；
使用者仍需遵守 Codex 与 DeepSeek 各自的服务条款。
