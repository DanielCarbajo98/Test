#!/usr/bin/env bash
# Instalación 24/7 en un servidor Ubuntu/Debian (Oracle Cloud Free, VPS...). Desde la carpeta memebot:
#   sudo bash deploy/install.sh
# Puedes ejecutarlo las veces que quieras: no borra nada y solo hace lo que falte.
set -euo pipefail
cd "$(dirname "$0")/.."
B="\033[1m"; G="\033[32m"; X="\033[0m"

echo -e "${B}[1/5] Docker${X}"
if ! command -v docker >/dev/null 2>&1; then
  apt-get update -y && apt-get install -y ca-certificates curl
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker >/dev/null 2>&1 || true

# Servidores pequeños (p. ej. Oracle VM.Standard.E2.1.Micro, 1 GB): añadir 4 GB de memoria de intercambio
MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if [ "$MEM_MB" -lt 3000 ] && ! swapon --show | grep -q .; then
  echo -e "${B}      Poca memoria (${MEM_MB} MB): creando 4 GB de swap${X}"
  fallocate -l 4G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=4096
  chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo -e "${B}[2/5] Preparando carpetas${X}"
mkdir -p state data data_launches
[ -f config.yaml ] || cp config.50eur.yaml config.yaml
[ -f .env ] || { touch .env; chmod 600 .env; }
# datos semilla: el cerebro arranca con lo ya recogido en vez de empezar de cero
if [ -f seed/data_seed.tar.gz ] && [ -z "$(ls -A data 2>/dev/null)" ]; then
  tar -xzf seed/data_seed.tar.gz && echo "  datos semilla cargados"
fi

echo -e "${B}[3/5] Construyendo el programa (2-5 min la primera vez)${X}"
docker compose build -q

if ! grep -q "^WALLET_PRIVATE_KEY=." .env; then
  echo -e "${B}[4/5] Asistente de configuración${X}"
  docker compose run --rm --no-deps -v "$PWD":/work -w /work bot python -m bot.setup
else
  echo -e "${B}[4/5] Configuración existente${X} (para cambiarla: sudo docker compose run --rm --no-deps -v \"\$PWD\":/work -w /work bot python -m bot.setup)"
fi

echo -e "${B}[5/5] Arrancando bot, panel, cerebro y recolectores${X}"
docker compose up -d
sleep 5
docker compose run --rm --no-deps bot python -m bot.preflight --config config.yaml || true

TOKEN=$(grep ^DASHBOARD_TOKEN= .env | cut -d= -f2)
IP=$(curl -s -4 --max-time 5 ifconfig.me || echo IP_DEL_SERVIDOR)
echo
echo -e "${G}${B}Listo. Todo funciona 24/7 y se reinicia solo.${X}"
echo
echo "  PANEL — en TU ordenador abre una terminal y escribe:"
echo "      ssh -L 8050:127.0.0.1:8050 -i TU_CLAVE ubuntu@$IP"
echo "    y en el navegador:  http://127.0.0.1:8050/?token=$TOKEN"
echo
echo "  Ver qué hace:      sudo docker compose logs -f bot"
echo "  Estado:            sudo docker compose ps"
echo "  PASAR A REAL:      sudo nano config.yaml   (mode: paper -> mode: live)   y   sudo docker compose restart bot"
echo "  Parar todo:        sudo docker compose down"
