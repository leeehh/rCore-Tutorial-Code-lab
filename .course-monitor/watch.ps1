param([Parameter(Mandatory=$true)][string]$ProjectPath,[switch]$Snapshot)
$ErrorActionPreference = 'Stop'
$project = (Resolve-Path -LiteralPath $ProjectPath).Path
$eventDirectory = Join-Path $project '.ai\events'
$statusFile = Join-Path $project '.ai\ide\live-viewer-status.json'
New-Item -ItemType Directory -Force -Path (Split-Path $statusFile) | Out-Null
$started = [DateTimeOffset]::UtcNow
$seen = [System.Collections.Generic.HashSet[string]]::new()
$versions = @{}
$labels = @{session_start='开始记录';session_end='结束记录';file_open='打开文件';file_close='关闭文件';file_edit='修改文件';file_save='保存文件';file_create='新建文件';file_delete='删除文件';file_rename='重命名文件';command_start='命令开始';command_end='命令结束';task_start='任务开始';task_end='任务结束';shell_integration_unavailable='终端集成不可用';ai_prompt='AI 提问';ai_command_result='AI 命令结果';ai_file_operation='AI 文件操作'}

function Read-NewEvents {
    $found = [System.Collections.Generic.List[object]]::new()
    foreach ($file in @(Get-ChildItem -LiteralPath $eventDirectory -Filter '*.jsonl' -File -ErrorAction SilentlyContinue)) {
        $version = "$($file.Length):$($file.LastWriteTimeUtc.Ticks)"
        if ($versions[$file.FullName] -eq $version) { continue }
        $stream = [IO.FileStream]::new($file.FullName, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
        try {
            # Bound initial display for long-running course repositories.
            $tail = $stream.Length -gt 2MB
            if ($tail) { [void]$stream.Seek(-2MB, [IO.SeekOrigin]::End) }
            $reader = [IO.StreamReader]::new($stream, [Text.Encoding]::UTF8)
            $data = $reader.ReadToEnd()
            if ($tail) { $first = $data.IndexOf("`n"); if ($first -ge 0) { $data = $data.Substring($first+1) } }
            $last = $data.LastIndexOf("`n")
            if ($last -ge 0) {
                foreach ($line in ($data.Substring(0,$last) -split "`n")) {
                    if (-not $line.Trim()) { continue }
                    $event = $line | ConvertFrom-Json
                    if ($event.id -and $seen.Add([string]$event.id)) { $found.Add($event) }
                }
            }
            $versions[$file.FullName] = $version
        } finally { if ($reader) { $reader.Dispose() }; $stream.Dispose() }
    }
    return $found | Sort-Object timestamp
}

if ($Snapshot) { @(Read-NewEvents) | ConvertTo-Json -Depth 8; exit }
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[Windows.Forms.Application]::EnableVisualStyles()
$form = [Windows.Forms.Form]::new()
$form.Text = 'rCore 实时日志 — ' + (Split-Path -Leaf $project)
$form.Size = [Drawing.Size]::new(1120,760)
$form.MinimumSize = [Drawing.Size]::new(850,560)
$form.StartPosition = 'CenterScreen'
$form.Font = [Drawing.Font]::new('Microsoft YaHei UI',10)
$form.BackColor = [Drawing.Color]::White
$header = [Windows.Forms.Label]::new()
$header.Text = "rCore 实验 · 实时操作记录`n$project"
$header.Dock = 'Top'; $header.Height = 68; $header.Padding = [Windows.Forms.Padding]::new(12)
$header.BackColor = [Drawing.Color]::FromArgb(233,241,253)
$footer = [Windows.Forms.Label]::new()
$footer.Dock = 'Bottom'; $footer.Height = 44; $footer.Padding = [Windows.Forms.Padding]::new(12)
$detail = [Windows.Forms.TextBox]::new()
$detail.Dock = 'Bottom'; $detail.Height = 145; $detail.Multiline = $true; $detail.ReadOnly = $true; $detail.ScrollBars = 'Vertical'
$detail.Text = '在 VS Code 中操作真实仓库。点击一行查看完整记录。仅显示最近 500 行；启动前的数据标为“历史”。'
$grid = [Windows.Forms.DataGridView]::new()
$grid.Dock = 'Fill'; $grid.ReadOnly = $true; $grid.AllowUserToAddRows = $false; $grid.AllowUserToDeleteRows = $false
$grid.RowHeadersVisible = $false; $grid.SelectionMode = 'FullRowSelect'; $grid.MultiSelect = $false
$grid.AutoSizeColumnsMode = 'Fill'; $grid.BackgroundColor = [Drawing.Color]::White
foreach ($column in @(@('mode','范围',45),@('time','本地时间',105),@('source','来源',65),@('action','行为',75),@('target','文件或命令',270),@('result','结果',65))) {
    $index = $grid.Columns.Add($column[0],$column[1]); $grid.Columns[$index].FillWeight = $column[2]
    $grid.Columns[$index].SortMode = 'NotSortable'
}
$grid.add_SelectionChanged({ if ($grid.SelectedRows.Count) { $detail.Text = $grid.SelectedRows[0].Tag | ConvertTo-Json -Depth 8 } })
$form.Controls.Add($grid); $form.Controls.Add($detail); $form.Controls.Add($footer); $form.Controls.Add($header)
$script:lastEvent = $null
$script:liveCount = 0
$timer = [Windows.Forms.Timer]::new(); $timer.Interval = 500
$timer.add_Tick({
    try {
        $incoming = @(Read-NewEvents)
        foreach ($event in $incoming) {
            $when = [DateTimeOffset]$event.timestamp
            $live = $when -ge $started
            if ($live) { $script:liveCount++ }
            $target = if ($event.file) { $event.file } elseif ($event.command) { $event.command } elseif ($event.prompt) { $event.prompt } elseif ($event.task) { $event.task } else { $event.reason }
            $result = if ($null -ne $event.exit_code) { "退出码 $($event.exit_code)" } else { '' }
            $action = if ($labels.ContainsKey($event.type)) { $labels[$event.type] } else { $event.type }
            $mode = if ($live) { '实时' } else { '历史' }
            $source = if ($event.source -eq 'vscode') { 'VS Code' } else { $event.source }
            $index = $grid.Rows.Add($mode,$when.ToLocalTime().ToString('MM-dd HH:mm:ss'),$source,$action,$target,$result)
            $grid.Rows[$index].Tag = $event
            if (-not $live) { $grid.Rows[$index].DefaultCellStyle.ForeColor = [Drawing.Color]::Gray }
            if ($grid.Rows.Count -gt 500) { $grid.Rows.RemoveAt(0) }
            $script:lastEvent = $event
        }
        if ($incoming.Count) {
            $grid.FirstDisplayedScrollingRowIndex = $grid.Rows.Count-1
            $state = @{pid=$PID;shown=$form.Visible;project=$project;rows=$grid.Rows.Count;liveEvents=$script:liveCount;updatedAt=[DateTimeOffset]::UtcNow.ToString('o');lastEventId=$script:lastEvent.id;lastEventType=$script:lastEvent.type;lastEventFile=$script:lastEvent.file}
            [IO.File]::WriteAllText($statusFile,($state | ConvertTo-Json),[Text.UTF8Encoding]::new($false))
        }
        $footer.ForeColor = [Drawing.Color]::DarkGreen
        $footer.Text = "每 0.5 秒自动刷新 · 本次新增 $script:liveCount 条 · 来源：项目 .ai/events · 点击记录查看详情"
    } catch {
        $footer.ForeColor = [Drawing.Color]::DarkRed
        $footer.Text = "读取日志失败：$($_.Exception.Message)"
        [IO.File]::WriteAllText($statusFile,(@{pid=$PID;shown=$form.Visible;error=$_.Exception.Message} | ConvertTo-Json),[Text.UTF8Encoding]::new($false))
    }
})
$form.add_Shown({ $timer.Start() })
$form.add_FormClosed({ $timer.Stop(); $timer.Dispose() })
[void]$form.ShowDialog()
$form.Dispose()
