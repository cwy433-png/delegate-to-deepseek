# DeepCodex Preview

一个以 DeepSeek V4 Flash 为默认模型、以 Codex App Server 为 agent 内核的
跨平台本地 GUI 原型。双击启动后会自动打开浏览器界面；用户不需要 ChatGPT
订阅，也不需要使用 TUI。

> 当前是开发预览版。Codex App Server 仍是实验性协议；请先在测试仓库中使用，
> 并在弹窗中认真检查命令和越界文件修改。

## 已实现

- 本地 GUI 内首次启动时直接弹出遮罩 API Key 输入框
- macOS Keychain / Windows Credential Manager 安全存储
- 独立的 `~/.deepcodex` 配置和会话目录，不读取 ChatGPT 登录状态
- DeepSeek V4 Flash + Responses API 固定为主模型
- 项目文件夹选择、流式对话、停止本轮
- 命令和越界文件修改审批弹窗
- 本轮 diff 和活动日志
- 默认禁用 Codex 插件、远程插件、网页搜索和分析遥测

## 启动

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

核心协议适配集中在 `scripts/app_server.py`。升级 Codex 后，应先重新生成
App Server schema 并运行测试：

```bash
codex app-server generate-json-schema --experimental --out /tmp/codex-app-schema
```

## 当前限制

- 尚未生成签名的 `.app`、`.dmg` 或 Windows 安装包。
- 当前界面在本地浏览器中打开；后续可原样装进 Tauri/WebView 桌面壳。
- Windows 逻辑已有单元测试，但这一版仍需要在真实 Windows 10/11 机器验收。
- 尚未恢复历史任务；每次连接会新建 Codex thread。
- 目前只处理命令和文件修改审批，其他实验性反向请求会安全拒绝。
- Codex App Server 是实验性接口，未来 Codex 版本可能要求更新适配层。

该项目不是 OpenAI 官方产品。Codex CLI 由 OpenAI 以 Apache-2.0 许可证开源；
使用者仍需遵守 Codex 与 DeepSeek 各自的服务条款。
