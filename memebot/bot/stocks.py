"""Acciones: ALERTAS de compra y venta por Telegram (no opera solo: tú ejecutas en tu broker).

  python -m bot.stocks --state state --loop 15      (servicio `stocks` de docker-compose)

Estrategia swing (días a semanas), sobre velas diarias de Yahoo Finance (gratis, sin clave):
  COMPRA «Retroceso en tendencia»: precio > media de 200 días, media 50 > media 200 y RSI(14) < 35.
     Una acción fuerte que cae a corto plazo y suele retomar la subida.
  COMPRA «Ruptura»: cierre por encima del máximo de 55 días, volumen > 1,5x la media y precio > media 200.
  VENTA (de las alertas de compra abiertas), lo primero que ocurra:
     · stop: precio < entrada − 2 × ATR(14)   · trailing: precio < máximo − 3 × ATR
     · pierde la tendencia: cierre < media de 200 días (retrocesos) o de 50 días (rupturas)
     · sobrecompra extrema: RSI(14) > 80
  La ruptura NO se da con RSI > 75 ni tras un salto de más del 15 % en un día (perseguir el precio).
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
IBEX35 = ["ACS.MC", "ACX.MC", "AENA.MC", "AMS.MC", "ANA.MC", "ANE.MC", "BBVA.MC", "BKT.MC", "CABK.MC", "CLNX.MC",
          "COL.MC", "ELE.MC", "ENG.MC", "FDR.MC", "FER.MC", "GRF.MC", "IAG.MC", "IBE.MC", "IDR.MC", "ITX.MC",
          "LOG.MC", "MAP.MC", "MRL.MC", "MTS.MC", "NTGY.MC", "PUIG.MC", "RED.MC", "REP.MC", "ROVI.MC", "SAB.MC",
          "SAN.MC", "SCYR.MC", "SLR.MC", "TEF.MC", "UNI.MC"]
EUROSTOXX50 = ["ADS.DE", "ADYEN.AS", "AD.AS", "AI.PA", "AIR.PA", "ALV.DE", "ABI.BR", "ASML.AS", "CS.PA", "BAS.DE",
               "BAYN.DE", "BMW.DE", "BNP.PA", "BN.PA", "DB1.DE", "DHL.DE", "DTE.DE", "ENEL.MI", "ENI.MI", "EL.PA",
               "RACE.MI", "IFX.DE", "INGA.AS", "ISP.MI", "KER.PA", "OR.PA", "MC.PA", "MBG.DE", "MUV2.DE", "NOKIA.HE",
               "RI.PA", "PRX.AS", "SAF.PA", "SGO.PA", "SAN.PA", "SAP.DE", "SU.PA", "SIE.DE", "STLAM.MI", "TTE.PA",
               "UCG.MI", "DG.PA", "VOW3.DE", "WKL.AS", "RMS.PA"]
US_CORE = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "AMD", "NFLX", "JPM", "V", "MA", "COST",
           "LLY", "UNH", "XOM", "WMT", "ORCL", "CRM", "ADBE", "INTC", "QCOM", "KO", "PEP", "DIS", "BA", "PLTR",
           "SPY", "QQQ"]
DEFAULT_WATCHLIST = US_CORE + IBEX35 + [s for s in EUROSTOXX50 if s not in IBEX35]
# listas dinámicas de Yahoo (se renuevan cada pocas horas)
SCREENERS = ["most_actives", "day_gainers", "day_losers", "undervalued_large_caps", "growth_technology_stocks",
             "undervalued_growth_stocks"]


def screener_symbols(http: httpx.Client, count: int = 100) -> list[str]:
    out: list[str] = []
    for scr in SCREENERS:
        try:
            r = http.get("https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved",
                         params={"scrIds": scr, "count": count}, headers=UA)
            quotes = r.json()["finance"]["result"][0]["quotes"]
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
            continue
        for q in quotes:
            sym = q.get("symbol")
            price = q.get("regularMarketPrice") or 0
            # fuera: chicharros (< 2 $), que son puro ruido y muy manipulables
            if sym and sym not in out and price >= 2 and q.get("quoteType", "EQUITY") in ("EQUITY", "ETF"):
                out.append(sym)
    return out


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
        r = http.get(YF.format(sym=sym), params={"range": "5y", "interval": "1d"}, headers=UA)
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


# --------------------------------------------------------------------------- divisas (precios en euros)
_FX: dict[str, tuple[float, float]] = {}


def eur_rate(http: httpx.Client, cur: str) -> float | None:
    """Euros por 1 unidad de `cur` (USD, GBP, GBp, CHF...). Caché de 30 min."""
    if not cur or cur.upper() == "EUR":
        return 1.0
    pence = cur == "GBp" or cur == "GBX"
    code = "GBP" if pence else cur.upper()
    hit = _FX.get(code)
    if hit and time.time() - hit[1] < 1800:
        rate = hit[0]
    else:
        try:
            r = http.get(YF.format(sym=f"{code}EUR=X"), params={"range": "5d", "interval": "1d"}, headers=UA)
            rate = float(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])
            _FX[code] = (rate, time.time())
        except Exception:  # noqa: BLE001 — sin tipo de cambio la alerta sale igual, sin euros
            return None
    return rate / 100 if pence else rate


def fmt_eur(v: float | None) -> str:
    return "—" if v is None else f"{v:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


# --------------------------------------------------------------------------- señales
def analyze(b: Bars, learned: dict | None = None) -> dict:
    """`learned` = {setup: (params, activa, info)} del cerebro de acciones (stocks_brain)."""
    from .stocks_brain import DEFAULT_PARAMS
    learned = learned or {}
    pp = (learned.get("Retroceso en tendencia") or (DEFAULT_PARAMS["Retroceso en tendencia"], True, {}))[0]
    bp = (learned.get("Ruptura") or (DEFAULT_PARAMS["Ruptura"], True, {}))[0]
    mp = {**DEFAULT_PARAMS["Momentum"], **(learned.get("Momentum") or (DEFAULT_PARAMS["Momentum"], True, {}))[0]}
    c = b.c
    s50, s200, r, a = sma(c, 50), sma(c, 200), rsi(c), atr(b.h, b.l, c)
    vavg = sma(b.v, 20)
    hi55 = np.max(b.h[-56:-1]) if len(c) > 56 else np.nan
    last = {"price": float(c[-1]), "sma50": float(s50[-1]), "sma200": float(s200[-1]), "rsi": float(r[-1]),
            "atr": float(a[-1]), "hi55": float(hi55), "vol_ratio": float(b.v[-1] / vavg[-1]) if vavg[-1] else 0.0,
            "chg_1d": float((c[-1] / c[-2] - 1) * 100), "chg_1m": float((c[-1] / c[-22] - 1) * 100) if len(c) > 22 else 0.0,
            "mom6m": float((c[-22] / c[-127] - 1) * 100) if len(c) > 127 else float("nan")}
    up = np.isfinite(last["sma200"]) and last["price"] > last["sma200"] and last["sma50"] > last["sma200"]
    last["trend"] = "alcista" if up else ("bajista" if np.isfinite(last["sma200"]) and last["price"] < last["sma200"] else "lateral")
    checks_pull = [
        ("Precio sobre la media de 200 días", last["price"] > last["sma200"]),
        ("Media 50 sobre media 200 (tendencia sana)", last["sma50"] > last["sma200"]),
        (f"RSI bajo (< {pp['rsi_max']}): {last['rsi']:.0f}", last["rsi"] < pp["rsi_max"]),
    ]
    checks_brk = [
        ("Precio sobre la media de 200 días", last["price"] > last["sma200"]),
        (f"Rompe el máximo de 55 días ({last['hi55']:.2f})", last["price"] > last["hi55"]),
        (f"Volumen > 1,5x la media ({last['vol_ratio']:.1f}x)", last["vol_ratio"] > 1.5),
        (f"Sin sobrecompra extrema (RSI ≤ {bp['rsi_cap']}): {last['rsi']:.0f}", last["rsi"] <= bp["rsi_cap"]),
        (f"Sin salto brusco de un día (≤ 15 %): {last['chg_1d']:+.1f}%", abs(last["chg_1d"]) <= 15),
    ]
    mom_ok = np.isfinite(last["mom6m"])
    checks_mom = [
        ("Precio sobre la media de 200 días y media 50 sobre la de 200", up),
        (f"Subida de 6 meses (sin el último) ≥ {mp['mom_min']} %: {last['mom6m']:+.0f} %" if mom_ok else
         "Subida de 6 meses: sin histórico suficiente", mom_ok and last["mom6m"] >= mp["mom_min"]),
        (f"Último mes sin estirarse (entre −10 % y +{mp['ret21_max']} %): {last['chg_1m']:+.1f} %",
         -10 <= last["chg_1m"] <= mp["ret21_max"]),
    ]
    setups = (("Retroceso en tendencia", checks_pull, pp), ("Ruptura", checks_brk, bp), ("Momentum", checks_mom, mp))
    active = {k: (learned.get(k) or (None, True, {}))[1] for k, _, _ in setups}
    signal, blocked = None, None
    for name, checks, _ in setups:
        if all(ok for _, ok in checks):
            if active[name]:
                signal = name
                break
            blocked = name
    last["signal"] = signal
    last["blocked_signal"] = blocked              # cumplía, pero esa estrategia está suspendida
    last["params"] = {k: v for k, _, v in setups}
    if signal:
        sp = last["params"][signal]
        last["plan"] = {"stop": last["price"] - sp["stop_atr"] * last["atr"],
                        "tp": last["price"] + sp["tp_atr"] * last["atr"], "max_days": sp["max_days"],
                        "stop_atr": sp["stop_atr"], "tp_atr": sp["tp_atr"],
                        "expected": ((learned.get(signal) or (None, None, {}))[2] or {}).get("out_of_sample")}
    last["checks"] = {k: ch for k, ch, _ in setups}
    last["reasoning"] = reasoning(last)
    last["sma50_series"], last["sma200_series"] = s50, s200
    return last


def _pct(a: float, b: float) -> float:
    return (a / b - 1) * 100 if b and np.isfinite(b) else float("nan")


def reasoning(a: dict) -> list[str]:
    """Explicación en lenguaje llano de la situación técnica de la acción (sin promesas)."""
    out = []
    p, s50, s200, r = a["price"], a["sma50"], a["sma200"], a["rsi"]
    d200, d50 = _pct(p, s200), _pct(p, s50)
    if np.isfinite(d200):
        if a["trend"] == "alcista":
            out.append(f"Tendencia de fondo ALCISTA: cotiza un {d200:+.1f}% respecto a su media de 200 días y la media de 50 "
                       f"está por encima de la de 200.")
        elif a["trend"] == "bajista":
            out.append(f"Tendencia de fondo BAJISTA: cotiza un {d200:+.1f}% respecto a su media de 200 días. "
                       f"Las compras en tendencia bajista tienen menos probabilidad de salir bien.")
        else:
            out.append(f"Tendencia LATERAL/indecisa: {d200:+.1f}% respecto a la media de 200 días, pero las medias no "
                       f"confirman una dirección clara.")
    if np.isfinite(d50):
        out.append(f"A corto plazo está un {d50:+.1f}% respecto a su media de 50 días "
                   f"({'por encima: fuerza reciente' if d50 > 0 else 'por debajo: debilidad o retroceso reciente'}).")
    if np.isfinite(r):
        zona = ("SOBRECOMPRADA (subió mucho y rápido; riesgo de corrección)" if r > 70 else
                "SOBREVENDIDA (cayó mucho; posible rebote)" if r < 30 else
                "con algo de debilidad" if r < 45 else "con algo de fuerza" if r > 55 else "neutral")
        out.append(f"RSI {r:.0f}: {zona}.")
    if a["atr"] and p:
        out.append(f"Volatilidad típica diaria (ATR): {100 * a['atr'] / p:.1f}% del precio.")
    out.append(f"En el último mes: {a['chg_1m']:+.1f}%. Hoy: {a['chg_1d']:+.1f}%.")
    if a.get("signal"):
        pl = a["plan"]
        out.append(f"→ SEÑAL DE COMPRA «{a['signal']}». Stop loss {pl['stop']:.2f} ({pl['stop_atr']} ATR), "
                   f"take profit {pl['tp']:.2f} ({pl['tp_atr']} ATR), plazo máximo {pl['max_days']} días.")
    elif a.get("blocked_signal"):
        out.append(f"→ Cumple «{a['blocked_signal']}», pero esa estrategia está SUSPENDIDA por el cerebro "
                   f"(no ha demostrado ventaja o falla en real). No se envía alerta.")
    else:
        rmax = (a.get("params") or {}).get("Retroceso en tendencia", {}).get("rsi_max", 35)
        miss = []
        if a["trend"] == "alcista" and np.isfinite(r) and r >= rmax:
            miss.append(f"para «Retroceso en tendencia» le falta que el RSI baje de {rmax} (ahora {r:.0f})")
        if p <= a["hi55"] and np.isfinite(a["hi55"]):
            miss.append(f"para «Ruptura» tendría que superar {a['hi55']:.2f} ({_pct(a['hi55'], p):+.1f}%) con volumen alto")
        if a["trend"] != "alcista":
            miss.append("ambas estrategias exigen que el precio esté por encima de su media de 200 días")
        out.append("→ Sin señal ahora: " + ("; ".join(miss) if miss else "no cumple las condiciones") + ".")
    return out


def exit_reason(pos: dict, a: dict, now: float | None = None) -> str:
    """Mismas salidas que simula el cerebro de acciones: stop, objetivo, tendencia rota y plazo."""
    p = a["price"]
    now = now or time.time()
    if p <= pos["stop"]:
        return f"STOP LOSS en {pos['stop']:.2f}"
    if pos.get("tp") and p >= pos["tp"]:
        return f"TAKE PROFIT alcanzado ({pos['tp']:.2f})"
    # un «retroceso» compra precisamente bajo la media de 50: su tendencia se rompe al perder la de 200
    if pos.get("setup") in ("Retroceso en tendencia", "Momentum"):
        if p < a["sma200"]:
            return "pierde la media de 200 días (tendencia rota)"
    elif p < a["sma50"]:
        return "pierde la media de 50 días (tendencia rota)"
    days = pos.get("max_days", 30)
    if now - pos.get("entry_ts", now) > days * 86400 * 1.45:     # días hábiles -> naturales
        return f"plazo máximo cumplido ({days} días de mercado)"
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


PAPER_EUR = float(os.getenv("STOCKS_PAPER_EUR", "100"))      # € simulados por cada alerta de compra
BROKER_FEE_EUR = float(os.getenv("STOCKS_FEE_EUR", "1"))      # (antiguo) comisión fija si no se sabe el valor


def trade_fees(sym: str, amount: float = PAPER_EUR) -> float:
    """€ de comisiones, cambio de divisa e impuestos de compra + venta con tu broker (bot/brokers.py)."""
    from . import brokers
    return round(brokers.fees_eur(sym, amount), 2)


def paper_pnl(ret_pct: float, amount: float = PAPER_EUR, fee: float | None = None, fees_total: float | None = None) -> float:
    """€ ganados o perdidos invirtiendo `amount` en la alerta, descontando los costes de compra y venta."""
    costs = fees_total if fees_total is not None else 2 * (BROKER_FEE_EUR if fee is None else fee)
    return amount * ret_pct / 100 - costs


def paper_summary(st: dict) -> dict:
    opened = list((st.get("positions") or {}).values())
    closed = st.get("closed") or []
    open_pnl = sum(paper_pnl((p.get("price", p["entry"]) / p["entry"] - 1) * 100, p.get("amount", PAPER_EUR),
                             fees_total=p.get("fees_eur")) for p in opened)
    realized = [c.get("pnl_eur", paper_pnl(c["ret"], c.get("amount", PAPER_EUR))) for c in closed if c.get("ret") is not None]
    wins = [x for x in realized if x > 0]
    from . import brokers
    bk = brokers.BROKERS[brokers.broker_key()]
    return {"per_alert": PAPER_EUR, "fee": BROKER_FEE_EUR, "broker": bk["name"], "broker_note": bk["note"],
            "open": len(opened),
            "invested_open": round(sum(p.get("amount", PAPER_EUR) for p in opened), 2),
            "open_pnl": round(open_pnl, 2), "realized": round(sum(realized), 2),
            "total": round(open_pnl + sum(realized), 2), "closed": len(realized),
            "win_rate": round(100 * len(wins) / len(realized), 1) if realized else None,
            "best": round(max(realized), 2) if realized else None, "worst": round(min(realized), 2) if realized else None}


def run_once(state_dir: Path, watchlist: list[str], http: httpx.Client | None = None,
             fetcher=fetch, max_per_run: int = 0, pause: float = 0.0, refresh_minutes: float = 60) -> dict:
    """Analiza las acciones. Con max_per_run > 0 va por turnos: primero las alertas abiertas y luego
    las que llevan más tiempo sin revisarse (todas se revisan al menos cada `refresh_minutes`)."""
    http = http or httpx.Client(timeout=20)
    f = state_dir / "stocks.json"
    st = json.loads(f.read_text()) if f.exists() else {"positions": {}, "alerts": [], "closed": []}
    charts = state_dir / "stock_charts"
    charts.mkdir(parents=True, exist_ok=True)
    today = time.strftime("%Y-%m-%d")
    prev = {r["symbol"]: r for r in st.get("watchlist", [])}
    universe = list(dict.fromkeys(list(st.get("positions", {})) + watchlist))
    if max_per_run:
        now = time.time()
        due = sorted(universe, key=lambda x: (x not in st.get("positions", {}), prev.get(x, {}).get("checked", 0)))
        todo = [x for x in due if x in st.get("positions", {}) or now - prev.get(x, {}).get("checked", 0) >= refresh_minutes * 60]
        todo = todo[:max_per_run]
    else:
        todo = universe
    from .stocks_brain import REGIME_SYMBOL, SETUPS, params_for
    learned = {k: params_for(state_dir, k) for k in SETUPS}
    market_up = None
    try:                                         # régimen de mercado: S&P 500 sobre su media de 200 días
        rb = json.loads((state_dir / "stock_bars" / f"{REGIME_SYMBOL}.json").read_text())
        rc = np.asarray(rb["c"], float)
        market_up = bool(rc[-1] > sma(rc, 200)[-1])
    except (OSError, ValueError, KeyError, IndexError):
        pass
    for k, (pp, act, info) in list(learned.items()):
        if pp.get("regime") and market_up is False:
            learned[k] = (pp, False, info)       # su regla exige mercado alcista y ahora no lo está
    bars_dir = state_dir / "stock_bars"
    bars_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    pending: list[tuple] = []
    for sym in todo:
        if pause:
            time.sleep(pause)
        b = fetcher(http, sym)
        if b is None:
            continue
        try:                                   # histórico para que el cerebro de acciones aprenda
            k = 1300
            (bars_dir / f"{sym.replace('.', '_')}.json").write_text(json.dumps(
                {"ts": b.ts[-k:].tolist(), "o": b.o[-k:].tolist(), "h": b.h[-k:].tolist(), "l": b.l[-k:].tolist(),
                 "c": b.c[-k:].tolist(), "v": b.v[-k:].tolist()}))
        except OSError:
            pass
        a = analyze(b, learned)
        if a["signal"]:                        # el cerebro solo activa las bolsas donde gana tras costes
            from .brokers import region
            regs = ((learned.get(a["signal"]) or (None, None, {}))[2] or {}).get("regions")
            if regs is not None and region(sym) not in regs:
                a["blocked_signal"], a["signal"] = a["signal"], None
                a.pop("plan", None)
        pos = st["positions"].get(sym)
        status = "vigilando"
        if pos:
            pos["peak"] = max(pos["peak"], a["price"])
            pos["price"] = a["price"]
            why = exit_reason(pos, a)
            if why:
                ex = a["price"]
                if why.startswith("STOP"):
                    ex = min(ex, pos["stop"]) if ex > pos["stop"] else ex
                ret = (ex / pos["entry"] - 1) * 100
                pnl = paper_pnl(ret, pos.get("amount", PAPER_EUR), fees_total=pos.get("fees_eur"))
                st["closed"].append({**pos, "exit": a["price"], "exit_ts": int(time.time()), "reason": why, "ret": ret,
                                     "pnl_eur": round(pnl, 2)})
                st["alerts"].append({"ts": int(time.time()), "symbol": sym, "side": "VENTA", "price": a["price"],
                                     "reason": why, "ret": ret})
                del st["positions"][sym]
                fx = eur_rate(http, b.currency)
                eur = f" ≈ {fmt_eur(a['price'] * fx)}" if fx and b.currency.upper() != "EUR" else ""
                head = (f"📈 ACCIONES · ✅ CIERRE CON BENEFICIO · VENDER {sym}" if pnl >= 0 else
                        f"📈 ACCIONES · ❌ CIERRE CON PÉRDIDA · VENDER {sym}")
                notify(f"{head}\n{b.name} a ~{a['price']:.2f} {b.currency}{eur}\nMotivo: {why}\n"
                       f"Resultado de la alerta: {ret:+.1f}% · con {pos.get('amount', PAPER_EUR):.0f} € habrías "
                       f"{'ganado' if pnl >= 0 else 'perdido'} {abs(pnl):.2f} € (con comisiones).\n"
                       f"El cerebro de acciones aprende de este resultado.")
                status = "venta"
            else:
                status = "en cartera"
        elif a["signal"] and st.get("last_buy", {}).get(sym) != today:
            exp = ((a["plan"].get("expected") or {}).get("expectancy") or 0.0)
            score = exp + 0.2 * min(a["vol_ratio"], 5) + (0.5 if a["trend"] == "alcista" else 0.0)
            pending.append((score, sym, b, a))
            status = "señal"
        rows.append({"symbol": sym, "name": b.name, "currency": b.currency, "price": a["price"], "chg_1d": a["chg_1d"],
                     "chg_1m": a["chg_1m"], "rsi": a["rsi"], "trend": a["trend"], "signal": a["signal"],
                     "status": status, "market_open": b.market_open, "checked": int(time.time()),
                     "reasoning": a["reasoning"], "plan": a.get("plan"),
                     "eur_rate": eur_rate(http, b.currency) if b.currency.upper() != "EUR" else 1.0,
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
    # ---- alertas de compra: solo las mejores y con un máximo diario
    max_day = int(os.getenv("STOCKS_MAX_ALERTS_DAY", "0")) or 10**6     # 0 = sin límite (solo estrategias con ventaja)
    ctl = {}
    try:
        ctl = json.loads((state_dir / "control.json").read_text())
    except (OSError, ValueError):
        pass
    muted = ctl.get("stocks_alerts") is False
    daily = st.get("daily_alerts") or {}
    if daily.get("day") != today:
        daily = {"day": today, "n": 0}
    pending.sort(key=lambda x: -x[0])
    by_sym = {r["symbol"]: r for r in rows}
    for score, sym, b, a in pending:
        if muted or daily["n"] >= max_day:
            if sym in by_sym:
                by_sym[sym]["status"] = "señal (silenciada)" if muted else "señal (límite diario)"
            continue
        daily["n"] += 1
        pl = a["plan"]
        st["positions"][sym] = {"symbol": sym, "name": b.name, "currency": b.currency, "entry": a["price"],
                                "entry_ts": int(time.time()), "stop": pl["stop"], "tp": pl["tp"],
                                "max_days": pl["max_days"], "peak": a["price"], "atr": a["atr"],
                                "setup": a["signal"], "price": a["price"], "expected": pl.get("expected"),
                                "amount": PAPER_EUR, "fees_eur": trade_fees(sym, PAPER_EUR)}
        st.setdefault("last_buy", {})[sym] = today
        st["alerts"].append({"ts": int(time.time()), "symbol": sym, "side": "COMPRA", "price": a["price"],
                             "reason": a["signal"], "stop": pl["stop"], "tp": pl["tp"]})
        ex = pl.get("expected") or {}
        hist = (f"\nEn el histórico (datos no vistos): acierto {ex['win_rate']}% · media {ex['expectancy']:+.2f}% "
                f"por operación ({ex['n']} casos)." if ex.get("n") else "")
        risk = (a["price"] - pl["stop"]) / a["price"] * 100
        reward = (pl["tp"] - a["price"]) / a["price"] * 100
        cap_line = "" if max_day >= 10**6 else f"Alerta {daily['n']} de {max_day} como máximo hoy.\n"
        fx = eur_rate(http, b.currency)
        e = (lambda v, fx=fx: f" ≈ {fmt_eur(v * fx)}") if fx and b.currency.upper() != "EUR" else (lambda v: "")
        notify(f"📈 ACCIONES · 🟢 COMPRAR {sym}\n{b.name} a ~{a['price']:.2f} {b.currency}{e(a['price'])}\nSeñal: {a['signal']} · "
               f"RSI {a['rsi']:.0f} · tendencia {a['trend']}\n"
               f"🛑 Stop loss: {pl['stop']:.2f}{e(pl['stop'])} (−{risk:.1f}%)\n"
               f"🎯 Take profit: {pl['tp']:.2f}{e(pl['tp'])} (+{reward:.1f}%)\n"
               f"⏳ Plazo máximo: {pl['max_days']} días de mercado{hist}"
               f"{'' if b.market_open else chr(10) + 'Mercado cerrado: precio del último cierre.'}\n"
               f"{cap_line}"
               f"Costes con {paper_summary(st)['broker']} (comisión, divisa e impuestos, ida y vuelta): "
               f"~{fmt_eur(st['positions'][sym]['fees_eur'])} con {PAPER_EUR:.0f} €.\n"
               f"Es una alerta, no una orden: decides tú en tu broker.")
        if sym in by_sym:
            by_sym[sym]["status"] = "compra"
        chart_f = charts / f"{sym.replace('.', '_')}.json"
        try:
            ch = json.loads(chart_f.read_text())
            ch["alerts"] = [x for x in st["alerts"] if x["symbol"] == sym][-20:]
            ch["position"] = st["positions"].get(sym)
            chart_f.write_text(json.dumps(ch))
        except (OSError, ValueError):
            pass
    st["daily_alerts"] = daily

    # conservar lo ya analizado de las que no tocaban en este turno (y olvidar las que salieron del universo)
    merged = {**{k: v for k, v in prev.items() if k in universe}, **{r["symbol"]: r for r in rows}}
    for sym, r in merged.items():
        if sym not in st["positions"] and r.get("status") == "en cartera":
            r["status"] = "vigilando"
    closed = st["closed"]
    rets = [x["ret"] for x in closed]
    st["paper"] = paper_summary(st)
    st.update({"market_up": market_up, "ts": int(time.time()), "watchlist": list(merged.values()), "universe": len(universe), "alerts": st["alerts"][-200:], "closed": closed[-200:],
               "track": {"n": len(rets), "win_rate": round(100 * np.mean(np.array(rets) > 0), 1) if rets else None,
                         "avg_ret": round(float(np.mean(rets)), 2) if rets else None}})
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    tmp.replace(f)
    return st


def learn_daily(state_dir: Path, st: dict, every_h: float = 24, min_stocks: int = 50) -> dict | None:
    """Una vez al día: el cerebro de acciones aprende de 2 años de histórico y de las alertas reales."""
    from . import stocks_brain
    f = state_dir / "stocks_brain.json"
    try:
        prev = json.loads(f.read_text()) if f.exists() else {}
        last = prev.get("ts", 0)
    except ValueError:
        prev, last = {}, 0
    if time.time() - last < every_h * 3600 and stocks_brain.is_current(prev.get("meta") or {}):
        return None
    bars = {}
    for bf in (state_dir / "stock_bars").glob("*.json"):
        try:
            bars[bf.stem] = json.loads(bf.read_text())
        except ValueError:
            continue
    if len(bars) < min_stocks:
        return None
    t0 = time.time()
    res = stocks_brain.run(state_dir, bars, st.get("closed", []))
    if res:
        log.info("Cerebro de acciones: aprendió de %d acciones en %.0f s", len(bars), time.time() - t0)
        act = [k for k, v in res["setups"].items() if v.get("active")]
        notify("📈 ACCIONES · 🧠 aprendizaje diario completado. Estrategias activas: "
               + (", ".join(act) if act else "ninguna (ninguna demuestra ventaja ahora mismo)") + ".")
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="state")
    ap.add_argument("--loop", type=float, default=0, help="repetir cada N minutos")
    ap.add_argument("--watchlist", default="", help="símbolos separados por comas (por defecto, la lista incluida)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s stocks: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    wl_env = os.getenv("STOCKS_WATCHLIST", "")
    fixed = [s.strip().upper() for s in (a.watchlist or wl_env).split(",") if s.strip()]
    from . import version
    http = httpx.Client(timeout=20)
    dyn, dyn_ts = [], 0.0
    while True:
        try:
            if not fixed and time.time() - dyn_ts > 6 * 3600:
                dyn, dyn_ts = screener_symbols(http), time.time()
                log.info("Universo dinámico de Yahoo: %d acciones", len(dyn))
            wl = fixed or list(dict.fromkeys(DEFAULT_WATCHLIST + dyn))
            st = run_once(Path(a.state), wl, http=http, max_per_run=150 if a.loop else 0, pause=1.0 if a.loop else 0)
            learn_daily(Path(a.state), st)
            log.info("Universo %d acciones · %d con datos · %d alertas abiertas", len(wl), len(st["watchlist"]),
                     len(st["positions"]))
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
