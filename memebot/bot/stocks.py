"""Acciones: ALERTAS de compra y venta por Telegram (no opera solo: tú ejecutas en tu broker).

  python -m bot.stocks --state state --loop 15      (servicio `stocks` de docker-compose)

Estrategia swing (días a semanas), sobre velas diarias de Yahoo Finance (gratis, sin clave):
  COMPRA «Retroceso en tendencia»: precio > media de 200 días, media 50 > media 200 y RSI(14) < 35.
     Una acción fuerte que cae a corto plazo y suele retomar la subida.
  COMPRA «Ruptura»: cierre por encima del máximo de 55 días, volumen > 1,5x la media y precio > media 200.
  VENTA (de las alertas de compra abiertas), lo primero que ocurra:
     · stop: precio < entrada − 2 × ATR(14)   · trailing: precio < máximo − 3 × ATR
     · pierde la tendencia: cierre < media de 200 días (retrocesos) o de 50 días (rupturas)
     · sobrecompra: RSI(14) > 75
Cada alerta se sigue como una operación simulada para medir si las alertas funcionan (historial en el panel).
No es asesoramiento financiero.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import numpy as np

log = logging.getLogger("stocks")
YF = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
UA = {"User-Agent": "Mozilla/5.0 (compatible; MemeBot/1.0)"}
DEFAULT_WATCHLIST = [
    # EE. UU.
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "AVGO", "NFLX", "SPY", "QQQ",
    # España (IBEX 35)
    "SAN.MC", "BBVA.MC", "ITX.MC", "IBE.MC", "TEF.MC", "REP.MC", "CABK.MC", "AENA.MC",
    # Europa
    "ASML.AS", "SAP.DE", "MC.PA",
]


# --------------------------------------------------------------------------- indicadores
def sma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        c = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def rsi(x: np.ndarray, n: int = 14) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) <= n:
        return out
    d = np.diff(x)
    up, dn = np.maximum(d, 0), np.maximum(-d, 0)
    au, ad = up[:n].mean(), dn[:n].mean()
    for i in range(n, len(x)):
        if i > n:
            au = (au * (n - 1) + up[i - 1]) / n
            ad = (ad * (n - 1) + dn[i - 1]) / n
        out[i] = 100.0 if ad == 0 else 100 - 100 / (1 + au / ad)
    return out


def atr(h: np.ndarray, lo: np.ndarray, c: np.ndarray, n: int = 14) -> np.ndarray:
    pc = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - lo, np.maximum(abs(h - pc), abs(lo - pc)))
    return sma(tr, n)


# --------------------------------------------------------------------------- datos
@dataclass
class Bars:
    symbol: str
    name: str
    currency: str
    ts: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray  # noqa: E741
    c: np.ndarray
    v: np.ndarray
    price: float
    market_open: bool
    extra: dict = field(default_factory=dict)


def fetch(http: httpx.Client, sym: str) -> Bars | None:
    try:
        r = http.get(YF.format(sym=sym), params={"range": "2y", "interval": "1d"}, headers=UA)
        res = r.json()["chart"]["result"][0]
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as e:
        log.info("Sin datos de %s: %s", sym, e)
        return None
    m, q = res["meta"], res["indicators"]["quote"][0]
    raw = list(zip(res.get("timestamp", []), q["open"], q["high"], q["low"], q["close"], q["volume"]))
    price = m.get("regularMarketPrice")
    rows = []
    for i, (t, o, h, lo, c, v) in enumerate(raw):
        if None not in (o, h, lo, c):
            rows.append((t, o, h, lo, c, v or 0))
        elif i == len(raw) - 1 and price:          # vela de hoy incompleta: se construye con el precio actual
            prev = rows[-1][4] if rows else price
            o = o if o is not None else prev
            rows.append((t, o, max(x for x in (h, o, price) if x is not None),
                         min(x for x in (lo, o, price) if x is not None), price, v or 0))
    if len(rows) < 60:
        return None
    a = np.array(rows, dtype=float)
    price = float(price or a[-1, 4])
    if int(a[-1, 0]) // 86400 == int(m.get("regularMarketTime", a[-1, 0])) // 86400:
        a[-1, 4] = price                            # la vela del día sigue al precio actual
        a[-1, 2], a[-1, 3] = max(a[-1, 2], price), min(a[-1, 3], price)
    tp = (m.get("currentTradingPeriod") or {}).get("regular") or {}
    now = time.time()
    return Bars(sym, m.get("longName") or m.get("shortName") or sym, m.get("currency", ""), a[:, 0], a[:, 1],
                a[:, 2], a[:, 3], a[:, 4], np.nan_to_num(a[:, 5]), price,
                bool(tp.get("start", 0) <= now <= tp.get("end", 0)))


# --------------------------------------------------------------------------- señales
def analyze(b: Bars) -> dict:
    c = b.c
    s50, s200, r, a = sma(c, 50), sma(c, 200), rsi(c), atr(b.h, b.l, c)
    vavg = sma(b.v, 20)
    hi55 = np.max(b.h[-56:-1]) if len(c) > 56 else np.nan
    last = {"price": float(c[-1]), "sma50": float(s50[-1]), "sma200": float(s200[-1]), "rsi": float(r[-1]),
            "atr": float(a[-1]), "hi55": float(hi55), "vol_ratio": float(b.v[-1] / vavg[-1]) if vavg[-1] else 0.0,
            "chg_1d": float((c[-1] / c[-2] - 1) * 100), "chg_1m": float((c[-1] / c[-22] - 1) * 100) if len(c) > 22 else 0.0}
    up = np.isfinite(last["sma200"]) and last["price"] > last["sma200"] and last["sma50"] > last["sma200"]
    last["trend"] = "alcista" if up else ("bajista" if np.isfinite(last["sma200"]) and last["price"] < last["sma200"] else "lateral")
    checks_pull = [
        ("Precio sobre la media de 200 días", last["price"] > last["sma200"]),
        ("Media 50 sobre media 200 (tendencia sana)", last["sma50"] > last["sma200"]),
        (f"RSI bajo (< 35): {last['rsi']:.0f}", last["rsi"] < 35),
    ]
    checks_brk = [
        ("Precio sobre la media de 200 días", last["price"] > last["sma200"]),
        (f"Rompe el máximo de 55 días ({last['hi55']:.2f})", last["price"] > last["hi55"]),
        (f"Volumen > 1,5x la media ({last['vol_ratio']:.1f}x)", last["vol_ratio"] > 1.5),
    ]
    signal = None
    if all(ok for _, ok in checks_pull):
        signal = "Retroceso en tendencia"
    elif all(ok for _, ok in checks_brk):
        signal = "Ruptura"
    last["signal"] = signal
    last["checks"] = {"Retroceso en tendencia": checks_pull, "Ruptura": checks_brk}
    last["sma50_series"], last["sma200_series"] = s50, s200
    return last


def exit_reason(pos: dict, a: dict) -> str:
    p = a["price"]
    if p < pos["stop"]:
        return f"stop (−2 ATR) en {pos['stop']:.2f}"
    if p < pos["peak"] - 3 * pos["atr"]:
        return "trailing (−3 ATR desde el máximo)"
    # un «retroceso» compra precisamente bajo la media de 50: su tendencia se rompe al perder la de 200
    if pos.get("setup") == "Retroceso en tendencia":
        if p < a["sma200"]:
            return "pierde la media de 200 días (tendencia rota)"
    elif p < a["sma50"]:
        return "pierde la media de 50 días"
    if a["rsi"] > 75:
        return f"sobrecompra (RSI {a['rsi']:.0f})"
    return ""


# --------------------------------------------------------------------------- ciclo
def notify(text: str):
    log.info(text)
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if tok and chat:
        try:
            httpx.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                       json={"chat_id": chat, "text": text, "disable_web_page_preview": True}, timeout=10)
        except httpx.HTTPError:
            pass


def run_once(state_dir: Path, watchlist: list[str], http: httpx.Client | None = None,
             fetcher=fetch) -> dict:
    http = http or httpx.Client(timeout=20)
    f = state_dir / "stocks.json"
    st = json.loads(f.read_text()) if f.exists() else {"positions": {}, "alerts": [], "closed": []}
    charts = state_dir / "stock_charts"
    charts.mkdir(parents=True, exist_ok=True)
    rows, today = [], time.strftime("%Y-%m-%d")
    for sym in watchlist:
        b = fetcher(http, sym)
        if b is None:
            continue
        a = analyze(b)
        pos = st["positions"].get(sym)
        status = "vigilando"
        if pos:
            pos["peak"] = max(pos["peak"], a["price"])
            pos["price"] = a["price"]
            why = exit_reason(pos, a)
            if why:
                ret = (a["price"] / pos["entry"] - 1) * 100
                st["closed"].append({**pos, "exit": a["price"], "exit_ts": int(time.time()), "reason": why, "ret": ret})
                st["alerts"].append({"ts": int(time.time()), "symbol": sym, "side": "VENTA", "price": a["price"],
                                     "reason": why, "ret": ret})
                del st["positions"][sym]
                notify(f"🔴 VENDER {sym} ({b.name}) a ~{a['price']:.2f} {b.currency}\nMotivo: {why}\n"
                       f"Resultado de la alerta: {ret:+.1f}%")
                status = "venta"
            else:
                status = "en cartera"
        elif a["signal"] and st.get("last_buy", {}).get(sym) != today:
            stop = a["price"] - 2 * a["atr"]
            st["positions"][sym] = {"symbol": sym, "name": b.name, "currency": b.currency, "entry": a["price"],
                                    "entry_ts": int(time.time()), "stop": stop, "peak": a["price"], "atr": a["atr"],
                                    "setup": a["signal"], "price": a["price"]}
            st.setdefault("last_buy", {})[sym] = today
            st["alerts"].append({"ts": int(time.time()), "symbol": sym, "side": "COMPRA", "price": a["price"],
                                 "reason": a["signal"]})
            notify(f"🟢 COMPRAR {sym} ({b.name}) a ~{a['price']:.2f} {b.currency}\nSeñal: {a['signal']} · "
                   f"RSI {a['rsi']:.0f} · tendencia {a['trend']}\nStop sugerido: {stop:.2f} (−2 ATR)"
                   f"{'' if b.market_open else chr(10) + 'Mercado cerrado: precio del último cierre.'}\n"
                   f"Es una alerta, no una orden: decides tú en tu broker.")
            status = "compra"
        rows.append({"symbol": sym, "name": b.name, "currency": b.currency, "price": a["price"], "chg_1d": a["chg_1d"],
                     "chg_1m": a["chg_1m"], "rsi": a["rsi"], "trend": a["trend"], "signal": a["signal"],
                     "status": status, "market_open": b.market_open,
                     "checks": {k: [[t, bool(ok)] for t, ok in v] for k, v in a["checks"].items()}})
        n = 180
        chart = {"symbol": sym, "name": b.name, "currency": b.currency,
                 "candles": [[int(t), o, h, lo, c, v] for t, o, h, lo, c, v in
                             zip(b.ts[-n:], b.o[-n:], b.h[-n:], b.l[-n:], b.c[-n:], b.v[-n:])],
                 "sma50": [None if not np.isfinite(x) else float(x) for x in a["sma50_series"][-n:]],
                 "sma200": [None if not np.isfinite(x) else float(x) for x in a["sma200_series"][-n:]],
                 "alerts": [x for x in st["alerts"] if x["symbol"] == sym][-20:],
                 "position": st["positions"].get(sym)}
        (charts / f"{sym.replace('.', '_')}.json").write_text(json.dumps(chart))
    closed = st["closed"]
    rets = [x["ret"] for x in closed]
    st.update({"ts": int(time.time()), "watchlist": rows, "alerts": st["alerts"][-200:], "closed": closed[-200:],
               "track": {"n": len(rets), "win_rate": round(100 * np.mean(np.array(rets) > 0), 1) if rets else None,
                         "avg_ret": round(float(np.mean(rets)), 2) if rets else None}})
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    tmp.replace(f)
    return st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="state")
    ap.add_argument("--loop", type=float, default=0, help="repetir cada N minutos")
    ap.add_argument("--watchlist", default="", help="símbolos separados por comas (por defecto, la lista incluida)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s stocks: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    wl_env = os.getenv("STOCKS_WATCHLIST", "")
    wl = [s.strip().upper() for s in (a.watchlist or wl_env).split(",") if s.strip()] or DEFAULT_WATCHLIST
    from . import version
    while True:
        try:
            st = run_once(Path(a.state), wl)
            log.info("%d acciones analizadas, %d alertas abiertas", len(st["watchlist"]), len(st["positions"]))
        except Exception:  # noqa: BLE001
            log.exception("Fallo en el análisis de acciones")
        if not a.loop:
            break
        end = time.time() + a.loop * 60
        while time.time() < end:
            if version.changed():
                return
            time.sleep(15)


if __name__ == "__main__":
    main()
