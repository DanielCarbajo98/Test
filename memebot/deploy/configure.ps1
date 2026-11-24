# Vuelve a abrir el asistente (cambiar claves, capital o Telegram). Lo lanza CONFIGURAR-Windows.bat
Set-Location (Join-Path $PSScriptRoot "..")
$here = (Get-Location).Path
docker compose run --rm --no-deps -v "${here}:/work" -w /work bot python -m bot.setup
docker compose restart bot
Read-Host "Pulsa Enter para cerrar"
