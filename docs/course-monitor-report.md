# 实验过程记录工具功能说明

本功能迁移自 `tg-rcore-tutorial` 的以下四次提交，并适配 rCore 的章节分支结构：

| 源提交 | 迁移能力 |
| --- | --- |
| `2d0f165` | 课程入口、VS Code 插件、实时日志、AI 事件适配、提交快照、容器配置 |
| `5d64228` | 完整教程、功能说明和记录流程 |
| `1e2722f` | 四种 Agent 的本地 JSONL 归档、独立配置、安装脚本和回归测试 |
| `1afdf2c` | Codex SessionEnd 的 3 秒时限和更新后的 hooks 提示 |

源码来自 [numpy1314/tg-rcore-tutorial](https://github.com/numpy1314/tg-rcore-tutorial)。
迁移工具保留其 [GPL-3.0 许可证](../.course-monitor/licenses/course-tools-GPL-3.0.txt)，
Rewind 组件保留 [MIT 许可证](../.course-monitor/licenses/Rewind-MIT.txt)。

## 功能清单

| 功能 | 当前实现 |
| --- | --- |
| VS Code 文件操作 | 打开、关闭、编辑、保存、新建、重命名和删除 |
| 终端与 Tasks | 命令、任务的开始和结束，退出码、耗时；终端依赖 Shell Integration |
| 实时查看 | Windows 独立日志窗口，Linux/macOS 终端持续查看 |
| 课程 AI 操作 | Codex 单次调用的 Prompt、会话边界、命令结果、文件操作 |
| 显式 AI 接入 | Python、Shell、PowerShell、Node.js 入口以及扩展事件接口 |
| 完整会话归档 | Codex、Claude Code、Cursor、VS Code Copilot |
| 归档模式 | messages、tool-calls、full，按 Agent 独立配置 |
| JSONL 文件 | `.ai/agent-sessions/<agent>/<UTC日期时间>_<session-id>.jsonl`，同一会话持续更新同一文件 |
| 提交快照 | 预提交 Hook 导出并暂存增量事件，按当前暂存区的事件 ID 去重 |
| 开关与恢复 | 保留已有日志和模式；课程配置重载后生效，归档配置每次 hook 读取 |
| 容器 | main 提供 Dev Container 初始化与连接后的插件安装入口 |
| 跨分支使用 | main 安装一次，切换 ch1–ch8 后继续记录 |

## 在 main 安装，所有实验分支使用

源码、配置示例和插件安装包仅放在 `main`。安装器将运行文件放到本地
`.ai/course-tools/`，建立 `git course` 和 `git agent-plugins` 入口，并设置稳定的提交 Hook。
本地排除规则写入 Git 的 `info/exclude`，因此切换到不同忽略规则的章节分支后仍然生效。

VS Code 扩展读取已安装的课程配置。Codex、Claude Code 的插件源也指向本地运行副本。
Agent 项目配置、Cursor hooks 和 Copilot hooks 都作为本地文件保留。
Copilot 使用 `.github/hooks/rcore-session-archive.json` 及课程工作区中的设置，章节原有的
`.vscode/settings.json` 保持原样。默认 hook 发现位置见 [VS Code 官方说明](https://code.visualstudio.com/docs/agent-customization/hooks#hook-file-locations)。

课程事件保存在 `.ai/events/`，提交检查点保存在 `.ai/submissions/`，会话正文保存在
`.ai/agent-sessions/`。安装过程从目标仓库开始生成记录。

## 验证

```sh
python3 -m unittest discover -s plugins/rcore-session-archive/tests
python3 -m unittest discover -s tests
bash -n scripts/setup-agent-plugins.sh
```

归档回归测试覆盖四种 Agent、模式过滤、重复事件、完整 JSONL、失败恢复和配置保留。
跨分支测试在隔离仓库安装工具后切换 main/ch1–ch8，验证运行文件、Git 入口、课程事件、
四种 Agent 的归档和真实提交 Hook；同时检查章节设置未被改写。
VS Code 事件通过安装包中的课程适配器与编辑器接口替身验证。
另在隔离目录使用真实 Codex CLI 验证了本地插件源安装、重新安装，以及缓存中的
Stop hook 写入 JSONL 归档。

Windows 图形窗口、VS Code 的真实界面交互和 Dev Container 构建仍需在对应环境中验收。
实验内核的构建与测试由章节自己的流程负责。

使用方法见 [实验过程记录](course-recording.md) 和 [AI 会话归档](agent-session-archive.md)。
