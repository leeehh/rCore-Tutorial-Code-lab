import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
PLUGIN = SCRIPTS.parent
REPOSITORY = PLUGIN.parents[1]
sys.path.insert(0, str(SCRIPTS))
import setup_agents as setup


class SetupPolicyTests(unittest.TestCase):
    def test_new_policy_and_existing_disabled_mode_are_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for agent in setup.AGENTS:
                path, config = setup.policy(root, agent)
                self.assertEqual({"enabled": True, "mode": "messages"}, config)
                setup.save_json(path, {"enabled": False, "mode": "full", "custom": 3})
                _, config = setup.policy(root, agent)
                self.assertEqual({"enabled": False, "mode": "full", "custom": 3}, config)
                _, config = setup.policy(root, agent, "tool-calls")
                self.assertFalse(config["enabled"])
                self.assertEqual("tool-calls", config["mode"])
                self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def test_malformed_or_symlink_policy_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path, _ = setup.policy(root, "codex")
            path.parent.mkdir()
            for text in ('{"broken":', '[]', '{"mode": []}', '{"enabled": "true"}'):
                path.write_text(text)
                with self.assertRaises(ValueError):
                    setup.policy(root, "codex")
                self.assertEqual(text, path.read_text())
            external = root / "external.json"
            path.rename(external)
            path.symlink_to(external)
            with self.assertRaises(ValueError):
                setup.policy(root, "codex")

    def test_toml_changes_preserve_other_plugins_comments_and_no_final_newline(self):
        original = '# keep\nmodel = "example"\n[features] # preserve\nhooks = false # note\nother = true\n[plugins."other@market"]\nenabled = true\n'
        updated = setup.toml_boolean(original, "features", "hooks", True)
        updated = setup.toml_boolean(updated, f'plugins."{setup.PLUGIN_ID}"', "enabled", True)
        self.assertIn('hooks = true # note', updated)
        self.assertIn('[plugins."other@market"]\nenabled = true', updated)
        self.assertEqual(updated, setup.toml_boolean(updated, "features", "hooks", True))
        self.assertEqual('[features]\nhooks = true\n', setup.toml_boolean('[features]', "features", "hooks", True))
        quoted = "[features]\n'hooks' = false\n"
        self.assertEqual("[features]\n'hooks' = true\n", setup.toml_boolean(quoted, "features", "hooks", True))

    def test_unsupported_toml_definitions_are_rejected_instead_of_duplicated(self):
        for text in ('features = { hooks = false }\n', 'features.hooks = false\n', '[features]\nhooks = "bad"\n', '[features]\nhooks = true\n[features]\nhooks = false\n'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                setup.toml_boolean(text, "features", "hooks", True)

    def test_claude_merge_preserves_other_plugins_env_and_hooks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / '.claude/settings.json'
            path.parent.mkdir()
            original = {'enabledPlugins': {'other@market': True}, 'env': {'KEEP': 'value'}, 'hooks': {'Stop': []}}
            path.write_text(json.dumps(original))
            _, merged = setup.claude_project_config(root)
            self.assertEqual(original['env'], merged['env'])
            self.assertEqual(original['hooks'], merged['hooks'])
            self.assertTrue(merged['enabledPlugins']['other@market'])
            self.assertTrue(merged['enabledPlugins'][setup.PLUGIN_ID])

    def test_auto_detects_clients_and_explicit_editors_need_no_cli(self):
        with mock.patch.object(setup.shutil, 'which', side_effect=lambda name: '/bin/' + name if name == 'cursor-agent' else None):
            self.assertEqual(['cursor'], setup.select_agents('auto'))
            self.assertEqual(['vscode'], setup.select_agents('vscode'))
            with self.assertRaisesRegex(ValueError, 'CLI'):
                setup.select_agents('all')

    def test_codex_installs_only_local_archive_and_disables_it_globally(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'project'
            root.mkdir()
            user_root = Path(temp) / 'user'
            codex_root = user_root / '.codex'
            codex_root.mkdir(parents=True)
            global_config = codex_root / 'config.toml'
            global_config.write_text('# keep\nmodel = "example"\n[plugins."other@market"]\nenabled = true\n')

            def command(args, _root, capture=False):
                if args[1:3] == ['plugin', 'list']:
                    return {'installed': [{'pluginId': setup.PLUGIN_ID}]}
                if args[1:4] == ['plugin', 'marketplace', 'list']:
                    return {'marketplaces': [{'name': setup.MARKETPLACE_NAME}]}

            with mock.patch.object(setup, 'run', side_effect=command) as run, mock.patch.object(setup.os, 'environ', {}), mock.patch.object(setup.Path, 'home', return_value=user_root):
                setup.install_codex(root)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertIn(['codex', 'plugin', 'marketplace', 'add', str(root / setup.RUNTIME_PATH)], commands)
            self.assertIn(['codex', 'plugin', 'add', setup.PLUGIN_ID], commands)
            self.assertEqual(1, sum(command[1:3] == ['plugin', 'add'] for command in commands))
            self.assertIn(f'[plugins."{setup.PLUGIN_ID}"]\nenabled = false', global_config.read_text())
            self.assertIn('[plugins."other@market"]\nenabled = true', global_config.read_text())
            project = (root / '.codex/config.toml').read_text()
            self.assertIn('hooks = true', project)
            self.assertIn(f'[plugins."{setup.PLUGIN_ID}"]\nenabled = true', project)

    def test_claude_installs_only_local_archive_and_preserves_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def command(args, _root, capture=False):
                return [] if capture else None

            with mock.patch.object(setup, 'run', side_effect=command) as run:
                setup.install_claude(root)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertIn(['claude', 'plugin', 'marketplace', 'add', '--scope', 'user', str(root / setup.RUNTIME_PATH)], commands)
            self.assertIn(['claude', 'plugin', 'install', '--scope', 'user', setup.PLUGIN_ID], commands)
            self.assertIn(['claude', 'plugin', 'disable', '--scope', 'user', setup.PLUGIN_ID], commands)
            self.assertEqual({setup.PLUGIN_ID: True}, json.loads((root / '.claude/settings.json').read_text())['enabledPlugins'])


class SetupCommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'project with spaces'
        shutil.copytree(PLUGIN, self.root / 'plugins' / PLUGIN.name)
        (self.root / 'scripts').mkdir()
        shutil.copy2(REPOSITORY / 'scripts/setup-agent-plugins.sh', self.root / 'scripts/setup-agent-plugins.sh')
        shutil.copy2(REPOSITORY / 'scripts/course_runtime.py', self.root / 'scripts/course_runtime.py')
        shutil.copy2(REPOSITORY / 'course.py', self.root / 'course.py')
        for directory in ('.course-monitor', '.agents', '.claude-plugin'):
            shutil.copytree(REPOSITORY / directory, self.root / directory)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)

    def run_setup(self, *args):
        return subprocess.run(['bash', str(self.root / 'scripts/setup-agent-plugins.sh'), *args],
                              cwd=self.root, text=True, capture_output=True, timeout=15)

    def test_cursor_and_vscode_set_up_independently_and_preserve_policy(self):
        for agent, directory in (('cursor', '.cursor'), ('copilot', '.vscode')):
            result = self.run_setup(agent)
            self.assertEqual(0, result.returncode, result.stderr)
            config_path = self.root / directory / 'session-archive.json'
            self.assertEqual({'enabled': True, 'mode': 'messages'}, json.loads(config_path.read_text()))
            self.assertIn('.ai/agent-sessions/', result.stdout)
            self.assertTrue((self.root / directory / 'rcore-hooks').is_dir())
            config_path.write_text('{"enabled":false,"mode":"full","custom":123}')
            result = self.run_setup(agent)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual({'enabled': False, 'mode': 'full', 'custom': 123}, json.loads(config_path.read_text()))
        self.assertFalse((self.root / '.codex').exists())
        self.assertFalse((self.root / '.claude').exists())

    def test_invalid_configuration_stops_without_overwriting_it(self):
        path = self.root / '.cursor/session-archive.json'
        path.parent.mkdir()
        path.write_text('{"mode":"invalid"}')
        result = self.run_setup('cursor')
        self.assertNotEqual(0, result.returncode)
        self.assertEqual('{"mode":"invalid"}', path.read_text())
        self.assertFalse((path.parent / 'hooks.json').exists())

    def test_explicit_mode_is_saved_and_extra_positional_argument_is_rejected(self):
        result = self.run_setup('cursor', '--mode', 'tool-calls')
        self.assertEqual(0, result.returncode, result.stderr)
        path = self.root / '.cursor/session-archive.json'
        self.assertEqual('tool-calls', json.loads(path.read_text())['mode'])
        before = path.read_bytes()
        result = self.run_setup('cursor', 'unexpected.json')
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(before, path.read_bytes())

    def test_help_does_not_create_project_configuration(self):
        result = self.run_setup('--help')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('tool-calls', result.stdout)
        self.assertFalse((self.root / '.cursor').exists())


if __name__ == '__main__':
    unittest.main()
