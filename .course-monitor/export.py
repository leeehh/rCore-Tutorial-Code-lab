#!/usr/bin/env python3
import datetime, json, pathlib, subprocess, sys, uuid
from policy import project_root, safe_directory, sanitize

root = project_root()
target = safe_directory(root, 'submissions')
def git(*args):
    return subprocess.check_output(['git', '-C', str(root), *args], encoding='utf8')

# Git errors abort the hook. Deduplication uses the index, not working copies.
known = set()
for name in git('ls-files', '-z', '--', '.ai/submissions/').split('\0'):
    if name.endswith('.jsonl'):
        for line in git('show', ':' + name).splitlines():
            known.add(json.loads(line)['id'])
pending = []
for file in sorted(safe_directory(root, 'events').glob('*.jsonl')):
    if file.is_symlink():
        raise ValueError('Event file must not be a symlink')
    data = file.read_bytes()
    # A concurrent append may end in a partial UTF-8 character.
    complete = data[:data.rfind(b'\n') + 1].decode('utf8')
    for line in complete.splitlines():
        if not line:
            continue
        event = sanitize(json.loads(line), root)
        if event is None or event['id'] in known:
            continue
        known.add(event['id'])
        pending.append(event)
if pending:
    pending.sort(key=lambda e: (e['timestamp'], e['id']))
    name = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H-%M-%S') + '.' + str(uuid.uuid4()) + '.jsonl'
    file = target / name
    with file.open('x', encoding='utf8', newline='\n') as stream:
        for event in pending:
            stream.write(json.dumps(event, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n')
    file.chmod(0o600)
    if '--stage' in sys.argv:
        # Chapter branches may ignore all dot-directories. Only the validated
        # checkpoint created above is force-added; raw journals stay local.
        git('add', '-f', '--', str(file.relative_to(root)))
    print(f'Exported {len(pending)} sanitized events to {file.relative_to(root)}')
else:
    print('No new course events to submit.')
