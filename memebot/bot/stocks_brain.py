"""Cerebro de las acciones: aprende cada día qué reglas, stops y objetivos funcionan, y de sus propias alertas.

1. HISTÓRICO. Con ~2 años de velas diarias de cientos de acciones, simula miles de operaciones de cada
   estrategia («Retroceso en tendencia», «Ruptura») con distintas combinaciones de:
     · umbral de entrada (RSI del retroceso),  · stop (x ATR),  · take profit (x ATR),  · plazo máximo (días).
   Elige con el periodo antiguo (70 %) y COMPRUEBA con el reciente (30 %) que no ha visto: así no se engaña.
2. REALIDAD. Cada alerta cerrada (stop, objetivo, plazo, tendencia rota) se compara con lo esperado.
   Si una estrategia rinde en real claramente peor de lo que prometía el histórico, se SUSPENDE hasta que
   el siguiente aprendizaje vuelva a demostrar ventaja; si nunca tuvo ventaja fuera de muestra, no se activa.
3. Escribe lecciones legibles y guarda todo en state/stocks_brain.json (lo usa el servicio de acciones).
Costes simulados: los REALES de tu broker (bot/brokers.py): comisión, cambio de divisa, impuesto a las
transacciones y diferencial. Además compara los brokers y por regiones (Europa / EE. UU.), y solo activa las
alertas de las bolsas donde la estrategia gana DESPUÉS de costes. No es asesoramiento financiero.
"""
from __future__ import annotations

import itertools
import json
import math
import time
from pathlib import Path

import numpy as np

from .stocks import atr, rsi, sma

import os

SPREAD = 0.001                     # diferencial de compra/venta y deslizamiento, por lado
COST = 0.0015                      # (compatibilidad) coste por lado sin comisión fija


from . import brokers


def cost_per_side(position_eur: float | None = None, fee_eur: float | None = None) -> float:
    """Coste por lado con TU broker: comisión fija / importe + diferencial.
    Trade Republic: 1 € por orden -> con 100 € es un 1 % por lado (2 % ida y vuelta)."""
    pos = position_eur if position_eur is not None else float(os.getenv("STOCKS_PAPER_EUR", "100"))
    fee = fee_eur if fee_eur is not None else float(os.getenv("STOCKS_FEE_EUR", "1"))
    return SPREAD + (fee / pos if pos > 0 else 0.0)


MODEL_VERSION = 4                  # súbelo al cambiar cómo se aprende: obliga a reaprender al momento


def is_current(meta: dict) -> bool:
    """¿Lo aprendido corresponde a esta versión, a tu broker y a tu importe por operación?"""
    return (meta.get("model") == MODEL_VERSION and meta.get("broker") == brokers.broker_key()
            and float(meta.get("position_eur") or -1) == float(os.getenv("STOCKS_PAPER_EUR", "100")))


SIZES_EUR = [100, 250, 500, 1000, 2500, 5000]
SETUPS = ["Retroceso en tendencia", "Ruptura", "Momentum"]
DEFAULT_PARAMS = {
    "Retroceso en tendencia": {"rsi_max": 35, "stop_atr": 2.0, "tp_atr": 3.0, "max_days": 20, "regime": 0},
    "Ruptura": {"rsi_cap": 75, "stop_atr": 2.0, "tp_atr": 4.0, "max_days": 30, "regime": 0},
    # Momentum: las que más han subido en 6 meses (sin contar el último) suelen seguir subiendo; pocas operaciones
    "Momentum": {"mom_min": 30, "ret21_max": 10, "stop_atr": 3.0, "tp_atr": 6.0, "max_days": 40, "regime": 1},
}
# regime = 1: solo comprar cuando el mercado general (S&P 500, SPY) está sobre su media de 200 días
GRID = {
    "Retroceso en tendencia": {"rsi_max": [30, 35, 40], "stop_atr": [1.5, 2.0, 3.0], "tp_atr": [2.0, 3.0, 5.0],
                               "max_days": [10, 20, 40], "regime": [0, 1]},
    "Ruptura": {"rsi_cap": [70, 75, 80], "stop_atr": [1.5, 2.0, 3.0], "tp_atr": [3.0, 4.0, 6.0],
                "max_days": [15, 30, 60], "regime": [0, 1]},
    "Momentum": {"mom_min": [20, 35, 50], "ret21_max": [5, 10, 20], "stop_atr": [2.0, 3.0, 4.0],
                 "tp_atr": [4.0, 6.0, 10.0], "max_days": [20, 40, 60], "regime": [0, 1]},
}
REGIME_SYMBOL = "SPY"


