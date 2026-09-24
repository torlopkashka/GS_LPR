# Первичная настройка сервера на Windows 10/11 с Docker Desktop.
# Запуск из папки проекта:
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1
#
# Создаёт .env со случайными ключами и server\config.yaml из примера,
# затем собирает и запускает контейнер.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function New-Secret { -join ((1..32) | ForEach-Object { '{0:x2}' -f (Get-Random -Maximum 256) }) }

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "Не найден Docker. Установите Docker Desktop: https://www.docker.com/products/docker-desktop/" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path ".env")) {
    $password = Read-Host "Придумайте пароль для веб-интерфейса (логин admin, без символов $ и #)"
    $text = Get-Content ".env.example" -Raw
    $text = $text -replace 'ADMIN_PASSWORD=.*', ('ADMIN_PASSWORD=' + $password.Replace('$', '$$'))
    $text = $text -replace 'SECRET_KEY=.*', "SECRET_KEY=$(New-Secret)"
    $text = $text -replace 'AGENT_TOKEN=.*', "AGENT_TOKEN=$(New-Secret)"
    [IO.File]::WriteAllText("$PSScriptRoot\.env", $text, (New-Object Text.UTF8Encoding $false))
    Write-Host "Создан .env. Токен бота Telegram и chat id впишите в него позже." -ForegroundColor Green
}

if (-not (Test-Path "server\config.yaml")) {
    Copy-Item "server\config.example.yaml" "server\config.yaml"
    Write-Host "Создан server\config.yaml: впишите RTSP-адреса камер и перезапустите: docker compose restart" -ForegroundColor Yellow
}

New-Item -ItemType Directory -Force -Path "data" | Out-Null
docker compose up -d --build
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""
Write-Host "Сервер запущен: http://localhost:8000" -ForegroundColor Green
Write-Host "Дальше установите агент ворот: agent\install-windows.ps1 (от имени администратора)"
