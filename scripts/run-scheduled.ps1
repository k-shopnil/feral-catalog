# Runs one catalog update: pull config changes, crawl/match/publish, push to GitHub Pages.
# Invoked by Windows Task Scheduler (see install-task.ps1). Plan §4.6.1.
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$lock = Join-Path $repo "data\run.lock"
if (Test-Path $lock) {
    $age = (Get-Date) - (Get-Item $lock).LastWriteTime
    if ($age.TotalMinutes -lt 60) { exit 0 }   # another run is active
}
New-Item -ItemType File -Force -Path $lock | Out-Null

try {
    # lower our priority so the PC stays responsive
    (Get-Process -Id $PID).PriorityClass = "BelowNormal"
    git -C $repo pull --ff-only --quiet 2>&1 | Out-Null
    & "$repo\.venv\Scripts\python.exe" -m catalog run --budget-minutes 20 *>> (Join-Path $repo "logs\scheduled.log")
}
finally {
    Remove-Item $lock -ErrorAction SilentlyContinue
    # keep 14 days of logs
    Get-ChildItem (Join-Path $repo "logs") -Filter "run-*.log" | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-14) } | Remove-Item -ErrorAction SilentlyContinue
}