def regime_series(bars: dict[str, dict]) -> tuple[np.ndarray, np.ndarray] | None:
    """(timestamps, mercado_alcista) del índice de referencia: cierre del SPY sobre su media de 200 días."""
    b = bars.get(REGIME_SYMBOL)
    if not b or len(b.get("c", [])) < 210:
        return None
    c = np.asarray(b["c"], float)
    s200 = sma(c, 200)
    return np.asarray(b["ts"], float), np.isfinite(s200) & (c > s200)


def regime_ok_at(reg, ts: np.ndarray) -> np.ndarray:
    if reg is None:
        return np.ones(len(ts), bool)
    rts, up = reg
    j = np.searchsorted(rts, ts + 43200, side="right") - 1      # dato del mismo día o anterior
    return np.where(j >= 0, up[np.clip(j, 0, len(up) - 1)], True)
MIN_TRADES_OOS = 30
MIN_TRADES_REGION = 30


# --------------------------------------------------------------------------- simulación
def indicators(b: dict) -> dict:
    c, h, lo, v = (np.asarray(b[k], dtype=float) for k in ("c", "h", "l", "v"))
    n = len(c)
    hi55 = np.full(n, np.nan)
    for i in range(56, n):
        hi55[i] = h[i - 55:i].max()
    vavg = sma(v, 20)
    chg = np.concatenate([[0.0], (c[1:] / c[:-1] - 1) * 100])
    mom = np.full(n, np.nan)                     # subida de 6 meses sin el último mes (en %)
    ret21 = np.full(n, np.nan)
    if n > 126:
        mom[126:] = (c[105:n - 21] / c[:n - 126] - 1) * 100
    if n > 21:
        ret21[21:] = (c[21:] / c[:-21] - 1) * 100
    return {"c": c, "o": np.asarray(b["o"], float), "h": h, "l": lo, "ts": np.asarray(b["ts"], float),
            "sma50": sma(c, 50), "sma200": sma(c, 200), "rsi": rsi(c), "atr": atr(h, lo, c), "hi55": hi55,
            "vol_ratio": np.divide(v, vavg, out=np.zeros(n), where=vavg > 0), "chg": chg, "mom": mom, "ret21": ret21}


def signals(ind: dict, setup: str, p: dict) -> np.ndarray:
    c, s50, s200, r = ind["c"], ind["sma50"], ind["sma200"], ind["rsi"]
    ok = np.isfinite(s200) & np.isfinite(r) & np.isfinite(ind["atr"])
    if p.get("regime") and "regime_ok" in ind:
        ok = ok & ind["regime_ok"]
    if setup == "Retroceso en tendencia":
        return ok & (c > s200) & (s50 > s200) & (r < p["rsi_max"])
    if setup == "Momentum":
        m, r21 = ind["mom"], ind["ret21"]
        good = np.isfinite(m) & np.isfinite(r21)
        return (ok & good & (c > s200) & (s50 > s200) & (np.nan_to_num(m) >= p["mom_min"])
                & (np.nan_to_num(r21) <= p["ret21_max"]) & (np.nan_to_num(r21) >= -10))
    return (ok & (c > s200) & (c > ind["hi55"]) & (ind["vol_ratio"] > 1.5) & (r <= p["rsi_cap"])
            & (np.abs(ind["chg"]) <= 15))


