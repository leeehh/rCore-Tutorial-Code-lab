#!/usr/bin/env python3
"""Install the repository's local session archive plugin and native editor hooks."""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

BUNDLE_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BUNDLE_ROOT / 'scripts'))
from course_runtime import RUNTIME_PATH, install_runtime, project_root

from archive_session import CONFIG_RELATIVE_PATHS, SUPPORTED_MODES
from archive_storage import atomic_write
from copilot_hook import install_hooks as install_copilot_hooks
from cursor_hook import install_hooks as install_cursor_hooks


PLUGIN_NAME = "rcore-session-archive"
MARKETPLACE_NAME = "rcore-tutorial-code"
PLUGIN_ID = f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"
AGENTS = {"codex": "codex", "claude": "claude-code", "cursor": "cursor", "vscode": "vscode-copilot"}
LABELS = {"codex": "Codex", "claude": "Claude Code", "cursor": "Cursor", "vscode": "VS Code Copilot"}


def colored(text, color, stream=None):
    stream = sys.stdout if stream is None else stream
    if stream.isatty() and os.environ.get("TERM") != "dumb" and not os.environ.get("NO_COLOR"):
        return f"\033[{color}m{text}\033[0m"
    return text


class Progress:
    def __init__(self):
        self.number = 0
        self.section_title = "启动检查"
        self.current_step = "读取参数"
        self.started = time.monotonic()
        self.completed = []

    def header(self, root, target):
        print(colored("\nrCore · AI 过程记录配置", "1;36"), flush=True)
        self.info(f"项目：{root}")
        self.info(f"目标：{target}")

    def section(self, title):
        self.number += 1
        self.section_title = title
        self.current_step = "开始配置"
        print(colored(f"\n[{self.number}] {title}", "1;36"), flush=True)

    def status(self, label, message, color="36", stream=None):
        stream = sys.stdout if stream is None else stream
        print(f"  {colored('[' + label + ']', color, stream)} {message}", file=stream, flush=True)

    def info(self, message):
        self.status("信息", message)

    def step(self, description, action, *args):
        self.current_step = description
        self.status("进行", description, "34")
        started = time.monotonic()
        result = action(*args)
        elapsed = time.monotonic() - started
        self.status("完成", f"{description}（{elapsed:.1f} 秒）", "32")
        return result

    def summary(self, root, agents, policies):
        self.section("配置结果")
        elapsed = time.monotonic() - self.started
        self.status("完成", f"已配置 {len(self.completed)}/{len(agents)} 个 Agent，共用时 {elapsed:.1f} 秒", "1;32")
        for agent in agents:
            path, config = policies[agent]
            label, color = ("启用", "32") if config["enabled"] else ("暂停", "33")
            self.status(label, f"{LABELS[agent]} · {config['mode']} · {path.relative_to(root)}", color)
        self.info("归档位置：.ai/agent-sessions/<agent>/<日期时间>_<session-id>.jsonl")
        for agent in agents:
            hint = {
                "codex": "Codex：从本项目打开新会话，在 /hooks 确认 Stop、SessionEnd 均已启用并信任；更新后可能需要重新审阅。",
                "claude": "Claude Code：从本项目重新启动，或在已有会话执行 /reload-plugins。",
                "cursor": "Cursor：打开本项目并重新加载窗口，在 Output → Hooks 查看执行情况。",
                "vscode": "VS Code Copilot：打开 .ai/ide/course.code-workspace，在 Output → Copilot Chat Hooks 查看执行情况。",
            }[agent]
            self.info(hint)
        self.info("切换实验分支后仍会归档；使用 git agent-plugins 可再次配置。")

    def failure(self, error):
        print(colored("\n配置未完成", "1;31", sys.stderr), file=sys.stderr, flush=True)
        self.status("失败", f"{self.section_title} → {self.current_step}", "31", sys.stderr)
        self.status("原因", str(error), "31", sys.stderr)
        if self.completed:
            self.status("已完成", "、".join(LABELS[agent] for agent in self.completed), "32", sys.stderr)
        self.status("提示", "请处理上述问题后重新运行配置脚本。", "33", sys.stderr)


def ordinary_path(root, path):
    for candidate in (path, *path.parents):
        if candidate == root:
            break
        if candidate.is_symlink():
            raise ValueError(f"项目配置不能是符号链接：{candidate}")


def policy(root, agent, mode=None):
    path = root / CONFIG_RELATIVE_PATHS[AGENTS[agent]]
    ordinary_path(root, path)
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(config, dict):
        raise ValueError(f"配置必须是 JSON 对象：{path}")
    config.setdefault("enabled", True)
    config.setdefault("mode", "messages")
    if not isinstance(config["enabled"], bool):
        raise ValueError(f"enabled 必须为 true 或 false：{path}")
    if not isinstance(config["mode"], str) or config["mode"] not in SUPPORTED_MODES:
        raise ValueError(f"mode 必须为 messages、tool-calls 或 full：{path}")
    if mode is not None:
        config["mode"] = mode
    return path, config


def save_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, text)


