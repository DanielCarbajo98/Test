"""Comandos de Telegram: controla y consulta el bot desde el móvil.

Solo obedece a TU chat (TELEGRAM_CHAT_ID); cualquier otro mensaje se ignora. Los comandos solo pueden
consultar, pausar, reanudar, cerrar posiciones o silenciar alertas: nunca cambian el riesgo ni activan dinero real.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

HELP = ("🤖 Comandos:\n"
        "/estado · resumen ahora mismo\n"
        "/posiciones · posiciones abiertas de cripto\n"
        "/cazador · cazador, copia y traders con habilidad\n"
        "/acciones · cartera simulada y estrategias de acciones\n"
        "/pausa · no abrir operaciones nuevas (las abiertas siguen gestionándose)\n"
        "/reanudar · volver a operar\n"
        "/cerrar_todo SI · cerrar todas las posiciones de cripto\n"
        "/silenciar_acciones · /activar_acciones · alertas de acciones")


def _read(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def _control(state: Path, **kv):
    f = state / "control.json"
    ctl = _read(f)
    ctl.update(kv, updated_at=int(time.time()))
    f.write_text(json.dumps(ctl))


def _dur(sec: float) -> str:
    m = int(max(0, sec) // 60)
    return f"{m // 1440} d {m % 1440 // 60} h" if m >= 1440 else (f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min")


def positions_text(state: Path) -> str:
    """Posiciones abiertas de cripto y de acciones, con su rendimiento y su beneficio o pérdida."""
    now = time.time()
    out = []
    pos = _read(state / "status.json").get("positions") or []
    tot = sum(p.get("pnl_usd", 0) for p in pos)
    out.append(f"🪙 CRIPTO · {len(pos)} abiertas · {'+' if tot >= 0 else '−'}${abs(tot):.2f}" if pos else "🪙 CRIPTO · sin posiciones abiertas")
    for p in sorted(pos, key=lambda x: -x.get("pnl_usd", 0)):
        pnl, pc = p.get("pnl_usd", 0), p.get("pnl_pct", 0)
        out.append(f"{'🟢' if pnl >= 0 else '🔴'} {p.get('symbol', '?')}: {pc:+.1f}% · {'+' if pnl >= 0 else '−'}${abs(pnl):.2f} "
                   f"(invertido ${p.get('cost', 0):.0f}, vale ${p.get('value', 0):.0f}) · {_dur(now - p.get('opened_at', now))}")
    st = _read(state / "stocks.json").get("positions") or {}
    rows = []
    for sym, p in st.items():
        amt = p.get("amount") or 100.0                         # alertas antiguas: se abrieron con 100 €
        r = 0.0 if p.get("pending_open") else (p.get("price", p["entry"]) / p["entry"] - 1) * 100
        rows.append((amt * r / 100 - (p.get("fees_eur") or 0), r, sym, p, amt))
    tot_s = sum(x[0] for x in rows)
    inv_s = sum(x[4] for x in rows)
    out.append("")
    out.append(f"📈 ACCIONES · {len(rows)} abiertas · invertido {inv_s:,.0f} € · {'+' if tot_s >= 0 else '−'}{abs(tot_s):.2f} € "
               f"({100 * tot_s / inv_s:+.1f}%)".replace(",", ".") if rows else "📈 ACCIONES · sin posiciones abiertas")
    out.append("(beneficio = lo que darían hoy al vender, ya descontadas comisiones de compra y venta)" if rows else "")
    for pnl, r, sym, p, amt in sorted(rows, key=lambda x: -x[0]):
        tag = " · esperando a poder comprar" if p.get("pending_open") else (" · pre-market" if p.get("price_session") == "pre" else
                                                                         " · after-hours" if p.get("price_session") == "post" else "")
        fees = p.get("fees_eur") or 0
        out.append(f"{'🟢' if pnl >= 0 else '🔴'} {sym}: invertido {amt:.0f} € → vale {amt + pnl:.2f} € · "
                   f"{'+' if pnl >= 0 else '−'}{abs(pnl):.2f} € ({100 * pnl / amt:+.1f}%)"
                   f"{f' · precio {r:+.1f}%, comisiones {fees:.2f} €' if fees >= 0.01 else ''}{tag}")
    return "\n".join(out)[:4000]


def answer(state: Path, text: str) -> str | None:
    cmd = (text or "").strip().split()
    if not cmd or not cmd[0].startswith("/"):
        return None
    c = cmd[0].split("@")[0].lower()
    if c in ("/start", "/ayuda", "/help"):
        return HELP
    if c == "/estado":
        from .watchdog import daily_summary
        return daily_summary(state).replace("RESUMEN DEL DÍA", "ESTADO AHORA")
    if c == "/posiciones":
        return positions_text(state)
    if c == "/cazador":
        h = _read(state / "hunter.json").get("summary") or {}
        st, cp = _read(state / "moonshot.json"), _read(state / "copy.json")
        tr = _read(state / "traders.json").get("tally") or {}
        out = ["🎯 Cazador y copia (simulado):"]
        for k in ("cazador", "copia"):
            s = h.get(k) or {}
            out.append(f"· {k}: {s.get('pnl', 0):+.2f} $ · {s.get('open', 0)} abiertas · {s.get('closed', 0)} cerradas")
        out.append(f"Estudio: {st.get('note', 'aún sin datos')}")
        out.append(f"Copia: {cp.get('note', 'aún sin datos')}")
        if tr:
            out.append("Traders: " + " · ".join(f"{k} {v}" for k, v in tr.items()))
        return "\n".join(out)
    if c == "/acciones":
        k, b = _read(state / "stocks.json"), _read(state / "stocks_brain.json")
        p = k.get("paper") or {}
        act = [n for n, v in (b.get("setups") or {}).items() if v.get("active")]
        return (f"📈 Acciones: cartera simulada {p.get('total', 0):+.2f} € · {p.get('open', 0)} abiertas · "
                f"{p.get('closed', 0)} cerradas\nEstrategias activas: {', '.join(act) or 'ninguna'}\n"
                + "\n".join("· " + x for x in (b.get("lessons") or [])[:6]))
    if c == "/pausa":
        _control(state, paused=True)
        return "⏸️ En pausa: no abrirá operaciones nuevas. Las abiertas siguen con sus stops. /reanudar para volver."
    if c == "/reanudar":
        _control(state, paused=False)
        return "▶️ Reanudado."
    if c == "/cerrar_todo":
        if len(cmd) > 1 and cmd[1].upper() in ("SI", "SÍ"):
            _control(state, close_all=True)
            return "🛑 Cerrando todas las posiciones de cripto en el próximo ciclo."
        return "Para confirmar escribe: /cerrar_todo SI"
    if c in ("/silenciar_acciones", "/activar_acciones"):
        _control(state, stocks_alerts=c == "/activar_acciones")
        return "🔕 Alertas de acciones silenciadas." if c == "/silenciar_acciones" else "🔔 Alertas de acciones activadas."
    return "No conozco ese comando. " + HELP


def poll(state: Path, memo: dict, http: httpx.Client | None = None) -> int:
    """Lee mensajes nuevos y responde. Devuelve cuántos comandos atendió."""
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return 0
    http = http or httpx.Client(timeout=15)
    off = memo.get("tg_offset")
    try:
        r = http.get(f"https://api.telegram.org/bot{tok}/getUpdates",
                     params={"timeout": 0, **({"offset": off} if off else {})})
        ups = r.json().get("result") or []
    except (httpx.HTTPError, ValueError):
        return 0
    if off is None:                                   # al arrancar: no responder a mensajes antiguos
        memo["tg_offset"] = (ups[-1]["update_id"] + 1) if ups else 0
        if ups:
            return 0
    n = 0
    for u in ups:
        memo["tg_offset"] = u["update_id"] + 1
        m = u.get("message") or {}
        if str((m.get("chat") or {}).get("id")) != str(chat):
            continue                                  # solo tu chat
        reply = answer(state, m.get("text", ""))
        if reply:
            try:
                http.post(f"https://api.telegram.org/bot{tok}/sendMessage", json={"chat_id": chat, "text": reply[:4000]})
            except httpx.HTTPError:
                pass
            n += 1
    return n
