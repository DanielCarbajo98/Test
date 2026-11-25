"""Arranque de cada servicio: instala SOLO las librerías nuevas antes de ejecutar el programa.

  python -m bot.bootstrap python -m bot.main --config config.yaml      (entrypoint de docker-compose)

Si una actualización añade librerías a requirements.txt, no hace falta reconstruir la imagen:
al reiniciarse, cada servicio instala lo que falte en el volumen compartido /deps (una sola vez,
con un candado para que no lo hagan dos servicios a la vez) y arranca. Solo usa la librería estándar.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import subprocess
import sys
from pathlib import Path

LIVE = Path("/app/requirements-live.txt")     # el de tu carpeta (lo actualiza el actualizador)
BAKED = Path("/app/requirements.txt")         # el que se instaló al construir la imagen
DEPS = Path(os.getenv("PYTHONUSERBASE", "/deps"))


def ensure_deps(live: Path = LIVE, baked: Path = BAKED, deps: Path = DEPS, installer=None) -> str:
    """Devuelve 'al día', 'instalado' o 'error'."""
    try:
        want = live.read_bytes()
    except OSError:
        return "al día"
    if baked.exists() and baked.read_bytes() == want:
        return "al día"
    deps.mkdir(parents=True, exist_ok=True)
    marker = deps / ".requirements.sha256"
    digest = hashlib.sha256(want).hexdigest()
    with open(deps / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if marker.exists() and marker.read_text().strip() == digest:
            return "al día"
        print("bootstrap: instalando librerías nuevas…", flush=True)
        env = {**os.environ, "PYTHONUSERBASE": str(deps)}
        cmd = [sys.executable, "-m", "pip", "install", "--user", "--no-cache-dir", "--disable-pip-version-check",
               "-r", str(live)]
        ok = (installer or (lambda c, e: subprocess.run(c, env=e).returncode == 0))(cmd, env)
        if not ok:
            print("bootstrap: no se pudieron instalar; se reintentará en el próximo arranque", flush=True)
            return "error"
        marker.write_text(digest)
        return "instalado"


def main():
    if len(sys.argv) < 2:
        print("uso: python -m bot.bootstrap <comando…>")
        raise SystemExit(2)
    ensure_deps()
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
