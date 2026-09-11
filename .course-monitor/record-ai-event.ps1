# One JSON event on stdin. Keep prompts out of command-line arguments.
$ErrorActionPreference = 'Stop'
[Console]::In.ReadToEnd() | python3 "$PSScriptRoot\ai-ingest.py"
if ($LASTEXITCODE -ne 0) { throw 'AI event recording failed' }