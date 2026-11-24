#!/usr/bin/env bash
# Instalación 24/7 en un servidor Ubuntu/Debian (VPS o Oracle Cloud Free). Ejecutar DENTRO de la carpeta memebot:
#   sudo bash deploy/install.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v docker >/dev/null 2>&1; then
  echo "==> Instalando Docker"
  apt-get update -y && apt-get install -y ca-certificates curl
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker

[ -f config.yaml ] || cp config.50eur.yaml config.yaml
if [ ! -f .env ]; then
  cp .env.example .env
  TOKEN=$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)
  sed -i "s/^DASHBOARD_TOKEN=.*/DASHBOARD_TOKEN=$TOKEN/" .env
fi
chmod 600 .env
mkdir -p state data data_launches

if grep -q "^SOLANA_RPC_URL=https://mainnet.helius-rpc.com/?api-key=TU_KEY" .env; then
  echo
  echo "!! Edita .env (nano .env) y pon tus claves: SOLANA_RPC_URL, JUPITER_API_KEY, WALLET_PRIVATE_KEY, Telegram."
  echo "   Luego vuelve a ejecutar: sudo bash deploy/install.sh"
  exit 0
fi

echo "==> Construyendo y arrancando (bot, panel, cerebro, recolectores)"
docker compose up -d --build
docker compose run --rm bot python -m bot.preflight --config config.yaml || true
TOKEN=$(grep ^DASHBOARD_TOKEN= .env | cut -d= -f2)
echo
echo "Listo. Todo corre 24/7 y se reinicia solo."
echo "  Estado:   docker compose ps"
echo "  Logs:     docker compose logs -f bot"
echo "  Panel:    desde TU ordenador ->  ssh -L 8050:127.0.0.1:8050 usuario@IP_DEL_SERVIDOR"
echo "            y abre  http://127.0.0.1:8050/?token=$TOKEN"
echo "  A real:   cambia 'mode: paper' por 'mode: live' en config.yaml y ->  docker compose restart bot"