def simulate(ind: dict, setup: str, p: dict, cost: float | tuple[float, float] = COST) -> list[tuple[float, float, str]]:
    """Operaciones (ts_entrada, retorno, motivo). Entrada en la apertura del día siguiente a la señal.
    `cost`: coste por lado, o (coste de compra, coste de venta)."""
    c_in, c_out = cost if isinstance(cost, tuple) else (cost, cost)
    sig = np.flatnonzero(signals(ind, setup, p))
    o, h, lo, c, a = ind["o"], ind["h"], ind["l"], ind["c"], ind["atr"]
    trend = ind["sma200"] if setup in ("Retroceso en tendencia", "Momentum") else ind["sma50"]
    out, free = [], 0
    n = len(c)
    for i in sig:
        e = i + 1
        if e >= n or e < free:
            continue
        entry = o[e] * (1 + c_in)
        stop, tp = entry - p["stop_atr"] * a[i], entry + p["tp_atr"] * a[i]
        reason, px, j = "plazo", c[min(n - 1, e + p["max_days"])], min(n - 1, e + p["max_days"])
        for k in range(e, min(n, e + p["max_days"] + 1)):
            if lo[k] <= stop:                         # el stop manda si ambos ocurren el mismo día
                px, reason, j = min(o[k], stop) if k > e else stop, "stop", k
                break
            if h[k] >= tp:
                px, reason, j = max(o[k], tp) if k > e else tp, "objetivo", k
                break
            if np.isfinite(trend[k]) and c[k] < trend[k] and k > e:
                px, reason, j = c[k], "tendencia rota", k
                break
        out.append((float(ind["ts"][e]), (px * (1 - c_out)) / entry - 1, reason))
        free = j + 1
    return out


def _stats(r: list[float]) -> dict:
    if not r:
        return {"n": 0, "win_rate": None, "expectancy": None, "lcb": None}
    x = np.array(r)
    lcb = 100 * (x.mean() - x.std(ddof=1) / math.sqrt(len(x))) if len(x) > 1 else None
    return {"n": len(x), "win_rate": round(100 * float((x > 0).mean()), 1), "expectancy": round(100 * float(x.mean()), 2),
            "lcb": round(lcb, 2) if lcb is not None else None}


# --------------------------------------------------------------------------- aprendizaje
def _sim_all(inds: dict, setup: str, p: dict, amount: float, broker: str) -> list[tuple[float, float, str, str]]:
    out = []
    for sym, ind in inds.items():
        reg = brokers.region(sym)
        out += [(t[0], t[1], t[2], reg) for t in simulate(ind, setup, p, brokers.costs(sym, amount, broker))]
    return out


