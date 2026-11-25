# Mudanza del bot de este PC a tu servidor gratuito de Oracle. Lo lanza MUDAR-A-SERVIDOR-Windows.bat.
# Lleva TODO: codigo, datos recogidos, aprendizaje, cartera simulada, .env y config.yaml (por SSH, cifrado).
# Despues para el bot de este PC para que no haya dos funcionando a la vez.
$ErrorActionPreference = "Continue"
Set-Location (Join-Path $PSScriptRoot "..")
function Paso($t) { Write-Host "`n== $t" -ForegroundColor Cyan }
function Fin($t) { Write-Host "`n$t" -ForegroundColor Yellow; Read-Host "Pulsa Enter para cerrar"; exit 1 }

Paso "1/6 Comprobando herramientas"
foreach ($c in "ssh", "scp", "tar") {
  if (-not (Get-Command $c -ErrorAction SilentlyContinue)) { Fin "Falta '$c'. En Windows: Configuracion > Aplicaciones > Caracteristicas opcionales > Cliente OpenSSH." }
}

Paso "2/6 Datos del servidor"
$ip = (Read-Host "IP publica del servidor (la ves en Oracle, en la ficha de la instancia)").Trim()
if (-not ($ip -match '^\d{1,3}(\.\d{1,3}){3}$')) { Fin "Esa IP no parece valida (ejemplo: 140.238.12.34)." }
Write-Host "Arrastra aqui el archivo de la clave privada que descargaste al crear el servidor (termina en .key) y pulsa Enter:"
$key = (Read-Host "Clave").Trim().Trim('"')
if (-not (Test-Path $key)) { Fin "No encuentro ese archivo de clave." }
# OpenSSH se niega a usar una clave que pueden leer otros usuarios del PC
icacls $key /inheritance:r /grant:r "$($env:USERNAME):(R)" | Out-Null
$dest = "ubuntu@$ip"
$opts = @("-i", $key, "-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=30")
ssh @opts $dest "echo conectado" 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) { Fin "No pude conectar. Revisa la IP, la clave y que la instancia este en estado 'En ejecucion'." }

Paso "3/6 Parando el bot de este PC (para que no queden dos funcionando)"
if (Get-Command docker -ErrorAction SilentlyContinue) { cmd /c "docker compose down >nul 2>nul" }

Paso "4/6 Empaquetando todo (puede tardar unos minutos)"
$pkg = Join-Path $env:TEMP "memebot-mudanza.tar.gz"
Remove-Item $pkg -ErrorAction SilentlyContinue
$here = (Get-Location).Path
$parent = Split-Path $here -Parent
$name = Split-Path $here -Leaf
tar -czf $pkg --exclude="$name/.update" --exclude="__pycache__" --exclude="$name/.pytest_cache" -C $parent $name
if ($LASTEXITCODE -ne 0) { Fin "No se pudo empaquetar. Haz una captura y enviamela." }
Write-Host ("Paquete: {0:N0} MB" -f ((Get-Item $pkg).Length / 1MB))

Paso "5/6 Subiendo al servidor (cifrado)"
scp @opts $pkg "${dest}:memebot-mudanza.tar.gz"
if ($LASTEXITCODE -ne 0) { Fin "Fallo la subida. Vuelve a intentarlo; el bot de este PC esta parado: si quieres reanudarlo, doble clic en INICIAR." }
Remove-Item $pkg -ErrorAction SilentlyContinue

Paso "6/6 Instalando y arrancando en el servidor (5-15 min la primera vez)"
$cmd = "set -e; rm -rf memebot.new; mkdir memebot.new; tar -xzf memebot-mudanza.tar.gz -C memebot.new; " +
       "if [ -d memebot ]; then mv memebot memebot.old.`$(date +%s); fi; mv memebot.new/$name memebot; rm -rf memebot.new memebot-mudanza.tar.gz; " +
       "cd memebot; sudo bash deploy/install.sh --no-setup; sudo bash deploy/tailscale.sh"
ssh -t @opts $dest $cmd
if ($LASTEXITCODE -ne 0) { Fin "Algo fallo en el servidor. Haz una captura de esta ventana y enviamela." }

Write-Host "`nMUDANZA COMPLETADA. El bot ya funciona 24/7 en tu servidor." -ForegroundColor Green
Write-Host "Puedes apagar este PC. Para que Docker Desktop no arranque solo: Docker Desktop > Settings > General > desmarca 'Start Docker Desktop when you sign in'."
Write-Host "No vuelvas a hacer doble clic en INICIAR en este PC: habria dos bots a la vez."
Read-Host "`nPulsa Enter para cerrar"
