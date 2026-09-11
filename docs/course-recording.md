# 实验过程记录

在 `main` 分支安装一次，之后在同一仓库切换到 `ch1`–`ch8` 都会继续记录。
课程工具、VS Code 插件和配置示例只由 `main` 分发，章节分支保持原有实验代码结构。

## 安装和开始实验

需要 Python 3.10+、Git 和 VS Code 1.93+。Windows 建议使用 PowerShell 7；
AI 归档另需对应的 Agent 客户端。

```sh
git switch main
python3 course.py
```

入口安装随仓库提供的 VS Code 插件，配置提交前导出，打开实验工作区与实时日志。
Windows 打开独立日志窗口；Linux/macOS 在启动入口的终端显示日志。
在 VS Code 中信任本实验工作区，保持该窗口打开，然后在另一个终端切换章节：

```sh
git switch ch1
# 如果尚未创建本地章节分支，git switch 会使用 origin/ch1 创建跟踪分支。
git course status
```

在 `main` 可以打开、编辑并保存 `README.md` 验证采集；在章节分支使用
`os/src/main.rs`。新建 VS Code 集成终端并执行 `git status`，可观察命令开始、结束
及退出码。命令采集需要终端支持 Shell Integration。

AI 会话归档在 `main` 另行配置：

```sh
./scripts/setup-agent-plugins.sh auto
# 也可指定 codex、claude、cursor、vscode 或 all。
```

安装后可以在任意章节分支运行 `git agent-plugins auto`。
从项目根目录开启普通 Codex、Claude Code 或 Cursor 会话；VS Code Copilot 使用
`.ai/ide/course.code-workspace`。详细设置见 [AI 会话归档说明](agent-session-archive.md)。

## 任意分支上的日常命令

以下 Git 入口在安装后可用，执行时自动定位仓库根目录：

| 命令 | 用途 |
| --- | --- |
| `git course` | 打开实验工作区和实时日志 |
| `git course logs` | 查看已有日志并持续显示新事件 |
| `git course status` | 查看记录开关、日志位置、文件数量和提交 Hook |
| `git course install` | 重新安装 VS Code 插件及提交 Hook |
| `git course install --skip-extension` | 只初始化本地工具与提交 Hook |
| `git course codex` | 发起一次只读 Codex 调用，记录问题和实际操作事件 |
| `git course codex --allow-edits` | 发起允许修改实验文件的 Codex 调用 |
| `git course export` | 手动导出本地事件快照 |
| `git agent-plugins auto` | 检测并配置 AI 会话归档 |
| `git agent-plugins cursor --mode full` | 调整指定客户端的归档模式 |

`main` 中的 `python3 course.py` 与 `./scripts/setup-agent-plugins.sh` 是首次安装入口。
更新工具时回到 `main` 获取新版本，再执行安装命令；已安装的记录开关、归档模式和日志
会被保留。重新克隆仓库或换一个工作目录后，需要在那个目录重新安装。

## 课程 Codex 操作记录

```sh
git course codex
```

输入实验问题，例如“请阅读 os/src/main.rs，简要说明启动流程”。每次运行提交一个问题，
记录 Prompt、会话边界以及实际产生的文件操作、命令结果。默认只读，`--allow-edits`
允许本次调用修改工作区文件。

这条操作记录入口使用临时会话模式。需要保存完整问答时，使用已经配置归档插件的
普通 Agent 会话。操作事件与会话归档分别保存在下列目录。

## 保存位置与提交

| 目录 | 内容 |
| --- | --- |
| `.ai/events/` | VS Code 和课程 AI 入口生成的本地 JSONL 事件 |
| `.ai/agent-sessions/<agent>/` | 带 UTC 日期时间与会话 ID 的 JSONL 会话归档 |
| `.ai/submissions/` | 提交前生成的增量事件快照 |
| `.ai/ide/` | 实验工作区文件及日志查看器状态 |
| `.ai/course-tools/` | 安装后的本地运行文件和课程配置 |

关闭日志窗口后文件仍然保留。请同学们不要改动或删除过程记录，提交时会检查这些记录
作为考核参考。正常 `git commit` 会导出并暂存课程事件快照；章节分支即使忽略隐藏目录，
提交 Hook 仍能纳入自己生成的快照。事件 ID 按当前暂存区去重。

`git course export` 只生成快照，不会暂存或提交。反复手动导出而未暂存上次快照时，
同一事件可能再次导出。原始事件、完整会话和本地运行文件均保持在本地。

## 配置与记录范围

安装后的课程开关位于 `.ai/course-tools/.course-monitor/config.json`。
将 `enabled` 设为 `false` 可暂停记录，恢复时设为 `true`，并在 VS Code 执行
`Developer: Reload Window`。插件也提供状态栏暂停按钮。

文件事件覆盖打开、关闭、编辑、保存、新建、重命名和删除；短时间内的连续编辑会合并。
终端与 Tasks 记录命令或任务边界、退出码和耗时。课程事件保存操作元数据，普通文件事件
不能单独判断由人还是 AI 操作；明确的 AI 归因来自课程入口或显式事件适配器。

自定义适配器可以向下面的入口传入一行 JSON，类型为 `ai_prompt`、`ai_file_operation`
或 `ai_command_result`，并给出 `tool`：

```sh
python3 .ai/course-tools/.course-monitor/ai-ingest.py
```

也提供对应的 `.sh`、`.ps1` 和 `.cjs` 入口。事件只接受项目内的路径与规定字段。

## 容器与故障排查

从 `main` 创建 Dev Container。创建时初始化运行文件，连接时通过 `git course install`
安装 VS Code 插件；之后切换章节并重新连接，也使用已安装的入口。
已有实验容器可执行：

```sh
python3 course.py install --skip-extension
```

然后在连接该容器的 VS Code 中，通过“Extensions: Install from VSIX...”安装
`.ai/course-tools/.course-monitor/rewind-ide-0.3.1.vsix`。
Rust、QEMU 和内核构建仍按章节说明操作。

- 找不到 `git course`：在 `main` 运行一次安装命令。
- 文件事件缺失：确认工作区已信任、课程插件已启用、本地课程配置的 `enabled` 为 `true`，
  重载窗口后打开并保存一个实验文件。`.ai/` 中的文件不用于采集测试。
- 终端事件缺失：使用 VS Code 内新建的终端，确认 Shell Integration 已启用。
- AI 会话缺失：核对对应 Agent 的配置，并在完成一轮问答后查看归档目录。
- 已有 Git Hook：安装器会保留原有检查并提示整合，避免覆盖已有提交流程。

迁移来源、功能清单与验证命令见 [功能说明](course-monitor-report.md)。
