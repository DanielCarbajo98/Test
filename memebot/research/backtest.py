"""Backtester de estrategias sobre velas de 1 minuto.

Supuestos conservadores (para no engañarnos):
  * Entrada en la APERTURA de la vela siguiente a la señal (no al cierre que la generó).
  * Coste por lado: fee 0,25 % + slippage/MEV 1 % + impacto de AMM estimado + 0,05 USD de red.
  * Si en una misma vela se tocan el stop y el take-profit, se asume el stop.
  * Stops: si la vela abre por debajo del stop (gap), se ejecuta en la apertura; si se
    hunde más del 40 % por debajo del stop dentro de la vela (rug), se ejecuta en el mínimo.
  * Take-profits: solo cuentan si la vela CIERRA por encima del nivel (las mechas sueltas
    de pools sin liquidez no dan beneficios falsos). Las velas con salto >5x se descartan.
  * Validación walk-forward: los parámetros se eligen con la primera mitad temporal
    (in-sample) y se evalúan en la segunda mitad (out-of-sample), que no se ha visto.

Uso:
  python -m research.backtest --data data --out reports
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numba import njit

from bot.signals import STRATEGIES, features, to_series

FEE = 0.0025
SLIP = 0.01
NET_USD = 0.05
SIZE_USD = 40.0
COOLDOWN_MIN = 240
ARTIFACT_JUMP = 5.0
REASONS = ["stop", "trailing", "time", "tp_full", "end", "breakeven", "lock"]


@njit(cache=True)
def simulate(o, h, l, c, entry_i, side_cost, sl, tp_g, tp_p, trail, trail_act, time_stop, time_gain,
             lock_act=9.99e9, lock_keep=0.0):
    """Simula una operación. Devuelve (retorno_neto, índice_salida, motivo).
    lock_act / lock_keep: «candado de beneficio»: cuando el máximo supera +lock_act, el stop sube hasta conservar
    lock_keep (fracción) de la mejor ganancia alcanzada. El time stop solo actúa si NUNCA llegó a +time_gain."""
    n = len(c)
    pe = o[entry_i] * (1 + side_cost)          # precio efectivo de entrada
    remaining = 1.0                            # fracción de tokens que queda
    proceeds = 0.0                             # en unidades de "precio x fracción"
    peak = pe
    hit = np.zeros(len(tp_g), dtype=np.bool_)
    any_hit = False
    for j in range(entry_i, n):
        trusted = not (j > 0 and c[j] > c[j - 1] * 5.0)
        stop = pe if any_hit else pe * (1 - sl)
        locked = False
        pk_gain = peak / pe - 1
        if pk_gain >= lock_act:
            lv = pe * (1 + lock_keep * pk_gain)
            if lv > stop:
                stop, locked = lv, True
        # 1) stop / break-even / candado de beneficio
        if l[j] <= stop:
            fill = o[j] if o[j] < stop else stop
            if l[j] < stop * 0.6:
                fill = l[j]
            proceeds += remaining * fill * (1 - side_cost)
            return proceeds / pe - 1, j, 6 if locked else (5 if any_hit else 0)
        # 2) trailing stop
        if peak >= pe * (1 + trail_act):
            lvl = peak * (1 - trail)
            if l[j] <= lvl:
                fill = o[j] if o[j] < lvl else lvl
                if l[j] < lvl * 0.6:
                    fill = l[j]
                proceeds += remaining * fill * (1 - side_cost)
                return proceeds / pe - 1, j, 1
        # 3) take profits (solo con cierre por encima del nivel)
        if trusted:
            for k in range(len(tp_g)):
                lvl = pe * (1 + tp_g[k])
                if not hit[k] and c[j] >= lvl:
                    q = min(tp_p[k], remaining)
                    proceeds += q * lvl * (1 - side_cost)
                    remaining -= q
                    hit[k] = True
                    any_hit = True
            if remaining <= 1e-9:
                return proceeds / pe - 1, j, 3
            if c[j] > peak:
                peak = c[j]
        # 4) time stop
        if not any_hit and j - entry_i >= time_stop and peak < pe * (1 + time_gain):
            proceeds += remaining * c[j] * (1 - side_cost)
            return proceeds / pe - 1, j, 2
    proceeds += remaining * c[n - 1] * (1 - side_cost)
    return proceeds / pe - 1, n - 1, 4


@dataclass
class PoolData:
    name: str
    pool: str
    ts: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray  # noqa: E741
    c: np.ndarray
    f: dict
    side_cost: np.ndarray   # coste por lado en cada vela (depende de la liquidez estimada)
    first_entry: int        # índice de la primera vela en la que se permite entrar


def load(data_dir: str, min_candles: int = 120, post_selection: bool = True) -> list[PoolData]:
    d = Path(data_dir)
    index = json.loads((d / "pools.json").read_text())
    out = []
    for addr, meta in index.items():
        fp = d / "ohlcv" / f"{addr}.json"
        if not fp.exists() or meta["price_now"] <= 0 or meta["fdv_now"] <= 0:
            continue
        s = to_series(json.loads(fp.read_text()))
        if s is None or len(s) < min_candles:
            continue
        supply = meta["fdv_now"] / meta["price_now"]
        f = features(s, meta["created_at"], supply)
        # liquidez estimada ~10 % de la FDV (típico en memecoins graduadas), mínimo 10k
        liq = np.maximum(0.10 * f["fdv"], 10_000)
        impact = SIZE_USD / (liq / 2 + SIZE_USD)
        side = FEE + SLIP + impact + NET_USD / SIZE_USD
        sel = meta.get("selected_at", meta.get("sampled_at", meta["created_at"])) if post_selection else 0
        first = int(np.searchsorted(s.ts, sel))
        if first >= len(s) - 1:
            continue        # todavía no hay datos posteriores a la selección
        out.append(PoolData(meta["name"], addr, s.ts, s.o, s.h, s.l, s.c, f, side, first))
    return out


def run(pools: list[PoolData], strategy: str, entry_p: dict, exit_p: dict, split_ts: int | None = None):
    fn = STRATEGIES[strategy]
    tp_g = np.array([g / 100 for g, _ in exit_p["tp"]], dtype=np.float64)
    tp_p = np.array([p / 100 for _, p in exit_p["tp"]], dtype=np.float64)
    trades = []
    for pd in pools:
        sig = np.flatnonzero(fn(pd.f, entry_p))
        next_free = 0
        for i in sig:
            e = i + 1
            if e >= len(pd.c) or e < next_free or i < pd.first_entry:
                continue
            r, j, why = simulate(pd.o, pd.h, pd.l, pd.c, e, pd.side_cost[e], exit_p["sl"] / 100, tp_g, tp_p,
                                 exit_p["trail"] / 100, exit_p["trail_act"] / 100, exit_p["time"],
                                 exit_p["time_gain"] / 100, exit_p.get("lock_act", 1e9) / 100,
                                 exit_p.get("lock_keep", 0) / 100)
            trades.append((int(pd.ts[e]), int(pd.ts[j]), float(r), REASONS[why], pd.name))
            next_free = j + 1 + COOLDOWN_MIN
    return trades


def stats(trades) -> dict:
    if not trades:
        return {"n": 0, "win_rate": 0, "avg_win": 0, "avg_loss": 0, "expectancy": 0, "profit_factor": 0,
                "median": 0, "worst": 0, "best": 0, "max_dd": 0, "total": 0, "pools": 0}
    r = np.array([t[2] for t in trades])
    wins, losses = r[r > 0], r[r <= 0]
    order = np.argsort([t[0] for t in trades])
    cum = np.cumsum(r[order])
    dd = float((np.maximum.accumulate(np.concatenate([[0], cum]))[1:] - cum).max())
    return {
        "n": len(r),
        "win_rate": round(100 * len(wins) / len(r), 1),
        "avg_win": round(100 * wins.mean(), 1) if len(wins) else 0,
        "avg_loss": round(100 * losses.mean(), 1) if len(losses) else 0,
        "expectancy": round(100 * r.mean(), 2),
        "profit_factor": round(wins.sum() / -losses.sum(), 2) if losses.sum() < 0 else math.inf,
        "median": round(100 * float(np.median(r)), 1),
        "worst": round(100 * r.min(), 1),
        "best": round(100 * r.max(), 1),
        "max_dd": round(100 * dd, 1),          # en % del tamaño de UNA operación
        "total": round(100 * r.sum(), 1),      # suma de retornos (en "operaciones")
        "pools": len({t[4] for t in trades}),
    }


def portfolio(trades, start=1000.0, risk_pct=1.0, sl_pct=25.0, cap_pct=5.0, max_open=4) -> dict:
    """Simulación de cartera: posiciones simultáneas limitadas, tamaño por riesgo, con interés compuesto."""
    eq, open_pos, curve = start, [], [start]
    for t_in, t_out, r, _, _ in sorted(trades):
        for p in [p for p in open_pos if p[0] <= t_in]:
            eq += p[1] * p[2]
            open_pos.remove(p)
            curve.append(eq)
        if len(open_pos) >= max_open:
            continue
        size = min(eq * risk_pct / sl_pct, eq * cap_pct / 100)
        open_pos.append((t_out, size, r))
    for p in sorted(open_pos):
        eq += p[1] * p[2]
        curve.append(eq)
    curve = np.array(curve)
    dd = float(((np.maximum.accumulate(curve) - curve) / np.maximum.accumulate(curve)).max())
    return {"final_equity": round(eq, 2), "return_pct": round(100 * (eq / start - 1), 1), "max_drawdown_pct": round(100 * dd, 1)}


# --------------------------------------------------------------------------- rejillas
ENTRY_GRID = {
    "momentum": {"min_vol_60m": [30_000, 80_000], "min_accel": [1.0, 1.5], "max_ret_60m": [60, 150],
                 "min_age": [30, 120], "min_fdv": [100_000, 400_000]},
    "early": {"min_vol_5m": [10_000, 30_000], "min_ret_5m": [10, 30]},
    "dip": {"min_drawdown": [40, 60], "min_bounce": [3, 8], "min_vol_60m": [30_000, 80_000],
            "min_fdv": [200_000, 500_000]},
    "breakout": {"min_accel": [1.5, 2.5], "min_vol_60m": [50_000, 120_000], "max_ret_60m": [40, 80],
                 "min_fdv": [150_000, 400_000]},
    "baseline": {"min_vol_60m": [50_000]},
}
EXIT_GRID = {
    "sl": [15, 25, 35],
    "tp": [[[30, 50], [80, 25]], [[50, 50], [150, 25]], [[100, 50], [300, 25]]],
    "trail": [20, 35],
    "trail_act": [30],
    "time": [60, 180],
    "time_gain": [10],
    "lock_act": [20, 1e9],          # candado de beneficio desde +20 % (o sin candado): el cerebro decide
    "lock_keep": [50],
}


def grid(g: dict):
    keys = list(g)
    for vals in itertools.product(*(g[k] for k in keys)):
        yield dict(zip(keys, vals))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default="reports")
    ap.add_argument("--min-trades", type=int, default=30)
    ap.add_argument("--allow-pre-selection", action="store_true",
                    help="permitir entradas anteriores a la selección (SESGADO: solo para comparar)")
    a = ap.parse_args()
    pools = []
    for dd in a.data.split(","):
        pools += load(dd, post_selection=not a.allow_pre_selection)
    if not pools:
        print("Aún no hay datos posteriores a la selección. Deja el recolector en bucle más tiempo.")
        return
    all_ts = np.concatenate([p.ts[p.first_entry:] for p in pools])
    split = int(np.median(all_ts))
    print(f"{len(pools)} pools, {len(all_ts):,} velas de 1 min; corte in/out-of-sample: {split}")

    results = {}
    for strat in STRATEGIES:
        best = None
        rows = []
        for ep in grid(ENTRY_GRID[strat]):
            for xp in grid(EXIT_GRID):
                tr = run(pools, strat, ep, xp)
                ins = [t for t in tr if t[0] < split]
                oos = [t for t in tr if t[0] >= split]
                si, so = stats(ins), stats(oos)
                rows.append((ep, xp, si, so))
                if si["n"] >= a.min_trades and (best is None or si["expectancy"] > best[2]["expectancy"]):
                    best = (ep, xp, si, so, tr)
        if best is None:
            results[strat] = {"note": "pocas operaciones"}
            print(f"{strat:9s} sin suficientes operaciones")
            continue
        ep, xp, si, so, tr = best
        oos_exp = [r[3]["expectancy"] for r in rows if r[3]["n"] >= a.min_trades // 2]
        results[strat] = {
            "entry": ep, "exit": xp, "in_sample": si, "out_of_sample": so, "all": stats(tr),
            "portfolio_oos": portfolio([t for t in tr if t[0] >= split], sl_pct=xp["sl"]),
            "exit_reasons": {k: sum(1 for t in tr if t[3] == k) for k in REASONS},
            "oos_share_positive_configs": round(100 * np.mean(np.array(oos_exp) > 0), 1) if oos_exp else None,
            "combos": len(rows),
        }
        print(f"{strat:9s} IS exp {si['expectancy']:+6.2f}% (n={si['n']:4d})  |  OOS exp {so['expectancy']:+6.2f}% "
              f"(n={so['n']:4d}, win {so['win_rate']}%, PF {so['profit_factor']})  {ep} {xp['sl']}/{xp['tp']}/{xp['trail']}/{xp['time']}")
    out = Path(a.out)
    out.mkdir(exist_ok=True)
    meta = {"pools": len(pools), "candles": int(len(all_ts)), "split_ts": split, "data": a.data,
            "post_selection": not a.allow_pre_selection,
            "from_ts": int(all_ts.min()), "to_ts": int(all_ts.max()),
            "costs": {"fee": FEE, "slip": SLIP, "net_usd": NET_USD, "size_usd": SIZE_USD}}
    (out / "backtest.json").write_text(json.dumps({"meta": meta, "results": results}, indent=1, default=str))
    print("Guardado en", out / "backtest.json")


if __name__ == "__main__":
    main()
