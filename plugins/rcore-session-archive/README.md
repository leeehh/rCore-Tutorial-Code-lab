# rCore Session Archive

本插件提供 Codex、Claude Code、Cursor 和 VS Code GitHub Copilot 的本地 JSONL
会话归档。配置和运行使用 Python 标准库。

首次在 main 分支的项目根目录运行：

```bash
./scripts/setup-agent-plugins.sh codex  # 或 claude / cursor / vscode / auto / all
```

安装后切换实验分支仍会归档，任意分支可使用 `git agent-plugins` 再次配置。
已安装的程序和插件源位于本地 `.ai/course-tools/`。

归档位置为 `.ai/agent-sessions/<agent>/<UTC日期时间>_<session-id>.jsonl`。
每个 Agent 使用自己目录中的 `session-archive.json`，支持 `enabled` 和
`messages` / `tool-calls` / `full` 三档 mode。

完整说明和 JSONL 格式见 [Agent 会话本地归档](../../docs/agent-session-archive.md)。

实现文件：

- `scripts/archive_session.py`：项目配置、归档路径、Codex/Claude transcript 过滤和 JSONL 写入。
- `scripts/archive_storage.py`：Cursor/Copilot 的 JSONL 存储和内容无关的 SQLite 排序索引。
- `scripts/cursor_hook.py`：Cursor 项目事件适配器及 hooks 安装。
- `scripts/copilot_hook.py`：Copilot v1 transcript 适配器及保留 JSONC 注释的 hooks 安装。
- `scripts/setup_agents.py`：四种 Agent 的安装与独立项目配置。

Codex / Claude Code 的 `hooks/hooks.json` 由插件加载。Cursor / Copilot 使用独立的
项目 hooks，并在安装时复制所需运行脚本，后续无需依赖当前分支的插件源码。
