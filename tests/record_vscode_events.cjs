'use strict';
// Exercise the actual course adapter shipped in the VSIX with editor callbacks.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = process.argv[2];
const extension = process.argv[3];
const callbacks = {};
const commands = {};
const listen = name => callback => {
  callbacks[name] = callback;
  return { dispose() {} };
};
const uri = file => ({ scheme: 'file', fsPath: file, toString: () => file });
const source = fs.existsSync(path.join(root, 'os/src/main.rs')) ? 'os/src/main.rs' : 'README.md';
const document = { uri: uri(path.join(root, source)), languageId: 'rust' };
const vscode = {
  workspace: {
    isTrusted: true,
    workspaceFolders: [{ uri: uri(root) }],
    textDocuments: [document],
  },
  window: {
    createOutputChannel: () => ({ appendLine() {}, show() {}, dispose() {} }),
    createStatusBarItem: () => ({ show() {}, dispose() {} }),
  },
  tasks: {},
  commands: {
    registerCommand: (name, action) => { commands[name] = action; return { dispose() {} }; },
    executeCommand: name => commands[name](),
  },
  StatusBarAlignment: { Left: 1 },
};
for (const name of ['onDidChangeWorkspaceFolders', 'onDidGrantWorkspaceTrust',
  'onDidOpenTextDocument', 'onDidCloseTextDocument', 'onDidSaveTextDocument',
  'onDidChangeTextDocument', 'onDidCreateFiles', 'onDidDeleteFiles', 'onDidRenameFiles']) {
  vscode.workspace[name] = listen(name);
}
for (const name of ['onDidStartTerminalShellExecution', 'onDidEndTerminalShellExecution', 'onDidOpenTerminal']) {
  vscode.window[name] = listen(name);
}
for (const name of ['onDidStartTask', 'onDidEndTaskProcess', 'onDidEndTask']) {
  vscode.tasks[name] = listen(name);
}
const courseExports = {};
vm.runInNewContext(fs.readFileSync(path.join(extension, 'out/course.js'), 'utf8'), {
  exports: courseExports,
  require: name => name === 'vscode' ? vscode : name === '../course/core.cjs'
    ? require(path.join(extension, 'course/core.cjs')) : require(name),
  setTimeout, clearTimeout, console,
});
const recorder = courseExports.activateCourse({ subscriptions: [] });
callbacks.onDidChangeWorkspaceFolders();
callbacks.onDidOpenTextDocument(document);
callbacks.onDidChangeTextDocument({ document, contentChanges: [{ text: 'DO_NOT_RECORD_SOURCE_CODE' }] });
callbacks.onDidSaveTextDocument(document);
callbacks.onDidCloseTextDocument(document);
const created = uri(path.join(root, 'course-example.txt'));
const renamed = uri(path.join(root, 'course-renamed.txt'));
callbacks.onDidCreateFiles({ files: [created] });
callbacks.onDidRenameFiles({ files: [{ oldUri: created, newUri: renamed }] });
callbacks.onDidDeleteFiles({ files: [renamed] });
const execution = { cwd: uri(root), commandLine: { value: 'git status', confidence: 2 } };
const terminal = { name: 'course test', shellIntegration: {} };
callbacks.onDidOpenTerminal(terminal);
callbacks.onDidStartTerminalShellExecution({ execution, terminal });
callbacks.onDidEndTerminalShellExecution({ execution, terminal, exitCode: 0 });
const task = { task: { name: 'course test task', scope: { uri: uri(root) } } };
callbacks.onDidStartTask({ execution: task });
callbacks.onDidEndTaskProcess({ execution: task, exitCode: 0 });
callbacks.onDidEndTask({ execution: task });
recorder.api.recordAIEvent(root, { type: 'ai_prompt', prompt: 'IDE_ADAPTER_PROMPT', tool: 'course-test' });
recorder.dispose();
const files = fs.readdirSync(path.join(root, '.ai/events')).filter(name => name.endsWith('.jsonl'));
const records = files.flatMap(name => fs.readFileSync(path.join(root, '.ai/events', name), 'utf8')
  .trim().split('\n').filter(Boolean).map(line => JSON.parse(line)));
const types = new Set(records.map(record => record.type));
for (const type of ['file_open', 'file_close', 'file_edit', 'file_save', 'file_create',
  'file_rename', 'file_delete', 'command_start', 'command_end', 'task_start', 'task_end', 'ai_prompt']) {
  assert(types.has(type), `Missing VS Code event: ${type}`);
}
assert(!JSON.stringify(records).includes('DO_NOT_RECORD_SOURCE_CODE'));
console.log('VSIX course callbacks wrote all supported operation events.');
