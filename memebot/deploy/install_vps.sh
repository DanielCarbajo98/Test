#!/usr/bin/env bash
# Instalación en un VPS Ubuntu/Debian limpio. Ejecutar como root:
#   curl -O .../install_vps.sh && bash install_vps.sh
set -euo pipefail
apt-get update && apt-get install -y python3 python3-venv git
id memebot >/dev/null 2>&1 || useradd -m -s /bin/bash memebot
mkdir -p /opt/memebot && cp -r "$(dirname "$0")/.."/* /opt/memebot/ 2>/dev/null || true
cd /opt/memebot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
[ -f config.yaml ] || cp config.example.yaml config.yaml
[ -f .env ] || { cp .env.example .env; chmod 600 .env; }
chown -R memebot:memebot /opt/memebot
cp deploy/memebot.service /etc/systemd/system/memebot.service
systemctl daemon-reload
echo "Listo. Edita /opt/memebot/.env y /opt/memebot/config.yaml, luego:"
echo "  sudo -u memebot /opt/memebot/.venv/bin/python -m bot.preflight --config /opt/memebot/config.yaml"
echo "  systemctl enable --now memebot && journalctl -u memebot -f"
