#!/usr/bin/env python3
import datetime, json, pathlib, sys, uuid
from policy import project_root, safe_directory, sanitize

root = project_root()
config = json.loads((pathlib.Path(__file__).resolve().parent / 'config.json').read_text(encoding='utf8'))
if config.get('enabled') is not True:
    raise SystemExit('Project recording is disabled')
raw = sys.stdin.buffer.read(65537)
if len(raw) > 65536:
    raise SystemExit('AI event exceeds 64 KiB')
event = json.loads(raw)
if event.get('type') not in {'ai_prompt', 'ai_file_operation', 'ai_command_result'} or not isinstance(event.get('tool'), str) or not event['tool'].strip():
    raise SystemExit('Expected an explicit AI event and tool name')
event.update(id=str(uuid.uuid4()), timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(), session='', actor='ai', source='ai-adapter')
safe = sanitize(event, root)
if safe is None:
    raise SystemExit('AI event is outside the permitted project paths')
# Unique event files prevent interleaving between simultaneous adapter processes.
file = safe_directory(root, 'events') / (safe['timestamp'][:10] + '.' + safe['id'] + '.jsonl')
with file.open('x', encoding='utf8', newline='\n') as stream:
    stream.write(json.dumps(safe, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n')
file.chmod(0o600)
print('AI event recorded')
