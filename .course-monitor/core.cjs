'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const secretKey = /(?:password|passwd|token|secret|api[_-]?key|authorization|credential|private[_-]?key)/i;
function redact(text) {
  return String(text)
    .replace(/-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-]*PRIVATE KEY-----|$)/g, '[REDACTED PRIVATE KEY]')
    .replace(/\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{16,}|AKIA[A-Z0-9]{16}|xox[baprs]-[\w-]{16,})\b/g, '[REDACTED]')
    .replace(/(https?:\/\/)[^\s/@:]+:[^\s/@]+@/gi, '$1[REDACTED]@')
    .replace(/\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+/gi, '[REDACTED AUTH]')
    .replace(/((?:--[\w-]*(?:password|passwd|token|secret|api-key|apikey|credential)|(?:\$env:)?[\w-]*(?:PASSWORD|PASSWD|TOKEN|SECRET|API_KEY|APIKEY|CREDENTIAL))["']?\s*(?:[=:]\s*|\s+))(?:"(?:\\.|[^"\\])*"|'[^']*'|[^\s,;}&]+(?:\s*))/gi, '$1[REDACTED] ')
    .replace(/([?&](?:key|password|token|secret|api_key)=)[^&\s]+/gi, '$1[REDACTED]');
}
function scrub(value, key = '') {
  if (secretKey.test(key)) return '[REDACTED]';
  if (typeof value === 'string') return redact(value).slice(0, 16384);
  if (Array.isArray(value)) return value.slice(0, 100).map(v => scrub(v));
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).slice(0, 100).map(([k,v]) => [k,scrub(v,k)]));
  return value;
}
function relativeFile(root, file, allowRoot = false) {
  if (!file) return null;
  const absolute = path.resolve(root, file);
  let physical = absolute;
  try { physical = fs.realpathSync(absolute); } catch {
    let ancestor=path.dirname(absolute);const suffix=[path.basename(absolute)];
    while(!fs.existsSync(ancestor)){const parent=path.dirname(ancestor);if(parent===ancestor)return null;suffix.unshift(path.basename(ancestor));ancestor=parent;}
    try {physical=path.join(fs.realpathSync(ancestor),...suffix);}catch{return null;}
  }
  const rel = path.relative(fs.realpathSync(root), physical).split(path.sep).join('/');
  if ((!allowRoot && !rel) || rel === '..' || rel.startsWith('../') || path.isAbsolute(rel)) return null;
  if (rel.split('/').some(p => ['.git','.ai','node_modules','target'].includes(p) || /^\.env(?:\.|$)/i.test(p)) || /\.(?:pem|key|p12|pfx)$/i.test(rel)) return null;
  return rel || '.';
}
const types = new Set(['session_start','session_end','file_open','file_close','file_save','file_edit','file_create','file_delete','file_rename','terminal_open','terminal_close','command_start','command_end','task_start','task_end','shell_integration_unavailable','ai_prompt','ai_file_operation','ai_command_result']);
// Explicit allowlist: no code, diffs, terminal output, arbitrary metadata or chat reasoning.
const fields = ['file','old_file','language','command','cwd','exit_code','duration_ms','execution_id','task','terminal','reason','prompt','tool','operation','confidence'];
function sanitizeEvent(event) {
  if (!types.has(event.type)) throw Error('Unsupported course event type');
  const id=event.id || crypto.randomUUID();if(!/^[a-f0-9-]{36}$/i.test(id))throw Error('Invalid event ID');
  const timestamp=event.timestamp || new Date().toISOString();if(typeof timestamp!=='string'||!Number.isFinite(Date.parse(timestamp)))throw Error('Invalid timestamp');
  const session=String(event.session || '');if(session && !/^[a-f0-9-]{36}$/i.test(session))throw Error('Invalid session ID');
  const result = {schema:'course-log-v1',id,timestamp:new Date(timestamp).toISOString(),session,type:event.type,actor:['ai','unknown','system'].includes(event.actor)?event.actor:'unknown',source:redact(event.source || 'unknown').slice(0,128)};
  for (const key of fields) if (event[key] !== undefined) {
    const value=event[key];if(typeof value!=='string' && typeof value!=='number' && value!==null)throw Error('Invalid field '+key);
    result[key] = scrub(value, key);
  }
  if (result.type !== 'ai_prompt') delete result.prompt;
  return result;
}
class Journal {
  constructor(root, source = 'vscode') {
    this.root = fs.realpathSync(root); this.source=source; this.session=crypto.randomUUID(); this.closed=false;
    const dir=path.join(this.root,'.ai','events'); fs.mkdirSync(dir,{recursive:true,mode:0o700});
    if (fs.realpathSync(dir) !== path.resolve(dir)) throw Error('Journal directory must not be a symlink');
    this.file=path.join(dir,new Date().toISOString().slice(0,10)+'.'+this.session+'.jsonl');
    this.write({type:'session_start',actor:'system'});
  }
  write(event) {
    if(this.closed) return;
    const safe = sanitizeEvent({...event,session:this.session,source:this.source});
    for (const k of ['file','old_file']) if (safe[k] !== undefined) { const rel=relativeFile(this.root,safe[k]); if(rel===null)return; safe[k]=rel; }
    if(safe.cwd!==undefined) { const rel=relativeFile(this.root,safe.cwd,true); if(rel===null)return; safe.cwd=rel; }
    fs.appendFileSync(this.file,JSON.stringify(safe)+'\n',{encoding:'utf8',mode:0o600});
    return safe;
  }
  close(reason='workspace_closed') { if(!this.closed){this.write({type:'session_end',actor:'system',reason});this.closed=true;} }
}
module.exports={redact,scrub,relativeFile,sanitizeEvent,Journal};
