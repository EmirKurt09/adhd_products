# e-Kampüs asistanını Windows oturumu açılınca arka planda (penceresiz) başlatır; çökerse yeniden başlatır.
#   Kur:      powershell -ExecutionPolicy Bypass -File scripts\windows-autostart.ps1
#   Kaldır:   powershell -ExecutionPolicy Bypass -File scripts\windows-autostart.ps1 -Remove
#   Loglar:   %LOCALAPPDATA%\ekampus\logs\ekampus.log
param([switch]$Remove)

$TaskName = "ekampus-asistan"
$Root = Split-Path -Parent $PSScriptRoot
$Pythonw = Join-Path $Root ".venv\Scripts\pythonw.exe"

if ($Remove) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Kaldırıldı: $TaskName"
    exit 0
}

if (-not (Test-Path $Pythonw)) {
    Write-Error "Bulunamadı: $Pythonw (önce .venv'i kur)"
    exit 1
}

$Action = New-ScheduledTaskAction -Execute $Pythonw -Argument "-m ekampus bot" -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Description "e-Kampüs asistanı: ödev/duyuru takibi ve Telegram botu" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Kuruldu ve başlatıldı: $TaskName"
Write-Host "Durum: Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
