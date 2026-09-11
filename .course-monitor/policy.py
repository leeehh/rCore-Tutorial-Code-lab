"""Course event boundary and redaction policy; never retain source or output."""
import datetime
import pathlib
import re
import uuid

def project_root():
    bundle = pathlib.Path(__file__).resolve().parent.parent
    if bundle.name == 'course-tools' and bundle.parent.name == '.ai':
        return bundle.parent.parent
    return bundle

TYPES = set('session_start session_end file_open file_close file_save file_edit file_create file_delete file_rename terminal_open terminal_close command_start command_end task_start task_end shell_integration_unavailable ai_prompt ai_file_operation ai_command_result'.split())
FIELDS = 'file old_file language command cwd exit_code duration_ms execution_id task terminal reason prompt tool operation confidence'.split()
SECRET = r'(?:password|passwd|token|secret|api[_-]?key|authorization|credential|private[_-]?key)'

def redact(value):
    value = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-]*PRIVATE KEY-----|$)', '[REDACTED PRIVATE KEY]', value)
    value = re.sub(r'\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{16,}|AKIA[A-Z0-9]{16}|xox[baprs]-[\w-]{16,})\b', '[REDACTED]', value)
    value = re.sub(r'(https?://)[^\s/@:]+:[^\s/@]+@', r'\1[REDACTED]@', value, flags=re.I)
    value = re.sub(r'\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+', '[REDACTED AUTH]', value, flags=re.I)
    pattern = r'((?:--[\w-]*' + SECRET + r'|(?:\$env:)?[\w-]*' + SECRET + r')[\"\x27]?\s*(?:[=:]\s*|\s+))(?:"(?:\\.|[^"\\])*"|\x27[^\x27]*\x27|[^\s,;}&]+)'
    value = re.sub(pattern, r'\1[REDACTED]', value, flags=re.I)
    return re.sub(r'([?&](?:key|password|token|secret|api_key)=)[^&\s]+', r'\1[REDACTED]', value, flags=re.I)[:16384]

def relative_file(root, value, allow_root=False):
    if not isinstance(value, str) or not value:
        return None
    if not pathlib.Path(value).is_absolute() and (re.match(r'^[A-Za-z]:', value) or value.startswith('\\\\')):
        return None
    try:
        relative = (root / value).resolve().relative_to(root.resolve())
    except (ValueError, OSError, RuntimeError):
        return None
    if not relative.parts:
        return '.' if allow_root else None
    if any(part in {'.git', '.ai', 'node_modules', 'target'} or re.match(r'^\.env(?:\.|$)', part, re.I) for part in relative.parts):
        return None
    if relative.suffix.lower() in {'.pem', '.key', '.p12', '.pfx'}:
        return None
    return relative.as_posix()

def safe_directory(root, name):
    directory = root / '.ai' / name
    if directory.resolve() != directory.absolute():
        raise ValueError('Log directories must not traverse symlinks')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory

def sanitize(event, root):
    if not isinstance(event, dict) or event.get('type') not in TYPES:
        raise ValueError('Unsupported course event')
    event_id = str(uuid.UUID(str(event['id'])))
    session = str(uuid.UUID(str(event['session']))) if event.get('session') else ''
    timestamp = datetime.datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00'))
    if timestamp.tzinfo is None:
        raise ValueError('Timestamp requires timezone')
    result = dict(schema='course-log-v1', id=event_id, timestamp=timestamp.astimezone(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z'), session=session, type=event['type'], actor=event.get('actor') if event.get('actor') in {'ai', 'unknown', 'system'} else 'unknown', source=redact(str(event.get('source', 'unknown')))[:128])
    for key in FIELDS:
        if key not in event or (key == 'prompt' and event['type'] != 'ai_prompt'):
            continue
        value = event[key]
        if value is not None and (isinstance(value, bool) or not isinstance(value, (str, int, float))):
            raise ValueError('Invalid field ' + key)
        if key in {'file', 'old_file', 'cwd'}:
            value = relative_file(root, value, key == 'cwd')
            if value is None:
                return None
        result[key] = redact(value) if isinstance(value, str) else value
    return result
