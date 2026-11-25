"""Actualizaciones automáticas desde tu repositorio de GitHub.

  python -m bot.updater --loop 30        (servicio `updater` de docker-compose)

Cada N minutos:
  1. Pregunta a GitHub por el último commit de la rama (token de SOLO LECTURA en .env: GITHUB_TOKEN).
  2. Si es nuevo, descarga el código a una carpeta temporal y PASA LOS TESTS allí. Si fallan, no aplica nada.
  3. Si cambian las librerías o docker-compose/Dockerfile, no lo aplica a medias: avisa para usar INICIAR.
  4. Si todo va bien, copia el código nuevo encima del actual (nunca toca .env, config.yaml, state/ ni data/),
     escribe bot/VERSION y los procesos se reinician solos con la versión nueva.
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import httpx

log = logging.getLogger("updater")
API = "https://api.github.com"
CODE_DIRS = ["bot", "research", "tests", "deploy", "docs"]
CODE_FILES = ["README.md", "GUIA_50EUR.md", "TU_PARTE.md", "config.example.yaml", "config.50eur.yaml",
              "requirements-dev.txt", "INICIAR-Windows.bat", "CONFIGURAR-Windows.bat", "PARAR-Windows.bat",
              "INICIAR-Mac.command", "CONFIGURAR-Mac.command", "PARAR-Mac.command", ".gitattributes"]
# cambios en estos archivos exigen reconstruir/relanzar Docker: no se aplican solos
REBUILD_FILES = ["requirements.txt", "Dockerfile", "docker-compose.yml"]
NEVER_TOUCH = {".env", "config.yaml", "state", "data", "data_launches", ".update"}


def latest_sha(http: httpx.Client, repo: str, branch: str) -> str:
    r = http.get(f"{API}/repos/{repo}/commits/{branch}")
    r.raise_for_status()
    return r.json()["sha"]


def download(http: httpx.Client, repo: str, sha: str, dest: Path) -> Path:
    """Descarga y descomprime el código de ese commit. Devuelve la carpeta memebot/ dentro."""
    r = http.get(f"{API}/repos/{repo}/zipball/{sha}", follow_redirects=True)
    r.raise_for_status()
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        for m in z.infolist():
            p = Path(m.filename)
            if p.is_absolute() or ".." in p.parts:
                raise RuntimeError(f"ruta sospechosa en el zip: {m.filename}")
        z.extractall(dest)
    tops = [d for d in dest.iterdir() if d.is_dir()]
    root = tops[0] / "memebot" if len(tops) == 1 else dest / "memebot"
    if not (root / "bot" / "main.py").exists():
        raise RuntimeError("el paquete descargado no contiene memebot/bot")
    return root


def needs_rebuild(new: Path, cur: Path) -> list[str]:
    out = []
    for f in REBUILD_FILES:
        a, b = new / f, cur / f
        if a.exists() and (not b.exists() or a.read_bytes() != b.read_bytes()):
            out.append(f)
    return out


def run_tests(root: Path) -> tuple[bool, str]:
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"], cwd=root,
                       capture_output=True, text=True, timeout=1800)
    tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
    return r.returncode == 0, " | ".join(tail)


def apply(new: Path, cur: Path, sha: str):
    """Copia el código nuevo sobre el actual, EN SU SITIO (los montajes de Docker siguen viéndolo)."""
    for d in CODE_DIRS:
        src, dst = new / d, cur / d
        if not src.exists():
            continue
        dst.mkdir(exist_ok=True)
        new_files = {p.relative_to(src) for p in src.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
        for rel in sorted(new_files):
            if d == "bot" and rel.as_posix() == "VERSION":
                continue
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src / rel, dst / rel)
        for p in sorted(dst.rglob("*"), reverse=True):          # borrar lo que ya no existe
            rel = p.relative_to(dst)
            if p.is_file() and "__pycache__" not in rel.parts and rel not in new_files and rel.as_posix() != "VERSION":
                p.unlink()
    for f in CODE_FILES:
        if (new / f).exists() and f not in NEVER_TOUCH:
            shutil.copyfile(new / f, cur / f)
    (cur / "bot" / "VERSION").write_text(sha + "\n")              # último paso: dispara los reinicios


def notify(text: str):
    log.info(text)
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if tok and chat:
        try:
            httpx.post(f"https://api.telegram.org/bot{tok}/sendMessage", json={"chat_id": chat, "text": text}, timeout=10)
        except httpx.HTTPError:
            pass


def check_once(work: Path, repo: str, branch: str, token: str, test: bool = True,
               http: httpx.Client | None = None) -> dict:
    state = work / "state"
    state.mkdir(exist_ok=True)
    status_f = state / "update.json"
    status = json.loads(status_f.read_text()) if status_f.exists() else {}
    http = http or httpx.Client(timeout=120, headers={"Authorization": f"Bearer {token}",
                                                      "Accept": "application/vnd.github+json"})
    current = (work / "bot" / "VERSION").read_text().strip() if (work / "bot" / "VERSION").exists() else ""
    sha = latest_sha(http, repo, branch)
    status.update({"checked_at": int(time.time()), "current": current, "latest": sha})
    if sha == current:
        status["state"] = "al día"
    elif status.get("rejected") == sha:
        status["state"] = "versión rechazada (tests fallidos)"
    elif status.get("needs_rebuild_sha") == sha:
        status["state"] = "requiere INICIAR"
    else:
        root = download(http, repo, sha, work / ".update" / "staged")
        rebuild = needs_rebuild(root, work)
        if rebuild:
            status.update({"state": "requiere INICIAR", "needs_rebuild_sha": sha, "files": rebuild})
            notify(f"🔄 Hay una versión nueva del bot que necesita reinstalar ({', '.join(rebuild)}). "
                   f"Haz doble clic en INICIAR cuando puedas; mientras, sigue funcionando la versión actual.")
        else:
            ok, out = run_tests(root) if test else (True, "tests omitidos")
            if not ok:
                status.update({"state": "versión rechazada (tests fallidos)", "rejected": sha, "error": out})
                notify(f"⚠️ Actualización {sha[:7]} descartada: no pasó los tests en tu equipo. Sigue la versión actual.")
            else:
                apply(root, work, sha)
                status.update({"state": "actualizado", "applied_at": int(time.time()), "current": sha, "tests": out})
                notify(f"✅ Bot actualizado a la versión {sha[:7]} (tests superados). Se reinicia solo.")
        shutil.rmtree(work / ".update", ignore_errors=True)
    status_f.write_text(json.dumps(status, indent=1))
    return status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default="/work")
    ap.add_argument("--loop", type=float, default=0, help="comprobar cada N minutos")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s updater: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    token = os.getenv("GITHUB_TOKEN", "")
    repo = os.getenv("UPDATE_REPO", "danielcarbajo98/test")
    branch = os.getenv("UPDATE_BRANCH", "claude/bold-johnson-evz8g1")
    if not token:
        log.info("Sin GITHUB_TOKEN en .env: actualizaciones automáticas desactivadas (usa CONFIGURAR para activarlas).")
        while a.loop:                     # no salir: Docker lo reiniciaría en bucle
            time.sleep(3600)
        return
    while True:
        try:
            st = check_once(Path(a.work), repo, branch, token)
            log.info("Estado: %s", st.get("state"))
        except Exception as e:  # noqa: BLE001
            log.warning("No se pudo comprobar actualizaciones: %s", e)
        if not a.loop:
            break
        time.sleep(a.loop * 60)


if __name__ == "__main__":
    main()
