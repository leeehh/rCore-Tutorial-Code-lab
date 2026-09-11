#!/usr/bin/env python3
"""Single repository entry point for course recording; Python 3.10+."""
import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

BUNDLE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BUNDLE / 'scripts'))
from course_runtime import HOOKS_PATH, install_runtime, project_root

ROOT = project_root(BUNDLE)
MONITOR = BUNDLE / '.course-monitor'
if (ROOT / '.ai/course-tools/.course-monitor/config.json').is_file():
    MONITOR = ROOT / '.ai/course-tools/.course-monitor'
VSIX = MONITOR / 'rewind-ide-0.3.1.vsix'

def git(*args, optional=False):
    result = subprocess.run(['git', '-C', str(ROOT), *args], text=True, encoding='utf8', capture_output=True)
    if result.returncode and not (optional and result.returncode == 1):
        raise RuntimeError(result.stderr.strip() or 'Git command failed')
    return result.stdout.strip()

def check_repository():
    if pathlib.Path(git('rev-parse', '--show-toplevel')).resolve() != ROOT:
        raise RuntimeError('course.py must be placed at the Git repository root.')
    for name in ['.ai', '.ai/course-tools', '.ai/events', '.ai/ide', '.ai/submissions']:
        path = ROOT / name
        if path.resolve() != path.absolute():
            raise RuntimeError('Refusing symlink in recording path: ' + name)

def code_command():
    candidate = shutil.which('code.cmd' if os.name == 'nt' else 'code')
    if not candidate and os.name == 'nt':
        for directory in [pathlib.Path(os.environ.get('LOCALAPPDATA', ''))/'Programs/Microsoft VS Code', pathlib.Path(os.environ.get('ProgramFiles', ''))/'Microsoft VS Code']:
            if (directory/'bin/code.cmd').is_file():
                candidate = str(directory/'bin/code.cmd'); break
    if not candidate:
        raise RuntimeError('VS Code CLI not found. Install VS Code and add its command to PATH.')
    env = dict(os.environ)
    if os.name == 'nt':
        # Invoke the official CLI directly; avoid interpreting repository paths
        # as batch syntax when they contain spaces or shell metacharacters.
        script = pathlib.Path(candidate)
        match = re.search(r'"%~dp0([^"\r\n]*cli\.js)"', script.read_text(encoding='utf-8-sig'), re.I)
        if not match:
            raise RuntimeError('Unrecognized VS Code CLI wrapper: ' + str(script))
        cli = (script.parent / match.group(1)).resolve(strict=True)
        executable = (script.parent.parent/'Code.exe').resolve(strict=True)
        cli.relative_to(script.parent.parent.resolve())
        env['ELECTRON_RUN_AS_NODE'] = '1'
        return [str(executable), str(cli)], env
    return [candidate], env

def install(skip_extension=False):
    global MONITOR, VSIX
    config = json.loads((MONITOR/'config.json').read_text(encoding='utf-8-sig'))
    if config.get('schema') != 'course-monitor-v1':
        raise RuntimeError('Unrecognized .course-monitor/config.json')
    hook = git('config', '--get', 'core.hooksPath', optional=True)
    if hook and hook not in {'.course-monitor/hooks', HOOKS_PATH}:
        raise RuntimeError('Existing hooksPath must be integrated first: ' + hook)
    if not hook:
        old = pathlib.Path(git('rev-parse', '--git-path', 'hooks/pre-commit'))
        if not old.is_absolute(): old = ROOT/old
        if old.exists(): raise RuntimeError('Existing pre-commit hook preserved. Integrate it before installing.')
    for name in ['hooks/pre-commit','export.py','policy.py','ai-ingest.py','codex-course.py','watch.ps1']:
        if not (MONITOR/name).is_file(): raise RuntimeError('Missing repository dependency: .course-monitor/' + name)
    runtime = install_runtime(BUNDLE, ROOT)
    MONITOR = runtime / '.course-monitor'
    VSIX = MONITOR / 'rewind-ide-0.3.1.vsix'
    config = json.loads((MONITOR/'config.json').read_text(encoding='utf-8-sig'))
    if not skip_extension:
        if not VSIX.is_file(): raise RuntimeError('Missing course VSIX: ' + str(VSIX))
        command, env = code_command()
        subprocess.run([*command,'--install-extension',str(VSIX),'--force'],env=env,check=True)
    for name in ['events','ide','submissions']:
        (ROOT/'.ai'/name).mkdir(parents=True,exist_ok=True,mode=0o700)
    (MONITOR/'hooks/pre-commit').chmod(0o755)
    git('config','--local','core.hooksPath',HOOKS_PATH)
    print('课程记录已配置。日志：'+str(ROOT/'.ai/events'),flush=True)
    print('切换实验分支后继续记录；使用 git course logs 查看，git course status 检查状态。',flush=True)
    if config.get('enabled') is not True:
        print('现有配置 enabled=false，仍保持关闭。启用后请重载 VS Code。',flush=True)

