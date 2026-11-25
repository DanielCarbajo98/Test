#!/usr/bin/env bash
# Panel privado desde el móvil y el PC, gratis: Tailscale crea una red privada solo entre TUS dispositivos.
# El panel NO queda abierto a internet: solo lo ven los dispositivos con tu cuenta de Tailscale.
#   sudo bash deploy/tailscale.sh
set -euo pipefail
cd "$(dirname "$0")/.."
B="\033[1m"; G="\033[32m"; X="\033[0m"
if ! command -v tailscale >/dev/null 2>&1; then
  echo -e "${B}Instalando Tailscale${X}"
  curl -fsSL https://tailscale.com/install.sh | sh
fi
systemctl enable --now tailscaled >/dev/null 2>&1 || true
if ! tailscale status >/dev/null 2>&1; then
  echo -e "${B}Abre el enlace que aparece abajo e inicia sesión (por ejemplo con tu cuenta de Google).${X}"
  tailscale up --hostname memebot
fi
# publica el panel (que sigue escuchando solo en 127.0.0.1) dentro de tu red privada, con HTTPS
tailscale serve --bg 8050 >/dev/null 2>&1 || tailscale serve --bg http://127.0.0.1:8050 >/dev/null 2>&1 || true
NAME=$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || echo memebot)
TOKEN=$(grep ^DASHBOARD_TOKEN= .env | cut -d= -f2 || true)
echo
echo -e "${G}${B}Panel privado listo.${X}"
echo "  1. Instala la app Tailscale en el móvil y en el PC e inicia sesión con la MISMA cuenta."
echo "  2. Abre:  https://${NAME}/?token=${TOKEN}"
echo "     (guárdalo en favoritos; no compartas el enlace: lleva tu token)"
echo "  Si la primera vez sale un aviso de HTTPS, actívalo en https://login.tailscale.com/admin/dns (HTTPS Certificates)."
