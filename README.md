# delegate-to-deepseek

把有边界的编码、排查、审查任务派给 DeepSeek V4 Flash，由控制方 agent 保留
任务划分、权限、验证和整合的决定权。

要求 Python 3.9+。`delegate.py` 的两种后端分别需要 Codex CLI 或 Claude Code
CLI；WorkBuddy 走的是直连模型这条路，两个 CLI 都不需要。密钥存在 macOS
Keychain / Windows Credential Manager，不进 argv、日志、配置或 Git。

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

## 当前限制

- Windows 逻辑已有单元测试，但仍需要在真实 Windows 10/11 机器验收。
- WorkBuddy 那条路上密钥是明文的，且跨轮次的推理连续性尚未实测；细节见
  `CODEBUDDY.md` 的「Known limits」。

该项目不是 OpenAI 官方产品。Codex CLI 由 OpenAI 以 Apache-2.0 许可证开源；
使用者仍需遵守 Codex 与 DeepSeek 各自的服务条款。
