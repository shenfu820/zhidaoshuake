param(
    [int]$MainPid,
    [string]$Task = "zhs-fuck"
)
$ErrorActionPreference = "SilentlyContinue"
$file = Join-Path $env:DSH_PROGRESS_DIR "$Task.jsonl"
$log  = Join-Path $PSScriptRoot "logs\info.log"
$last = ""
function Emit($state, $msg) {
    $evt = @{ v = 1; task = $Task; state = $state; msg = $msg } | ConvertTo-Json -Compress
    Add-Content -Path $file -Value $evt -Encoding UTF8
}
Emit "running" "monitor started, tailing logs/info.log"
while ($true) {
    $alive = $null -ne (Get-Process -Id $MainPid -ErrorAction SilentlyContinue)
    $line = ""
    if (Test-Path $log) {
        $line = Get-Content $log -Tail 1 -Encoding UTF8
    }
    if ($line -and $line -ne $last) {
        $last = $line
        $msg = $line -replace '^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}:\s+', ''
        Emit "running" $msg
    }
    if (-not $alive) {
        Emit "done" "main process exited"
        break
    }
    Start-Sleep -Seconds 10
}