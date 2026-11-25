#!/usr/bin/env bash
# Arranque en Mac (o Linux con escritorio) usando Docker Desktop. Lo lanza INICIAR-Mac.command con doble clic.
set -euo pipefail
cd "$(dirname "$0")/.."
paso() { printf "\n\033[36m== %s\033[0m\n" "$1"; }
fin() { printf "\n\033[33m%s\033[0m\n" "$1"; read -r -p "Pulsa Enter para cerrar"; exit 1; }

paso "1/5 Comprobando Docker Desktop"
docker info >/dev/null 2>&1 || fin "Docker Desktop no está instalado o no está abierto. Ábrelo, espera a que la ballena quede quieta y vuelve a hacer doble clic en INICIAR."

paso "2/5 Preparando carpetas"
mkdir -p state data data_launches
[ -f config.yaml ] || cp config.50eur.yaml config.yaml
[ -f .env ] || { touch .env; chmod 600 .env; }
if [ -f seed/data_seed.tar.gz ] && [ -z "$(ls -A data 2>/dev/null)" ]; then tar -xzf seed/data_seed.tar.gz; fi

paso "3/5 Construyendo el programa (5-10 min la primera vez)"
docker compose build || fin "Fallo al construir. Haz una captura de esta ventana y envíamela."

if ! grep -q "^WALLET_PRIVATE_KEY=." .env; then
  paso "4/5 Asistente de configuración"
  docker compose run --rm --no-deps -v "$PWD":/work -w /work bot python -m bot.setup
else
  paso "4/5 Configuración existente"
fi

paso "5/5 Arrancando bot, panel, cerebro y recolectores"
docker compose up -d --force-recreate
TOKEN=$(grep ^DASHBOARD_TOKEN= .env | cut -d= -f2)
URL="http://127.0.0.1:8050/?token=$TOKEN"
sleep 5
(command -v open >/dev/null && open "$URL") || (command -v xdg-open >/dev/null && xdg-open "$URL") || true
printf "\n\033[32mLISTO. Todo funciona en segundo plano aunque cierres esta ventana.\033[0m\nPanel (guárdalo en favoritos): %s\n" "$URL"
read -r -p "Pulsa Enter para cerrar esta ventana"