def save_json(path, config):
    save_text(path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")


def toml_boolean(text, section, key, value):
    """Edit a standard table without rewriting unrelated keys or comments.

    The client writes standard [features] and [plugins."id"] tables. Reject
    ambiguous/unsupported definitions instead of creating duplicate settings.
    """
    try:
        import tomllib
    except ImportError:
        tomllib = None
    parts = section.split(".", 1) if section.startswith("plugins.") else [section]
    parts = [part.strip('"\'') for part in parts]
    if tomllib is not None:
        data = tomllib.loads(text)
        table = data
        for part in parts:
            table = table.get(part, {}) if isinstance(table, dict) else None
        if not isinstance(table, dict):
            raise ValueError(f"TOML 表格式不受支持：{section}")
    lines = text.splitlines(keepends=True)
    headers = []
    section_tokens = re.compile(r'"(?:\\.|[^"\\])*"|\'[^\']*\'|[A-Za-z0-9_-]+')
    current_table = []
    for index, line in enumerate(lines):
        match = re.fullmatch(r"\s*\[([^\[\]]+)\]\s*(?:#.*)?(?:\n)?", line)
        if match:
            tokens = [part.strip('"\'') for part in section_tokens.findall(match[1])]
            current_table = tokens
            if tokens == parts:
                headers.append(index)
        elif line.lstrip().startswith("["):
            current_table = []
        elif "=" in line and not line.lstrip().startswith("#"):
            assignment = line.split("=", 1)[0].strip()
            keys = [part.strip('"\'') for part in section_tokens.findall(assignment)]
            assigned_path = current_table + keys
            if current_table != parts and assigned_path and (
                assigned_path[:len(parts)] == parts or parts[:len(assigned_path)] == assigned_path
            ):
                raise ValueError(f"请先将 {section} 配置改为标准 TOML 表；保留原文件，未覆盖内联或点分字段。")
    if len(headers) > 1:
        raise ValueError(f"TOML 中有重复表：{section}")
    replacement = "true" if value else "false"
    if headers:
        start = headers[0]
        end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
        pattern = re.compile(rf"^(\s*(?:{re.escape(key)}|\"{re.escape(key)}\"|'{re.escape(key)}')\s*=\s*)(true|false)(\s*(?:#[^\r\n]*)?\s*)$")
        matches = [i for i in range(start + 1, end) if pattern.fullmatch(lines[i])]
        if len(matches) > 1:
            raise ValueError(f"TOML 中有重复字段：{section}.{key}")
        if matches:
            i = matches[0]
            match = pattern.fullmatch(lines[i])
            lines[i] = match[1] + replacement + match[3]
        else:
            if any(re.match(rf"\s*[\"']?{re.escape(key)}[\"']?\s*=", line) for line in lines[start + 1:end]):
                raise ValueError(f"TOML 布尔值格式不受支持：{section}.{key}")
            if not lines[start].endswith("\n"):
                lines[start] += "\n"
            lines.insert(start + 1, f"{key} = {replacement}\n")
        output = "".join(lines)
    else:
        output = text.rstrip() + f"\n\n[{section}]\n{key} = {replacement}\n"
    if tomllib is not None:
        # Inline/dotted tables may conflict with a newly appended standard table.
        tomllib.loads(output)
    return output.lstrip("\n")


def codex_project_config(root):
    path = root / ".codex/config.toml"
    ordinary_path(root, path)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    text = toml_boolean(text, "features", "hooks", True)
    text = toml_boolean(text, f'plugins."{PLUGIN_ID}"', "enabled", True)
    return path, text


def claude_project_config(root):
    path = root / ".claude/settings.json"
    ordinary_path(root, path)
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(config, dict) or not isinstance(config.setdefault("enabledPlugins", {}), dict):
        raise ValueError("Claude Code settings.json / enabledPlugins 必须是对象")
    config["enabledPlugins"][PLUGIN_ID] = True
    return path, config


def run(command, root, capture=False):
    print(colored("      $ " + shlex.join(command), "2"), flush=True)
    result = subprocess.run(command, cwd=root, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return json.loads(result.stdout) if capture else None


def disable_codex_globally():
    codex_root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    user_config = codex_root / "config.toml"
    user_text = user_config.read_text(encoding="utf-8") if user_config.exists() else ""
    user_text = toml_boolean(user_text, f'plugins."{PLUGIN_ID}"', "enabled", False)
    save_text(user_config, user_text)


def install_codex(root, progress=None):
    progress = progress or Progress()
    path, text = codex_project_config(root)
    installed = progress.step("检查已安装的 Codex 插件", run, ["codex", "plugin", "list", "--json"], root, True)
    if any(item.get("pluginId") == PLUGIN_ID for item in installed.get("installed", [])):
        progress.step("刷新 Codex 归档插件缓存", run, ["codex", "plugin", "remove", PLUGIN_ID], root)
    marketplaces = progress.step("检查 Codex 插件源", run, ["codex", "plugin", "marketplace", "list", "--json"], root, True)
    if any(item.get("name") == MARKETPLACE_NAME for item in marketplaces.get("marketplaces", [])):
        progress.step("刷新 Codex 归档插件源", run, ["codex", "plugin", "marketplace", "remove", MARKETPLACE_NAME], root)
    progress.step("注册本项目的 Codex 插件源", run, ["codex", "plugin", "marketplace", "add", str(root / RUNTIME_PATH)], root)
    progress.step("安装 Codex 本地归档插件", run, ["codex", "plugin", "add", PLUGIN_ID], root)
    progress.step("设置 Codex 插件的用户级开关", disable_codex_globally)
    progress.step("在本项目启用 Codex hooks 和插件", save_text, path, text)


def install_claude(root, progress=None):
    progress = progress or Progress()
    path, config = claude_project_config(root)
    installed = progress.step("检查已安装的 Claude Code 插件", run, ["claude", "plugin", "list", "--json"], root, True)
    if any(item.get("id") == PLUGIN_ID for item in installed):
        progress.step("刷新 Claude Code 归档插件缓存", run, ["claude", "plugin", "uninstall", "--scope", "user", PLUGIN_ID], root)
    marketplaces = progress.step("检查 Claude Code 插件源", run, ["claude", "plugin", "marketplace", "list", "--json"], root, True)
    if any(item.get("name") == MARKETPLACE_NAME for item in marketplaces):
        progress.step("刷新 Claude Code 归档插件源", run, ["claude", "plugin", "marketplace", "remove", MARKETPLACE_NAME], root)
    progress.step("注册本项目的 Claude Code 插件源", run, ["claude", "plugin", "marketplace", "add", "--scope", "user", str(root / RUNTIME_PATH)], root)
    progress.step("安装 Claude Code 本地归档插件", run, ["claude", "plugin", "install", "--scope", "user", PLUGIN_ID], root)
    progress.step("设置 Claude Code 插件的用户级开关", run, ["claude", "plugin", "disable", "--scope", "user", PLUGIN_ID], root)
    progress.step("在本项目启用 Claude Code 归档插件", save_json, path, config)


def select_agents(target, progress=None):
    if target == "auto":
        binaries = {"codex": ("codex",), "claude": ("claude",),
                    "cursor": ("cursor", "cursor-agent"), "vscode": ("code", "code-insiders")}
        selected = []
        for agent, names in binaries.items():
            if any(shutil.which(name) for name in names):
                selected.append(agent)
                if progress:
                    progress.status("检测到", LABELS[agent], "32")
            elif progress:
                hint = f"；可显式选择 {agent} 配置图形界面客户端" if agent in {"cursor", "vscode"} else ""
                progress.status("跳过", f"{LABELS[agent]}：未检测到 CLI{hint}", "33")
        if not selected:
            raise ValueError("未检测到 Agent CLI；图形界面用户请显式使用 cursor 或 vscode。")
    else:
        selected = list(AGENTS) if target == "all" else [target]
    for agent in selected:
        if agent in {"codex", "claude"} and not shutil.which(agent):
            raise ValueError(f"未找到 {LABELS[agent]} CLI，请先安装该客户端。")
    return selected


def main(argv=None):
    parser = argparse.ArgumentParser(description="配置 Codex、Claude Code、Cursor、VS Code Copilot 的本地会话归档。只需 Python 3.9+ 与相应客户端。")
    parser.add_argument("agent", nargs="?", default="auto", choices=["auto", *AGENTS, "copilot", "all"])
    parser.add_argument("--mode", choices=sorted(SUPPORTED_MODES), help="指定本地归档等级；省略时保留原设置，首次默认 messages")
    args = parser.parse_args(argv)
    target = "vscode" if args.agent == "copilot" else args.agent
    root = project_root(BUNDLE_ROOT)
    progress = Progress()
    progress.header(root, target)
    try:
        progress.section("环境与配置")
        agents = progress.step("检测 Agent 客户端", select_agents, target, progress)
        progress.info("将配置：" + "、".join(LABELS[agent] for agent in agents))
        policies = {agent: progress.step(f"检查 {LABELS[agent]} 归档配置", policy, root, agent, args.mode) for agent in agents}
        # Detect malformed project settings before modifying plugin installations.
        if "codex" in agents:
            progress.step("检查 Codex 项目设置", codex_project_config, root)
        if "claude" in agents:
            progress.step("检查 Claude Code 项目设置", claude_project_config, root)
        progress.step("安装跨分支运行文件", install_runtime, BUNDLE_ROOT, root)
        installers = {"codex": install_codex, "claude": install_claude,
                      "cursor": install_cursor_hooks, "vscode": install_copilot_hooks}
        for number, agent in enumerate(agents, 1):
            progress.section(f"{LABELS[agent]}（{number}/{len(agents)}）")
            if agent in {"codex", "claude"}:
                installers[agent](root, progress)
            else:
                progress.step("安装项目 hooks 和归档运行脚本", installers[agent], root)
            path, config = policies[agent]
            progress.step("保存归档开关和模式", save_json, path, config)
            progress.completed.append(agent)
        progress.summary(root, agents, policies)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        progress.failure(error)
        return 1
    except KeyboardInterrupt:
        progress.failure("用户中断了配置。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
