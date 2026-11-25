# Arranque en Windows (Docker Desktop). Lo lanza INICIAR-Windows.bat con doble clic.
$ErrorActionPreference = "Continue"   # los avisos de docker van por stderr: no son errores
Set-Location (Join-Path $PSScriptRoot "..")
function Paso($t) { Write-Host "`n== $t" -ForegroundColor Cyan }
function Fin($t) { Write-Host "`n$t" -ForegroundColor Yellow; Read-Host "Pulsa Enter para cerrar"; exit 1 }

Paso "1/5 Comprobando Docker Desktop"
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Fin "Docker Desktop no esta instalado. Instalalo (paso 4 de la guia) y vuelve a hacer doble clic en INICIAR." }
cmd /c "docker info >nul 2>nul"
if ($LASTEXITCODE -ne 0) { Fin "Docker Desktop esta cerrado o arrancando. Abrelo, espera a que ponga 'Engine running' (abajo a la izquierda) y vuelve a hacer doble clic en INICIAR." }

Paso "2/5 Preparando carpetas"
New-Item -ItemType Directory -Force state, data, data_launches | Out-Null
if (-not (Test-Path config.yaml)) { Copy-Item config.50eur.yaml config.yaml }
if (-not (Test-Path .env)) { New-Item .env -ItemType File | Out-Null }
if ((Test-Path seed\data_seed.tar.gz) -and -not (Get-ChildItem data -ErrorAction SilentlyContinue)) { tar -xzf seed\data_seed.tar.gz }

Paso "3/5 Construyendo el programa (5-10 min la primera vez)"
docker compose build
if ($LASTEXITCODE -ne 0) { Fin "Fallo al construir. Haz una captura de esta ventana y enviamela." }

if (-not (Select-String -Path .env -Pattern '^WALLET_PRIVATE_KEY=.' -Quiet)) {
  Paso "4/5 Asistente de configuracion"
  $here = (Get-Location).Path
  docker compose run --rm --no-deps -v "${here}:/work" -w /work bot python -m bot.setup
} else { Paso "4/5 Configuracion existente (para cambiar claves: doble clic en CONFIGURAR-Windows)" }

Paso "5/5 Arrancando bot, panel, cerebro y recolectores"
docker compose up -d --force-recreate
$m = Select-String -Path .env -Pattern '^DASHBOARD_TOKEN=(.*)'
if (-not $m) { Fin "Falta la configuracion. Vuelve a hacer doble clic en INICIAR y completa el asistente." }
$token = $m.Matches[0].Groups[1].Value.Trim()
$url = "http://127.0.0.1:8050/?token=$token"
Start-Sleep 5
Start-Process $url
Write-Host "`nLISTO. Todo funciona en segundo plano aunque cierres esta ventana." -ForegroundColor Green
Write-Host "Panel (guardalo en favoritos): $url"
Read-Host "`nPulsa Enter para cerrar esta ventana"
