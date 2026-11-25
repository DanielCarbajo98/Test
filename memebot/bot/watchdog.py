"""Vigilante y resumen diario (corre dentro del servicio `updater`, que siempre está en marcha).

* Cada minuto comprueba que el bot, las acciones y el cerebro dan señales de vida (sus archivos de estado
  se actualizan). Si uno se para, avisa por Telegram una vez; cuando vuelve, avisa también.
* Cada día a las 21:00 (hora de España) envía un resumen: saldo, operaciones del día, posiciones,
  aprendizaje y cartera simulada de acciones.
Si el ordenador entero está apagado, nadie puede avisar: para eso está el servidor 24/7.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

# servicio -> (archivo, campo de fecha, minutos máximos sin actualizar)
CHECKS = {
    "bot de cripto": ("status.json", "ts", 10),
    "análisis de acciones": ("stocks.json", "ts", 120),
    "cerebro de cripto": ("brain.json", "updated_at", 120),
}
SUMMARY_HOUR = 21


def madrid_now(now: float | None = None) -> datetime:
    """Hora de España sin depender de tzdata: CET (+1) / CEST (+2, último domingo de marzo a último de octubre)."""
    utc = datetime.fromtimestamp(now or time.time(), timezone.utc)

    def last_sunday(year: int, month: int) -> datetime:
        d = datetime(year, month + 1, 1, tzinfo=timezone.utc) - timedelta(days=1) if month < 12 else \
            datetime(year, 12, 31, tzinfo=timezone.utc)
        return d - timedelta(days=(d.weekday() + 1) % 7)

    start = last_sunday(utc.year, 3).replace(hour=1)
    end = last_sunday(utc.year, 10).replace(hour=1)
    return utc + timedelta(hours=2 if start <= utc < end else 1)


def _read(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def notify(text: str):
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if tok and chat:
        try:
            httpx.post(f"https://api.telegram.org/bot{tok}/sendMessage", json={"chat_id": chat, "text": text}, timeout=10)
        except httpx.HTTPError:
            pass


def check_health(state: Path, memo: dict, now: float | None = None, send=notify) -> dict:
    """Avisa de servicios parados (una vez) y de su recuperación. `memo` guarda qué está caído."""
    now = now or time.time()
    down = memo.setdefault("down", {})
    for name, (fname, field, max_min) in CHECKS.items():
        f = state / fname
        if not f.exists():
            continue                                  # aún no ha arrancado nunca
        ts = _read(f).get(field) or 0
        age = (now - ts) / 60 if ts else 1e9
        if age > max_min and name not in down:
            down[name] = now
            send(f"⚠️ VIGILANTE · El {name} no da señales desde hace {age:.0f} min. "
                 f"Comprueba que el servidor (o el PC) está encendido y Docker funcionando.")
        elif age <= max_min and name in down:
            mins = (now - down.pop(name)) / 60
            send(f"✅ VIGILANTE · El {name} vuelve a funcionar (estuvo parado unos {mins:.0f} min).")
    return memo


def daily_summary(state: Path, now: float | None = None) -> str:
    st, brain, stocks = _read(state / "status.json"), _read(state / "brain.json"), _read(state / "stocks.json")
    sb = _read(state / "stocks_brain.json")
    day = madrid_now(now).strftime("%Y-%m-%d")
    lines = [f"📊 RESUMEN DEL DÍA · {madrid_now(now).strftime('%d/%m')}"]
    if st:
        eq, start = st.get("equity", 0), st.get("start_equity") or 0
        tot = (eq / start - 1) * 100 if start else 0
        lines.append(f"🪙 CRIPTO ({'simulado' if st.get('mode') == 'paper' else 'REAL'}): saldo ${eq:,.2f} "
                     f"({tot:+.1f}% desde el inicio) · hoy {st.get('daily_pnl', 0):+.2f} $")
        lines.append(f"   Posiciones abiertas: {len(st.get('positions', []))} · estrategia: {st.get('strategy', '—')}")
    f = state / "trades.csv"
    if f.exists():
        from .history import positions
        today = [d for d in positions(f) if madrid_now(datetime.fromisoformat(d["closed"]).timestamp())
                 .strftime("%Y-%m-%d") == day]
        if today:
            w = [d for d in today if d["ret"] > 0]
            pnl = sum(d["got"] - d["cost"] for d in today)
            lines.append(f"   Cerradas hoy: {len(today)} (✅ {len(w)} · ❌ {len(today) - len(w)}) · resultado {pnl:+.2f} $")
        else:
            lines.append("   Hoy no se cerró ninguna operación.")
    if brain:
        lines.append(f"🧠 Cerebro: {brain.get('status_text', '—')}")
    if stocks:
        p = stocks.get("paper") or {}
        act = [k for k, v in (sb.get("setups") or {}).items() if v.get("active")]
        lines.append(f"📈 ACCIONES: {len(stocks.get('watchlist', []))} analizadas · estrategias activas: "
                     f"{', '.join(act) if act else 'ninguna (sin ventaja con tus comisiones)'}")
        if p:
            lines.append(f"   Cartera simulada: {p.get('total', 0):+.2f} € · abiertas {p.get('open', 0)} · cerradas "
                         f"{p.get('closed', 0)}")
    lines += market_radar(state)
    up = _read(state / "update.json")
    if up.get("current"):
        lines.append(f"🔄 Versión {up['current'][:7]} · {up.get('state', '')}")
    return "\n".join(lines)


def market_radar(state: Path, http: httpx.Client | None = None) -> list[str]:
    """Foto del mercado: miedo/codicia, BTC/ETH/SOL, bolsa y titulares (solo informativo)."""
    ctx, stocks = _read(state / "context.json"), _read(state / "stocks.json")
    out = ["🌍 RADAR DE MERCADO"]
    if ctx.get("fng") is not None:
        out.append(f"   Miedo y codicia: {ctx['fng']} ({ctx.get('fng_text', '')})")
    try:
        r = (http or httpx.Client(timeout=10)).get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin,ethereum,solana", "vs_currencies": "usd", "include_24hr_change": "true"})
        d = r.json() if r.status_code == 200 else {}
        parts = [f"{n} {d[k]['usd']:,.0f} $ ({d[k].get('usd_24h_change', 0):+.1f}%)"
                 for k, n in (("bitcoin", "BTC"), ("ethereum", "ETH"), ("solana", "SOL")) if k in d]
        if parts:
            out.append("   " + " · ".join(parts))
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        if ctx.get("sol_price"):
            out.append(f"   SOL {ctx['sol_price']:,.2f} $ ({(ctx.get('sol_h24') or 0):+.1f}% en 24 h)")
    if stocks.get("market_up") is not None:
        out.append("   Bolsa (S&P 500): " + ("tendencia alcista, sobre su media de 200 días" if stocks["market_up"]
                                             else "bajo su media de 200 días: prudencia"))
    heads = [h for h in ctx.get("headlines") or [] if abs(h.get("score", 0)) >= 0.3][:2]
    for h in heads:
        out.append(f"   📰 {h.get('title', '')[:110]}")
    return out if len(out) > 1 else []


def maybe_daily_summary(state: Path, memo: dict, now: float | None = None, send=notify) -> bool:
    m = madrid_now(now)
    day = m.strftime("%Y-%m-%d")
    if m.hour >= SUMMARY_HOUR and memo.get("summary_day") != day:
        memo["summary_day"] = day
        send(daily_summary(state, now))
        return True
    return False
