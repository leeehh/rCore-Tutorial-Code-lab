# rCore-Tutorial-Code

课程过程记录：在仓库根目录运行 `python3 course.py`，打开实验与实时日志。安装要求、日志位置和 Codex 入口见 [实验过程记录说明](docs/course-recording.md)。

AI过程记录：在仓库根目录运行 `./scripts/setup-agent-plugins.sh auto`，也可具体选择 `codex`、`claude`、`cursor`、`vscode`。会话记录保存到 `.ai/agent-sessions/<agent>/`，文件名包含日期时间，格式为 JSONL。请同学们不要改动或删除这些记录，提交时会检查这些记录作为考核参考。详细说明见 [AI 会话归档说明](docs/agent-session-archive.md)。

**Codex 首次使用需要信任 hooks**：安装完成后，在实验仓库根目录的终端运行 `codex`，进入后输入 `/hooks`，找到 `rcore-session-archive` 的 `Stop` 和 `SessionEnd`，分别审阅并选择 **Trust（信任）**。未信任时不会自动保存会话。使用 VS Code Codex 的同学完成后还需重载窗口并新建会话；更新插件后，如提示 hooks 发生变化，请重新审阅并信任。

记录功能与验证：[实验过程记录工具功能说明](docs/course-monitor-report.md)。工具只在 `main` 分支分发；安装一次后，切换到 `ch1`–`ch8` 仍会记录。实验分支可使用 `git course logs` 查看日志，使用 `git agent-plugins auto` 再次配置 AI 归档。

## Code

- [Soure Code of labs](https://github.com/LearningOS/rCore-Tutorial-Code)

## Documents

- Concise Manual: [rCore-Tutorial-Guide](https://LearningOS.github.io/rCore-Tutorial-Guide/)

- Detail Book [rCore-Tutorial-Book-v3](https://rcore-os.github.io/rCore-Tutorial-Book-v3/)

## OS API docs of rCore Tutorial Code

- [OS API docs of ch1](https://learningos.github.io/rCore-Tutorial-Code/ch1/os/index.html)
  AND [OS API docs of ch2](https://learningos.github.io/rCore-Tutorial-Code/ch2/os/index.html)
- [OS API docs of ch3](https://learningos.github.io/rCore-Tutorial-Code/ch3/os/index.html)
  AND [OS API docs of ch4](https://learningos.github.io/rCore-Tutorial-Code/ch4/os/index.html)
- [OS API docs of ch5](https://learningos.github.io/rCore-Tutorial-Code/ch5/os/index.html)
  AND [OS API docs of ch6](https://learningos.github.io/rCore-Tutorial-Code/ch6/os/index.html)
- [OS API docs of ch7](https://learningos.github.io/rCore-Tutorial-Code/ch7/os/index.html)
  AND [OS API docs of ch8](https://learningos.github.io/rCore-Tutorial-Code/ch8/os/index.html)
- [OS API docs of ch9](https://learningos.github.io/rCore-Tutorial-Code/ch9/os/index.html)

## Related Resources

- [Learning Resource](https://github.com/LearningOS/rust-based-os-comp2025/blob/main/relatedinfo.md)

## Build & Run

```bash
# setup build&run environment first
$ git clone https://github.com/LearningOS/rCore-Tutorial-Code.git
$ cd rCore-Tutorial-Code
$ git clone https://github.com/LearningOS/rCore-Tutorial-Test.git user
$ git checkout ch$ID
$ cd os
# run OS in ch$ID
$ make run
```

Notice: $ID is from [1-9]

## Grading

```bash
# setup build&run environment first
$ git clone https://github.com/LearningOS/rCore-Tutorial-Code.git
$ cd rCore-Tutorial-Code
$ rm -rf ci-user
$ git clone https://github.com/LearningOS/rCore-Tutorial-Checker.git ci-user
$ git clone https://github.com/LearningOS/rCore-Tutorial-Test.git ci-user/user
$ git checkout ch$ID
# check&grade OS in ch$ID with more tests
$ cd ci-user && make test CHAPTER=$ID
```

Notice: $ID is from [3,4,5,6,8]
