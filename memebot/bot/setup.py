"""Asistente de configuración: te pregunta lo mínimo, comprueba cada clave al momento y lo deja todo listo.

  python -m bot.setup          (o lo lanza automáticamente  sudo bash deploy/install.sh)

Escribe .env (permisos 600) y config.yaml. No envía tus claves a ningún sitio salvo al
propio servicio que las emite, para comprobar que funcionan.
"""
from __future__ import annotations

import getpass
import os
import secrets
import shutil
import sys
import time
from pathlib import Path

import httpx
import yaml

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL = "So11111111111111111111111111111111111111112"
G, Y, R, B, X = "\033[32m", "\033[33m", "\033[31m", "\033[1m", "\033[0m"


def ok(m):
    print(f"  {G}✔{X} {m}")


def warn(m):
    print(f"  {Y}!{X} {m}")


def bad(m):
    print(f"  {R}✘{X} {m}")


def ask(q: str, default: str = "", secret: bool = False) -> str:
    sfx = f" [{default}]" if default and not secret else ""
    try:
        v = (getpass.getpass(f"  {q}{sfx}: ") if secret else input(f"  {q}{sfx}: ")).strip()
    except EOFError:
        v = ""
    return v or default


def yes(q: str, default: bool = True) -> bool:
    v = ask(q + (" (S/n)" if default else " (s/N)")).lower()
    return default if not v else v in ("s", "si", "sí", "y", "yes")


# --------------------------------------------------------------------------- comprobaciones
def check_rpc(url: str) -> bool:
    try:
        r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "getSlot"}, timeout=15)
        return r.status_code == 200 and "result" in r.json()
    except (httpx.HTTPError, ValueError):
        return False


def check_jupiter(key: str) -> bool:
    try:
        r = httpx.get("https://api.jup.ag/swap/v2/order", headers={"x-api-key": key}, timeout=15,
                      params={"inputMint": USDC, "outputMint": SOL, "amount": "10000000", "slippageBps": "100"})
        return r.status_code == 200 and int(r.json().get("outAmount") or 0) > 0
    except (httpx.HTTPError, ValueError):
        return False


def telegram_chat_id(token: str, wait_s: int = 120) -> str | None:
    """Espera a que el usuario escriba a su bot y devuelve su chat id."""
    end = time.time() + wait_s
    while time.time() < end:
        try:
            r = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15).json()
            for u in reversed(r.get("result", [])):
                chat = (u.get("message") or {}).get("chat")
                if chat:
                    return str(chat["id"])
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(3)
    return None