def optimize(bars: dict[str, dict], cost: float | None = None, amount: float | None = None,
             broker: str | None = None, compare: bool = True) -> dict:
    """Aprende con los costes REALES de tu broker y tu importe por operación (o con `cost` fijo, para pruebas)."""
    amount = amount if amount is not None else float(os.getenv("STOCKS_PAPER_EUR", "100"))
    broker = brokers.broker_key(broker)
    inds = {s: indicators(b) for s, b in bars.items() if len(b.get("c", [])) >= 260}
    if len(inds) < 20:
        return {}
    reg = regime_series(bars)
    for ind in inds.values():
        ind["regime_ok"] = regime_ok_at(reg, ind["ts"])
    all_ts = np.concatenate([i["ts"] for i in inds.values()])
    split = float(np.quantile(all_ts, 0.7))

    def sim(setup, p, amt=amount, brk=broker):
        if cost is not None:
            return [(t[0], t[1], t[2], brokers.region(sym)) for sym, ind in inds.items()
                    for t in simulate(ind, setup, p, cost)]
        return _sim_all(inds, setup, p, amt, brk)

    res = {}
    for setup in SETUPS:
        best = None
        keys = list(GRID[setup])
        for vals in itertools.product(*(GRID[setup][k] for k in keys)):
            p = dict(zip(keys, vals))
            trades = sim(setup, p)
            ins = [t[1] for t in trades if t[0] < split]
            if len(ins) < 50:
                continue
            si = _stats(ins)
            if best is None or si["lcb"] > best[1]["lcb"]:
                best = (p, si, trades)
        if best is None:
            res[setup] = {"params": DEFAULT_PARAMS[setup], "edge": False, "note": "pocas operaciones históricas"}
            continue
        p, si, trades = best
        oos = [t for t in trades if t[0] >= split]
        so = _stats([t[1] for t in oos])
        reasons = {}
        for t in oos:
            reasons[t[2]] = reasons.get(t[2], 0) + 1
        edge = _has_edge(so)
        # por regiones: solo se activan las bolsas donde gana después de costes
        by_region = {}
        for rg in ("EU", "US", "OTRA"):
            ri, ro = [t[1] for t in trades if t[3] == rg and t[0] < split], [t[1] for t in oos if t[3] == rg]
            if ro or ri:
                st_o = _stats(ro)
                by_region[rg] = {"in_sample": _stats(ri), "out_of_sample": st_o,
                                 "edge": bool(st_o["n"] >= MIN_TRADES_REGION and (st_o["expectancy"] or -1) > 0
                                              and (st_o["lcb"] if st_o["lcb"] is not None else -9) > -1.0
                                              and (_stats(ri)["expectancy"] or -1) > 0)}
        regions = [rg for rg, v in by_region.items() if v["edge"]]
        if edge and not regions:
            regions = list(by_region)          # gana en conjunto aunque cada región tenga pocas operaciones
        if not edge and regions:
            edge = True                        # no gana en conjunto, pero sí en alguna región concreta
        so_all = so
        if edge and regions and len(regions) < len(by_region):     # lo que se espera es lo de las bolsas activas
            so = _stats([t[1] for t in oos if t[3] in regions])
            reasons = {}
            for t in oos:
                if t[3] in regions:
                    reasons[t[2]] = reasons.get(t[2], 0) + 1
        keep = (lambda t: t[3] in regions) if (edge and regions) else (lambda t: True)
        by_size, by_broker = {}, {}
        if compare and cost is None:
            for size in SIZES_EUR:
                by_size[str(size)] = _stats([t[1] for t in sim(setup, p, size) if t[0] >= split and keep(t)])
            for bk in brokers.BROKERS:
                tr = [t for t in sim(setup, p, amount, bk) if t[0] >= split and keep(t)]
                by_broker[bk] = {"all": _stats([t[1] for t in tr]),
                                 **{rg: _stats([t[1] for t in tr if t[3] == rg]) for rg in ("EU", "US")}}
        res[setup] = {"params": p, "in_sample": si, "out_of_sample": so, "out_of_sample_all": so_all,
                      "exit_reasons_oos": reasons, "edge": bool(edge),
                      "regions": regions if edge else [], "by_region": by_region,
                      "by_size": by_size, "by_broker": by_broker}
    b = brokers.BROKERS[broker]
    res["_meta"] = {"model": MODEL_VERSION, "stocks": len(inds), "split_ts": int(split), "ts": int(time.time()), "position_eur": amount,
                    "broker": broker, "broker_name": b["name"], "broker_note": b["note"]}
    return res


def _has_edge(so: dict) -> bool:
    return bool(so["n"] >= MIN_TRADES_OOS and so["expectancy"] is not None and so["expectancy"] > 0
                and (so["lcb"] or -1) > -0.5)


def live_feedback(learned: dict, closed: list[dict]) -> dict:
    """Compara las alertas reales cerradas con lo que prometía el histórico y decide si suspender."""
    out = {}
    for setup in SETUPS:
        rets = [c["ret"] / 100 for c in closed if c.get("setup") == setup and c.get("ret") is not None]
        live = _stats(rets)
        exp_hist = ((learned.get(setup) or {}).get("out_of_sample") or {}).get("expectancy")
        suspended = False
        why = ""
        if live["n"] >= 8 and exp_hist is not None and live["expectancy"] < min(0.0, exp_hist - 2.0):
            suspended, why = True, (f"en real rinde {live['expectancy']:+.2f}% por alerta frente a {exp_hist:+.2f}% "
                                    f"esperado ({live['n']} alertas)")
        out[setup] = {"live": live, "suspended": suspended, "why": why}
    return out


