# Registers the "feral-catalog" scheduled task: at logon (+2 min) and every 30 minutes while logged on.
$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $repo "scripts\run-scheduled.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$logon.Delay = "PT2M"
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Minutes 30)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 45) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName "feral-catalog" -Action $action -Trigger @($logon, $repeat) -Settings $settings `
    -Principal $principal -Description "feral: update the TV catalog from the BDIX media server" -Force | Out-Null
Write-Host "Scheduled task 'feral-catalog' installed (at logon + every 30 min)."
