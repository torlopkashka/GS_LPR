# Установка агента ворот как фоновой задачи Windows.
# Запуск: PowerShell от имени администратора, из папки agent:
#   powershell -ExecutionPolicy Bypass -File .\install-windows.ps1
#
# Агент стартует при включении компьютера (даже без входа пользователя),
# перезапускается при сбое, журнал пишет в agent\agent.log.

$ErrorActionPreference = "Stop"
$Dir = $PSScriptRoot
$TaskName = "GS-LPR Gate Agent"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Запустите PowerShell от имени администратора." -ForegroundColor Red
    exit 1
}

# 1. Python
$py = Get-Command py -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Host "Не найден Python. Установите Python 3.11+ с https://www.python.org/downloads/windows/" -ForegroundColor Red
    Write-Host "(при установке отметьте 'Install for all users' и 'Add python.exe to PATH')"
    exit 1
}

# 2. Виртуальное окружение и зависимости
if (-not (Test-Path "$Dir\venv")) {
    & py -3 -m venv "$Dir\venv"
}
& "$Dir\venv\Scripts\python.exe" -m pip install --upgrade pip | Out-Null
& "$Dir\venv\Scripts\python.exe" -m pip install -r "$Dir\requirements.txt"

# 3. Конфигурация
if (-not (Test-Path "$Dir\agent.yaml")) {
    Copy-Item "$Dir\agent.example.yaml" "$Dir\agent.yaml"
    $envFile = Join-Path (Split-Path $Dir -Parent) ".env"
    if (Test-Path $envFile) {
        $token = (Get-Content $envFile | Where-Object { $_ -match '^AGENT_TOKEN=' }) -replace '^AGENT_TOKEN=', '' -replace '\s+#.*$', ''
        if ($token) {
            (Get-Content "$Dir\agent.yaml") -replace 'REPLACE_WITH_AGENT_TOKEN', $token.Trim() |
                Set-Content "$Dir\agent.yaml" -Encoding UTF8
        }
    }
    Write-Host "Создан agent.yaml: проверьте тип реле и COM-порт." -ForegroundColor Yellow
}

# 4. Задача планировщика: при старте системы, от SYSTEM.
# run-agent.cmd перезапускает агент, если тот завершится.
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$Dir\run-agent.cmd`"" -WorkingDirectory $Dir
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description "Агент управления воротами GS LPR" | Out-Null

Write-Host ""
Write-Host "Готово. Дальше:" -ForegroundColor Green
Write-Host "  1) проверьте реле:   .\venv\Scripts\python.exe gate_agent.py -c agent.yaml --test"
Write-Host "  2) запустите агент:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "  3) журнал:           Get-Content .\agent.log -Wait"