def balances(rpc: str, owner: str) -> tuple[float, float]:
    sol = httpx.post(rpc, json={"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [owner]},
                     timeout=15).json()["result"]["value"] / 1e9
    res = httpx.post(rpc, json={"jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
                                "params": [owner, {"mint": USDC}, {"encoding": "jsonParsed"}]}, timeout=15).json()
    usdc = sum(float(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
               for a in res["result"]["value"])
    return sol, usdc


def print_qr(text: str):
    try:
        import qrcode
        q = qrcode.QRCode(border=1)
        q.add_data(text)
        q.print_ascii(invert=True)
    except ImportError:
        pass


# --------------------------------------------------------------------------- asistente
def main():
    root = Path.cwd()
    env_f, cfg_f = root / ".env", root / "config.yaml"
    env: dict[str, str] = {}
    if env_f.exists():
        for ln in env_f.read_text().splitlines():
            if "=" in ln and not ln.lstrip().startswith("#"):
                k, v = ln.split("=", 1)
                env[k.strip()] = v.strip()
    print(f"\n{B}MemeBot · configuración{X}  (Enter = valor por defecto; las claves no se muestran al escribir)\n")

    # 1. wallet --------------------------------------------------------------
    from solders.keypair import Keypair

    print(f"{B}1. Wallet del bot{X}")
    kp = None
    if env.get("WALLET_PRIVATE_KEY"):
        try:
            kp = Keypair.from_base58_string(env["WALLET_PRIVATE_KEY"])
            ok(f"Ya hay una wallet configurada: {kp.pubkey()}")
            if not yes("¿Mantenerla?"):
                kp = None
        except ValueError:
            kp = None
    if kp is None:
        print("  [1] Crear una wallet NUEVA solo para el bot (recomendado)\n  [2] Usar una wallet de Phantom (pegar su clave privada)")
        if ask("Elige", "1") == "2":
            while kp is None:
                try:
                    kp = Keypair.from_base58_string(ask("Clave privada (base58)", secret=True))
                except ValueError:
                    bad("Clave no válida, prueba otra vez")
        else:
            kp = Keypair()
            print(f"\n  {Y}{B}GUARDA ESTA CLAVE PRIVADA AHORA{X} (papel o gestor de contraseñas). Con ella puedes importar")
            print("  la wallet en Phantom (Añadir cuenta → Importar clave privada) y ver el bot en tu móvil.")
            print(f"  Quien la tenga controla el dinero. No se volverá a mostrar.\n\n  {B}{kp}{X}\n")
            input("  Pulsa Enter cuando la hayas guardado… ")
        env["WALLET_PRIVATE_KEY"] = str(kp)
    address = str(kp.pubkey())
    ok(f"Dirección del bot: {B}{address}{X}")

    # 2. RPC -----------------------------------------------------------------
    print(f"\n{B}2. Conexión a Solana (Helius, gratis: dashboard.helius.dev → API Keys){X}")
    cur = env.get("SOLANA_RPC_URL", "")
    while True:
        v = ask("API key de Helius o URL completa del RPC", "" if "TU_KEY" in cur else cur, secret=False)
        if not v:
            warn("Sin RPC privado se usará el público, que bloquea peticiones: el bot comprará mucho menos.")
            v = "https://api.mainnet-beta.solana.com"
        url = v if v.startswith("http") else f"https://mainnet.helius-rpc.com/?api-key={v}"
        if check_rpc(url):
            ok("RPC funciona")
            env["SOLANA_RPC_URL"] = url
            break
        bad("Ese RPC no responde. Revisa la clave.")

    # 3. Jupiter ------------------------------------------------------------
    print(f"\n{B}3. Jupiter (gratis: portal.jup.ag → API Keys){X}")
    while True:
        v = ask("API key de Jupiter (Enter para dejarlo para luego)", secret=True) or env.get("JUPITER_API_KEY", "")
        if not v:
            warn("Sin clave de Jupiter solo funciona el modo paper. Puedes volver a ejecutar este asistente después.")
            break
        if check_jupiter(v):
            ok("Jupiter funciona")
            env["JUPITER_API_KEY"] = v
            break
        bad("Jupiter rechaza esa clave.")

    # 4. Telegram -----------------------------------------------------------
    print(f"\n{B}4. Avisos en el móvil por Telegram (opcional){X}")
    if yes("¿Quieres avisos por Telegram?", bool(env.get("TELEGRAM_BOT_TOKEN"))):
        print("  En Telegram abre @BotFather → /newbot → elige un nombre → copia el token que te da.")
        tok = ask("Token del bot", secret=True) or env.get("TELEGRAM_BOT_TOKEN", "")
        try:
            me = httpx.get(f"https://api.telegram.org/bot{tok}/getMe", timeout=15).json()
        except (httpx.HTTPError, ValueError):
            me = {}
        if me.get("ok"):
            name = me["result"]["username"]
            print(f"  Ahora abre https://t.me/{name} y envíale cualquier mensaje (por ejemplo «hola»). Esperando…")
            chat = telegram_chat_id(tok)
            if chat:
                env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"] = tok, chat
                httpx.post(f"https://api.telegram.org/bot{tok}/sendMessage", timeout=15,
                           json={"chat_id": chat, "text": "✅ MemeBot conectado. Aquí recibirás cada compra y venta."})
                ok("Telegram conectado: te ha llegado un mensaje de prueba")
            else:
                warn("No llegó ningún mensaje en 2 minutos; vuelve a ejecutar el asistente para reintentarlo.")
        else:
            bad("Token de Telegram no válido; se omite.")

    # 4b. Actualizaciones automáticas ----------------------------------------
    print(f"\n{B}Actualizaciones automáticas (recomendado){X}")
    print("  Con un token de GitHub de SOLO LECTURA, el bot se actualiza solo cuando haya mejoras.")
    print("  Créalo en github.com/settings/personal-access-tokens/new → Only select repositories → test")
    print("  → Permissions → Contents: Read-only → Generate token.")
    while True:
        tok = (ask("Token de GitHub (Enter para omitir)", secret=True) or env.get("GITHUB_TOKEN", "")).strip()
        if not tok:
            warn("Sin token: tendrás que actualizar a mano descargando el ZIP.")
            break
        if not tok.startswith(("github_pat_", "ghp_")) or len(tok) < 40:
            bad(f"Eso no parece un token de GitHub completo (empieza por «{tok[:6]}…», {len(tok)} caracteres). "
                f"Debe empezar por «github_pat_» y tener unos 90 caracteres: cópialo con el icono de copiar.")
            continue
        try:
            r = httpx.get("https://api.github.com/repos/danielcarbajo98/test/commits",
                          params={"sha": "claude/bold-johnson-evz8g1", "per_page": 1},
                          headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}, timeout=15)
            code = r.status_code
        except httpx.HTTPError:
            code = 0
        if code == 200:
            env["GITHUB_TOKEN"] = tok
            ok("Actualizaciones automáticas activadas")
            break
        reasons = {
            401: "token no válido, caducado o mal copiado. Regenera el token en GitHub y cópialo con el icono.",
            404: "el token es válido pero NO tiene acceso al repositorio «test». En GitHub, edita el token: "
                 "Repository access → Only select repositories → danielcarbajo98/test, y Permissions → "
                 "Contents → Read-only. Guarda con «Update» y vuelve a pegarlo.",
            403: "GitHub ha rechazado el acceso (permisos insuficientes o límite temporal). Revisa Contents: Read-only.",
            0: "no hay conexión con GitHub ahora mismo. Inténtalo de nuevo en un momento.",
        }
        bad(f"GitHub responde {code or 'sin conexión'}: {reasons.get(code, 'respuesta inesperada.')} "
            f"(Enter para omitir)")

    # 4c. Analista Claude ---------------------------------------------------
    print(f"\n{B}Analista Claude (opcional, de pago por uso){X}")
    print("  Claude revisa el mercado y las noticias cada hora y puede vetar compras o reducir el riesgo.")
    print("  Clave en console.anthropic.com → API Keys (necesita saldo en la cuenta). Coste orientativo en la guía.")
    while True:
        key = (ask("Clave de la API de Claude (Enter para omitir)", secret=True) or env.get("ANTHROPIC_API_KEY", "")).strip()
        if not key:
            warn("Sin clave: el bot funciona igual, sin el analista Claude.")
            break
        try:
            r = httpx.get("https://api.anthropic.com/v1/models/claude-opus-5-5", timeout=15,
                          headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
            code = r.status_code
        except httpx.HTTPError:
            code = 0
        if code == 200:
            env["ANTHROPIC_API_KEY"] = key
            ok("Analista Claude activado")
            break
        bad({401: "clave no válida o mal copiada (empieza por «sk-ant-»).",
             403: "la clave no tiene permiso para este modelo.",
             0: "sin conexión con Anthropic ahora mismo."}.get(code, f"respuesta inesperada ({code}).")
            + " Prueba otra vez o pulsa Enter.")

    # 5. Perfil y modo ------------------------------------------------------
    print(f"\n{B}5. Capital y modo{X}")
    while True:
        try:
            capital = float(ask("Capital del bot en USDC", "50").replace(",", ".").replace("$", "").replace("€", ""))
            if capital >= 20:
                break
            bad("Mínimo 20 USDC (con menos, las comisiones se lo comen).")
        except ValueError:
            bad("Escribe solo el número, por ejemplo 50")
    base = root / ("config.50eur.yaml" if capital <= 150 else "config.example.yaml")
    cfg = yaml.safe_load(base.read_text())
    if cfg_f.exists() and yes("Ya existe config.yaml. ¿Mantener tus ajustes y cambiar solo capital/modo?"):
        cfg = yaml.safe_load(cfg_f.read_text())
    cfg.setdefault("risk", {})["starting_equity_usd"] = capital
    live_ok = bool(env.get("JUPITER_API_KEY"))
    mode = "paper"
    if live_ok and yes("¿Empezar YA con dinero real? (recomendado: no, primero paper)", False):
        mode = "live"
    cfg["mode"] = mode
    old_capital = None
    if cfg_f.exists():
        try:
            old_capital = float((yaml.safe_load(cfg_f.read_text()) or {}).get("risk", {}).get("starting_equity_usd"))
        except (TypeError, ValueError):
            old_capital = None
    cfg_f.write_text("# Generado por bot/setup.py\n" + yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
    ok(f"config.yaml: capital {capital:g} USDC, modo {mode.upper()}")
    pf = root / "state" / "portfolio.json"
    if mode == "paper" and pf.exists() and old_capital is not None and abs(old_capital - capital) > 0.01:
        if yes(f"¿Reiniciar la simulación con {capital:g} $ en vez de {old_capital:g} $? (se guarda una copia)"):
            stamp = time.strftime("%Y%m%d-%H%M%S")
            for name in ("portfolio.json", "trades.csv", "equity.csv"):
                f = root / "state" / name
                if f.exists():
                    f.rename(f.with_name(f"{f.stem}.antes-{stamp}{f.suffix}"))
            ok("Simulación reiniciada con el nuevo capital")

    env.setdefault("DASHBOARD_TOKEN", secrets.token_urlsafe(24))
    if env_f.exists():
        shutil.copy(env_f, env_f.with_suffix(".bak"))
    env_f.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    try:
        os.chmod(env_f, 0o600)
    except OSError:
        pass                     # carpetas de Windows montadas en Docker no admiten permisos Unix
    ok(".env guardado (solo lo puede leer tu usuario)")

    # 6. Fondos ------------------------------------------------------------
    print(f"\n{B}6. Fondos{X}")
    try:
        sol, usdc = balances(env["SOLANA_RPC_URL"], address)
    except Exception:  # noqa: BLE001
        sol, usdc = 0.0, 0.0
    print(f"  Saldo actual: {usdc:.2f} USDC · {sol:.4f} SOL")
    if usdc < capital * 0.95 or sol < 0.05:
        print(f"\n  Envía a esta dirección, por la {B}red Solana{X}:\n\n    {B}{address}{X}\n")
        print_qr(address)
        print(f"  · {max(0.0, capital - usdc):.2f} USDC (el capital del bot)\n  · al menos 0,05 SOL (comisiones; ~5 € de SOL va sobrado)")
        print("  Desde Bit2Me/Kraken/Coinbase: Retirar → USDC → red SOLANA → pega la dirección. Prueba antes con poco.")
    else:
        ok("Fondos suficientes")
    print(f"\n{G}{B}Configuración completa.{X} Panel: token {env['DASHBOARD_TOKEN']}\n")


if __name__ == "__main__":
    sys.exit(main())