def lessons(learned: dict, fb: dict) -> list[str]:
    out = []
    for setup in SETUPS:
        L = learned.get(setup) or {}
        p, so = L.get("params", {}), L.get("out_of_sample") or {}
        if so.get("n"):
            out.append(f"«{setup}»: con stop a {p.get('stop_atr')} ATR, objetivo a {p.get('tp_atr')} ATR y plazo de "
                       f"{p.get('max_days')} días, en datos recientes no vistos acierta el {so['win_rate']}% con "
                       f"{so['expectancy']:+.2f}% de media por operación ({so['n']} operaciones)."
                       + ("" if L.get("edge") else " No demuestra ventaja suficiente: sus alertas quedan desactivadas."))
            er = L.get("exit_reasons_oos") or {}
            if er:
                top = max(er, key=er.get)
                out.append(f"«{setup}»: la salida más frecuente es «{top}» ({er[top]} de {sum(er.values())}).")
        f = fb.get(setup) or {}
        if L.get("regions") and len(L.get("by_region") or {}) > 1:
            alls = L.get("out_of_sample_all") or {}
            out.append(f"«{setup}»: solo se activa en {', '.join(brokers.REGION_NAMES[r] for r in L['regions'])}: "
                       f"en el resto no gana después de costes"
                       + (f" (contando todas las bolsas saldría {alls['expectancy']:+.2f}%)." if alls.get("n") else "."))
        bb = L.get("by_broker") or {}
        if bb:
            rank = sorted(bb, key=lambda k: bb[k]["all"].get("expectancy") or -99, reverse=True)
            out.append(f"«{setup}» por broker (media por operación con {int(float(os.getenv('STOCKS_PAPER_EUR', '100')))} €): " + " · ".join(
                f"{brokers.BROKERS[k]['name']} {bb[k]['all']['expectancy']:+.2f}%" for k in rank
                if bb[k]["all"].get("expectancy") is not None) + ".")
        bs = L.get("by_size") or {}
        good = [int(k) for k, v in bs.items() if (v.get("expectancy") or -1) > 0 and v.get("n", 0) >= MIN_TRADES_OOS]
        if bs:
            if good:
                out.append(f"«{setup}»: con tu broker, compensa a partir de ~{min(good)} € por operación "
                           f"(por debajo, la comisión se come la ganancia).")
            else:
                out.append(f"«{setup}»: con tu broker no compensa con ningún importe probado (hasta "
                           f"{max(int(k) for k in bs)} €).")
        if p.get("regime"):
            out.append(f"«{setup}» funciona mejor comprando solo cuando el mercado general (S&P 500) está en tendencia "
                       f"alcista (sobre su media de 200 días).")
        if f.get("live", {}).get("n"):
            lv = f["live"]
            out.append(f"«{setup}» en real: {lv['n']} alertas cerradas, acierto {lv['win_rate']}%, media "
                       f"{lv['expectancy']:+.2f}%." + (f" SUSPENDIDA: {f['why']}." if f.get("suspended") else ""))
    return out


def run(state_dir: Path, bars: dict[str, dict], closed: list[dict]) -> dict:
    learned = optimize(bars)
    if not learned:
        return {}
    fb = live_feedback(learned, closed)
    data = {"ts": int(time.time()), "setups": {}, "meta": learned.get("_meta", {})}
    for setup in SETUPS:
        L = learned[setup]
        active = bool(L.get("edge")) and not fb[setup]["suspended"]
        data["setups"][setup] = {**L, "live": fb[setup]["live"], "suspended": fb[setup]["suspended"],
                                 "suspended_why": fb[setup]["why"], "active": active}
    data["lessons"] = lessons(learned, fb)
    f = state_dir / "stocks_brain.json"
    prev = json.loads(f.read_text()) if f.exists() else {}
    data["history"] = (prev.get("history", []) + [{"ts": data["ts"], **{
        s: {"active": data["setups"][s]["active"], "oos_exp": (data["setups"][s].get("out_of_sample") or {}).get("expectancy")}
        for s in SETUPS}}])[-120:]
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=float))
    tmp.replace(f)
    return data


def params_for(state_dir: Path, setup: str) -> tuple[dict, bool, dict]:
    """(parámetros, activa, info) que debe usar el servicio de acciones para una estrategia."""
    try:
        d = json.loads((state_dir / "stocks_brain.json").read_text())
        s = d["setups"][setup]
        if "meta" in d and not is_current(d["meta"]):
            return {**DEFAULT_PARAMS[setup], **s["params"]}, False, s    # aprendido con otros costes: en pausa
        return {**DEFAULT_PARAMS[setup], **s["params"]}, bool(s.get("active")), s
    except (OSError, ValueError, KeyError):
        return dict(DEFAULT_PARAMS[setup]), False, {}      # sin aprendizaje todavía: no se envían alertas