def open_workspace():
    command, env=code_command()
    settings={'terminal.integrated.shellIntegration.enabled':True}
    if os.name=='nt':
        pwsh=shutil.which('pwsh')
        fallback=pathlib.Path(os.environ.get('ProgramFiles',''))/'PowerShell/7/pwsh.exe'
        if not pwsh and fallback.is_file(): pwsh=str(fallback)
        if pwsh:
            settings.update({'terminal.integrated.profiles.windows':{'Course PowerShell':{'path':pwsh,'args':['-NoLogo']}},'terminal.integrated.defaultProfile.windows':'Course PowerShell'})
        else: print('未找到 PowerShell 7；终端采集需另选支持 Shell Integration 的终端。',flush=True)
    workspace=ROOT/'.ai/ide/course.code-workspace'
    config=json.loads(workspace.read_text(encoding='utf8')) if workspace.exists() else {}
    config['folders']=[{'name':ROOT.name,'path':'../..'}]
    config.setdefault('settings',{}).update(settings)
    workspace.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf8')
    subprocess.run([*command,'--new-window',str(workspace)],env=env,check=True)

def logs():
    directory=ROOT/'.ai/events'
    if os.name=='nt':
        powershell=shutil.which('powershell.exe')
        if not powershell: raise RuntimeError('Windows PowerShell is required for the log viewer.')
        subprocess.Popen([powershell,'-NoLogo','-NoProfile','-STA','-File',str(MONITOR/'watch.ps1'),'-ProjectPath',str(ROOT)],creationflags=subprocess.CREATE_NEW_CONSOLE)
        print('已打开当前实验仓库的实时日志窗口。',flush=True)
        return
    print('实时读取 '+str(directory)+'；按 Ctrl+C 结束查看。',flush=True)
    seen=set()
    while True:
        for file in sorted(directory.glob('*.jsonl')):
            data=file.read_bytes();complete=data[:data.rfind(b'\n')+1].decode('utf8')
            for line in complete.splitlines():
                if not line: continue
                event=json.loads(line)
                if event['id'] not in seen:
                    seen.add(event['id']);print(json.dumps(event,ensure_ascii=False),flush=True)
        time.sleep(0.5)

def main():
    parser=argparse.ArgumentParser(description='仓库内的课程记录入口；不带参数时安装并打开实验与实时日志。')
    parser.add_argument('action',nargs='?',default='start',choices=['start','install','logs','status','codex','export'])
    parser.add_argument('--skip-extension',action='store_true',help='仅安装项目 hooks；供容器初始化或已单独安装扩展时使用')
    parser.add_argument('--allow-edits',action='store_true',help='Codex 使用 workspace-write 沙箱；默认只读')
    args=parser.parse_args()
    if args.action in ['start','install'] and pathlib.Path('/.dockerenv').exists():
        # A Windows bind mount can have a different owner in Docker. Scope this
        # exception to this explicitly selected repository inside the container.
        safe=subprocess.run(['git','config','--global','--get-all','safe.directory'],text=True,capture_output=True)
        if str(ROOT) not in safe.stdout.splitlines():
            subprocess.run(['git','config','--global','--add','safe.directory',str(ROOT)],check=True)
    check_repository()
    if args.action in ['start','install']:
        install(args.skip_extension)
        if args.action=='start': open_workspace();logs()
    elif args.action=='logs': logs()
    elif args.action=='status':
        config=json.loads((MONITOR/'config.json').read_text(encoding='utf-8-sig'))
        print(json.dumps(dict(project=str(ROOT),recordingEnabled=config.get('enabled') is True,hooksPath=git('config','--get','core.hooksPath',optional=True),logs=str(ROOT/'.ai/events'),logFiles=len(list((ROOT/'.ai/events').glob('*.jsonl'))),snapshots=len(list((ROOT/'.ai/submissions').glob('*.jsonl')))),ensure_ascii=False,indent=2))
    elif args.action=='export':
        subprocess.run([sys.executable,str(MONITOR/'export.py')],check=True)
    elif args.action=='codex':
        command=[sys.executable,str(MONITOR/'codex-course.py')]
        if args.allow_edits: command.append('--allow-edits')
        if sys.stdin.isatty():
            prompt=input('请输入实验问题：')
            if not prompt.strip(): return
            result=subprocess.run(command,input=prompt,encoding='utf8',env=dict(os.environ,PYTHONIOENCODING='utf-8'))
        else: result=subprocess.run(command,env=dict(os.environ,PYTHONIOENCODING='utf-8'))
        if result.returncode: raise RuntimeError('Codex 调用未成功，退出码 '+str(result.returncode))

if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');sys.stderr.reconfigure(encoding='utf-8')
    try: main()
    except KeyboardInterrupt: pass
    except (OSError,ValueError,RuntimeError,subprocess.CalledProcessError) as error:
        print(str(error),file=sys.stderr);sys.exit(1)
