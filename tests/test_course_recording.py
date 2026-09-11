"""Install on main once, then exercise recording on the real chapter branches."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import course_runtime


def node_binary():
    if shutil.which('node'):
        return shutil.which('node')
    return next((str(path) for path in (Path.home() / '.vscode-server/bin').glob('*/node') if path.is_file()), None)


class CourseRecordingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='rcore-course-test-')
        self.addCleanup(temporary.cleanup)
        self.temp = Path(temporary.name)
        self.root = self.temp / 'project with spaces'
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=str(self.temp / 'gitconfig'),
                        GIT_CONFIG_NOSYSTEM='1', PYTHONDONTWRITEBYTECODE='1',
                        CODEX_HOME=str(self.temp / 'codex'), COURSE_TEST_LOG=str(self.temp / 'cli.jsonl'))
        self.run_command('git', 'clone', '--quiet', '--shared', '--no-checkout', str(ROOT), str(self.root), cwd=self.temp)
        self.git('switch', 'main')
        names = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT).decode().split('\0')
        for name in filter(None, names):
            path = ROOT / name
            if not path.is_file():
                continue
            destination = self.root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
        self.git('config', 'user.name', 'Course Test')
        self.git('config', 'user.email', 'course-test@example.test')
        self.git('add', '.')
        self.git('commit', '--allow-empty', '-m', 'Course tools fixture')
        binary_dir = self.temp / 'bin'
        binary_dir.mkdir()
        fake = '''#!PYTHON
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['COURSE_TEST_LOG'], 'a') as log:
    log.write(json.dumps({'name': name, 'args': args})+'\\n')
if name == 'codex' and args and args[0] == 'exec':
    sys.stdin.read()
    messages = [
      {'type':'item.completed','item':{'id':'command','type':'command_execution','command':'git status','exit_code':0,'status':'completed'}},
      {'type':'item.completed','item':{'id':'change','type':'file_change','changes':[{'path':'os/src/main.rs','kind':'update'}],'status':'completed'}},
      {'type':'item.completed','item':{'id':'reply','type':'agent_message','text':'COURSE_REPLY'}},
    ]
    for message in messages: print(json.dumps(message))
elif '--json' in args:
    print(json.dumps({'installed':[], 'marketplaces':[]} if name == 'codex' else []))
'''.replace('PYTHON', sys.executable)
        for name in ('code', 'cursor', 'codex', 'claude'):
            path = binary_dir / name
            path.write_text(fake)
            path.chmod(0o755)
        self.env['PATH'] = str(binary_dir) + os.pathsep + self.env['PATH']

    def run_command(self, *args, cwd=None, data=None, check=True, env=None):
        result = subprocess.run(args, cwd=cwd or self.root, env=env or self.env, input=data,
                                text=True, capture_output=True, timeout=30)
        if check:
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return result

    def git(self, *args, **kwargs):
        return self.run_command('git', *args, **kwargs)

    def archive_agents(self, branch):
        runtime = self.root / '.ai/course-tools/plugins/rcore-session-archive'
        hooks = json.loads((runtime / 'hooks/hooks.json').read_text())['hooks']
        stamp = '2026-09-11T12:00:00Z'
        for agent in ('codex', 'claude-code'):
            source = self.temp / (branch + '-' + agent + '.jsonl')
            if agent == 'codex':
                records = [
                    {'timestamp': stamp, 'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': 'QUESTION_' + branch}},
                    {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'phase': 'final_answer', 'content': 'ANSWER_' + branch}},
                ]
            else:
                records = [
                    {'timestamp': stamp, 'type': 'user', 'message': {'role': 'user', 'content': 'QUESTION_' + branch}},
                    {'type': 'assistant', 'message': {'role': 'assistant', 'stop_reason': 'end_turn', 'content': 'ANSWER_' + branch}},
                ]
            source.write_text(''.join(json.dumps(record) + '\n' for record in records))
            env = {k:v for k,v in self.env.items() if k not in ('PLUGIN_ROOT','CLAUDE_PLUGIN_ROOT')}
            env['CLAUDE_PLUGIN_ROOT'] = str(runtime)
            if agent == 'codex':
                env['PLUGIN_ROOT'] = str(runtime)
            payload = {'hook_event_name':'Stop', 'cwd':str(self.root), 'session_id':branch,
                       'transcript_path':str(source), 'last_assistant_message':'ANSWER_' + branch}
            for event in ('Stop', 'SessionEnd'):
                payload['hook_event_name'] = event
                command = hooks[event][0]['hooks'][0]['command']
                self.run_command('bash', '-c', command, data=json.dumps(payload), env=env)
        cursor_hooks = json.loads((self.root / '.cursor/hooks.json').read_text())['hooks']
        for event, extra in (('beforeSubmitPrompt', {'prompt':'QUESTION_' + branch}),
                             ('afterAgentResponse', {'text':'ANSWER_' + branch})):
            payload = {'hook_event_name':event, 'conversation_id':branch, 'generation_id':'turn',
                       'workspace_roots':[str(self.root)], **extra}
            self.run_command('bash', '-c', cursor_hooks[event][0]['command'], data=json.dumps(payload))
        source = self.temp / (branch + '-copilot.jsonl')
        records = [
            ('session.start', {'sessionId':branch, 'version':1}),
            ('user.message', {'content':'QUESTION_' + branch}),
            ('assistant.turn_start', {'turnId':'0'}),
            ('assistant.message', {'content':'ANSWER_' + branch, 'toolRequests':[]}),
            ('assistant.turn_end', {'turnId':'0'}),
        ]
        source.write_text(''.join(json.dumps({'type':kind, 'data':data, 'id':str(i), 'timestamp':stamp})+'\n'
                                  for i,(kind,data) in enumerate(records)))
        hook = json.loads((self.root / '.github/hooks/rcore-session-archive.json').read_text())['hooks']['Stop'][0]
        self.run_command('bash', '-c', hook['command'], data=json.dumps({
            'hook_event_name':'Stop', 'session_id':branch, 'transcript_path':str(source),
            'cwd':str(self.root), 'timestamp':stamp}))
        for agent in ('codex', 'claude-code', 'cursor', 'vscode-copilot'):
            files = list((self.root / '.ai/agent-sessions' / agent).glob('*_' + branch + '.jsonl'))
            self.assertEqual(1, len(files), agent)
            content = files[0].read_text()
            self.assertIn('QUESTION_' + branch, content)
            self.assertIn('ANSWER_' + branch, content)
            self.assertEqual(0o600, files[0].stat().st_mode & 0o777)

    def test_install_once_records_on_main_and_all_real_chapter_branches(self):
        node = node_binary()
        if not node:
            self.skipTest('Node.js is required to exercise the VSIX adapter')
        for index in range(1,9):
            self.git('fetch', '--quiet', str(ROOT), f'refs/remotes/origin/ch{index}:refs/heads/ch{index}')
        # Normal installations create source-side bytecode that survives checkout.
        install_env = {key:value for key,value in self.env.items()
                       if key not in ('PYTHONDONTWRITEBYTECODE', 'PYTHONPYCACHEPREFIX')}
        self.run_command(sys.executable, 'course.py', 'install', env=install_env)
        self.run_command('bash', 'scripts/setup-agent-plugins.sh', 'all', env=install_env)
        self.assertTrue(list((self.root / 'scripts').rglob('*.pyc')))
        self.assertTrue(list((self.root / 'plugins').rglob('*.pyc')))
        self.assertEqual(course_runtime.HOOKS_PATH, self.git('config', '--get', 'core.hooksPath').stdout.strip())
        extension = self.temp / 'extension'
        with zipfile.ZipFile(self.root / '.ai/course-tools/.course-monitor/rewind-ide-0.3.1.vsix') as package:
            package.extractall(self.temp)
            self.assertEqual('0.3.1', json.loads(package.read('extension/package.json'))['version'])
        for branch in ('main', *('ch' + str(i) for i in range(1,9))):
            with self.subTest(branch=branch):
                self.git('switch', branch)
                if branch != 'main':
                    self.assertFalse((self.root / 'course.py').exists())
                    self.assertFalse((self.root / 'plugins/rcore-session-archive/scripts/setup_agents.py').exists())
                    self.assertFalse((self.root / '.course-monitor/config.json').exists())
                status = json.loads(self.git('course', 'status').stdout)
                self.assertTrue(status['recordingEnabled'])
                self.assertEqual(str(self.root), status['project'])
                self.run_command(node, str(ROOT / 'tests/record_vscode_events.cjs'), str(self.root), str(extension))
                self.git('course', 'codex', data='COURSE_PROMPT_' + branch)
                self.archive_agents(branch)
                # A clean chapter checkout must remain clean despite local setup and callbacks.
                self.assertEqual('', self.git('status', '--porcelain', '--untracked-files=all').stdout.strip())
                self.git('add', '.')
                self.assertEqual('', self.git('diff', '--cached', '--name-only').stdout.strip())
                self.git('commit', '--allow-empty', '-m', 'Checkpoint ' + branch)
                paths = self.git('diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD').stdout.splitlines()
                self.assertTrue(paths)
                self.assertTrue(all(name.startswith('.ai/submissions/') for name in paths))
                submitted = [json.loads(line) for name in paths for line in (self.root / name).read_text().splitlines()]
                self.assertIn('ai_prompt', {event['type'] for event in submitted})
                self.assertIn('file_save', {event['type'] for event in submitted})
                self.assertEqual(len(submitted), len({event['id'] for event in submitted}))
                self.assertEqual('', self.git('status', '--porcelain').stdout.strip())
        # Reconfigure from a chapter with no source tooling and preserve local choices.
        policy = self.root / '.cursor/session-archive.json'
        policy.write_text('{"enabled":false,"mode":"full"}')
        settings = (self.root / '.vscode/settings.json').read_bytes()
        self.git('agent-plugins', 'cursor')
        self.assertEqual({'enabled':False, 'mode':'full'}, json.loads(policy.read_text()))
        self.git('agent-plugins', 'vscode')
        self.assertEqual(settings, (self.root / '.vscode/settings.json').read_bytes())
        self.assertEqual('', self.git('status', '--porcelain').stdout.strip())

    def test_reinstall_preserves_disabled_policy_logs_and_local_git_exclusions(self):
        self.run_command(sys.executable, 'course.py', 'install', '--skip-extension')
        config = self.root / '.ai/course-tools/.course-monitor/config.json'
        value = json.loads(config.read_text())
        value.update(enabled=False, custom='keep')
        config.write_text(json.dumps(value))
        journal = self.root / '.ai/events/keep.jsonl'
        journal.write_text('KEEP\n')
        exclude = self.root / '.git/info/exclude'
        with exclude.open('a') as output:
            output.write('/my-local-files/\n')
        self.run_command(sys.executable, 'course.py', 'install', '--skip-extension')
        self.assertEqual(value, json.loads(config.read_text()))
        self.assertEqual('KEEP\n', journal.read_text())
        self.assertIn('/my-local-files/', exclude.read_text())
        self.assertEqual(1, exclude.read_text().splitlines().count('/.ai/course-tools/'))
        self.assertFalse(json.loads(self.git('course', 'status').stdout)['recordingEnabled'])

    def test_existing_commit_hook_is_preserved(self):
        hook = self.root / '.git/hooks/pre-commit'
        hook.write_text('#!/bin/sh\nexit 0\n')
        before = hook.read_bytes()
        result = self.run_command(sys.executable, 'course.py', 'install', '--skip-extension', check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(before, hook.read_bytes())
        self.assertFalse((self.root / '.ai/course-tools').exists())

    def test_symlink_runtime_is_rejected(self):
        outside = self.temp / 'outside'
        outside.mkdir()
        (self.root / '.ai').mkdir(exist_ok=True)
        (self.root / '.ai/course-tools').symlink_to(outside, target_is_directory=True)
        result = self.run_command(sys.executable, 'course.py', 'install', '--skip-extension', check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertEqual([], list(outside.iterdir()))


if __name__ == '__main__':
    unittest.main()
