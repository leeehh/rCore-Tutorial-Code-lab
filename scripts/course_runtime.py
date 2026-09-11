"""Install branch-independent course tools in the local checkout."""

import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

RUNTIME_PATH = Path('.ai/course-tools')
HOOKS_PATH = '.ai/course-tools/.course-monitor/hooks'
LOCAL_PATHS = (
    '.ai/course-tools/', '.ai/events/', '.ai/ide/', '.ai/agent-sessions/',
    '.codex/config.toml', '.codex/session-archive.json',
    '.claude/settings.json', '.claude/settings.local.json', '.claude/session-archive.json',
    '.cursor/hooks.json', '.cursor/session-archive.json', '.cursor/rcore-hooks/',
    '.vscode/session-archive.json', '.vscode/rcore-hooks/',
    '.github/hooks/rcore-session-archive.json',
)
BUNDLE_FILES = ('course.py', '.course-monitor', 'plugins/rcore-session-archive',
                'scripts', '.agents/plugins/marketplace.json', '.claude-plugin/marketplace.json')


def project_root(bundle):
    bundle = Path(bundle).resolve()
    if bundle.name == 'course-tools' and bundle.parent.name == '.ai':
        return bundle.parent.parent
    return bundle


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True, encoding='utf-8').strip()


def ordinary_path(root, path):
    for candidate in (path, *path.parents):
        if candidate == root:
            break
        if candidate.is_symlink():
            raise ValueError('记录工具路径不能是符号链接：' + str(candidate))


def write_file(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(content)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def install_runtime(bundle, root=None):
    """Copy only executable tooling; preserve the installed project policy."""
    bundle = Path(bundle).resolve()
    root = project_root(bundle) if root is None else Path(root).resolve()
    if Path(git(root, 'rev-parse', '--show-toplevel')).resolve() != root:
        raise ValueError('请在实验仓库根目录安装记录工具。')
    runtime = root / RUNTIME_PATH
    ordinary_path(root, runtime)
    if bundle != runtime:
        copies = []
        for name in BUNDLE_FILES:
            source = bundle / name
            if not source.exists():
                raise ValueError('记录工具文件缺失，请从 main 分支安装：' + name)
            for path in sorted(source.rglob('*')) if source.is_dir() else [source]:
                if path.is_symlink():
                    raise ValueError('记录工具源文件不能是符号链接：' + str(path))
                if not path.is_file() or '__pycache__' in path.parts or path.suffix in {'.pyc', '.pyo'} or path.is_relative_to(bundle / '.course-monitor/node'):
                    continue
                relative = path.relative_to(bundle)
                destination = runtime / relative
                ordinary_path(root, destination)
                if relative.as_posix() == '.course-monitor/config.json' and destination.exists():
                    continue
                copies.append((path, destination))
        for source, destination in copies:
            write_file(destination, source.read_bytes(), 0o700 if source.stat().st_mode & 0o111 else 0o600)
    if not (runtime / 'plugins/rcore-session-archive/scripts/setup_agents.py').is_file():
        raise ValueError('归档插件运行文件不完整，请从 main 分支重新安装。')
    exclude = Path(git(root, 'rev-parse', '--git-path', 'info/exclude'))
    if not exclude.is_absolute():
        exclude = root / exclude
    if exclude.is_symlink():
        raise ValueError('Git 本地排除配置不能是符号链接。')
    current = exclude.read_text(encoding='utf-8') if exclude.exists() else ''
    # Source-side caches remain after main's tracked Python files are checked out.
    # Keep these patterns unanchored so they cover caches at any directory depth.
    patterns = ['/' + name for name in LOCAL_PATHS] + ['__pycache__/', '*.py[cod]']
    missing = [pattern for pattern in patterns if pattern not in current.splitlines()]
    if missing:
        text = current + ('\n' if current and not current.endswith('\n') else '')
        text += '# Installed course tools and local journals survive branch switches.\n'
        write_file(exclude, (text + '\n'.join(missing) + '\n').encode('utf-8'))
    aliases = {
        'course': [sys.executable, str(RUNTIME_PATH / 'course.py')],
        'agent-plugins': [sys.executable, str(RUNTIME_PATH / 'plugins/rcore-session-archive/scripts/setup_agents.py')],
    }
    for name, command in aliases.items():
        git(root, 'config', '--local', 'alias.' + name, '!' + shlex.join(command))
    return runtime
