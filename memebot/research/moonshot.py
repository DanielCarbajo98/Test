"""Estudio «cazador»: ¿compensa apostar poco a memecoins recién nacidas buscando las que se multiplican?

Usa la muestra SIN sesgo de supervivencia (data_launches: lanzamientos apuntados al nacer, ganen o pierdan).
  1. Distribución real: qué % llega a 2x, 5x, 10x, 100x desde el precio de entrada y qué % se muere.
  2. Regla cazador: entrar a los E minutos del lanzamiento si pasa unos filtros (volumen desde el nacimiento,
     impulso de 5 min, subida máxima ya hecha); al llegar a 2x vender la MITAD (recuperas lo invertido) y dejar
     correr el resto con stop dinámico; stop de pérdida y plazo máximo.
  3. Elige la regla con el periodo antiguo (70 %) y la COMPRUEBA en el reciente (30 %) que no ha visto.
Los filtros solo usan datos que el bot también tiene en directo (DexScreener: edad, volumen, cambio 5 min/1 h).

  python -m research.moonshot --data data_launches --out state/moonshot.json
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import time
from pathlib import Path

import numpy as np

COST_SIDE = 0.03            # recién nacidas: deslizamiento + comisiones + prioridad (~3 % por lado, prudente)
ENTRY_MIN = [5, 15, 30, 60]
MIN_VOL = [0, 2_000, 10_000, 50_000]          # $ negociados desde el nacimiento hasta la entrada
MOM5 = [None, 0.0, 0.2]                        # cambio de precio en los últimos 5 min (mínimo)
RUNUP_MAX = [3.0, 10.0, math.inf]              # cuánto ha subido ya desde el nacimiento (máximo, en x)
EXITS = [{"stop": s, "trail": t, "hours": h} for s in (0.3, 0.5) for t in (0.3, 0.5) for h in (6, 24)]
DEFAULT_EXIT = {"stop": 0.5, "trail": 0.4, "hours": 24}
MIN_TRADES_IS, MIN_TRADES_OOS = 40, 30


def load(data_dir: str | Path, now: float | None = None) -> list[dict]:
    """Lanzamientos con velas suficientes: [{pool, created, o,h,l,c,v (por minuto desde el nacimiento)}]."""
    from bot.signals import to_series
    d = Path(data_dir)
    try:
        index = json.loads((d / "pools.json").read_text())
    except (OSError, ValueError):
        return []
    now = now or time.time()
    out = []
    for addr, meta in index.items():
        created = int(meta.get("created_at") or 0)
        if not created or now - created < 7 * 3600:            # aún no sabemos cómo acaba
            continue
        f = d / "ohlcv" / f"{addr}.json"
        try:
            rows = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        s = to_series(rows)
        if s is None or len(s.c) < 10:
            continue
        ts0 = int(s.ts[0])
        if ts0 - created > 20 * 60:                              # empieza demasiado tarde: no vemos el nacimiento
            continue
        # rellenar hasta 25 h tras el nacimiento con el último precio (sin operaciones = token muerto/plano)
        n_need = int((min(now, created + 25 * 3600) - ts0) // 60) + 1
        o, h, l, c, v = (np.asarray(x, float) for x in (s.o, s.h, s.l, s.c, s.v))
        if len(c) < n_need:
            pad = n_need - len(c)
            last = c[-1]
            o, h, l, c = (np.concatenate([x, np.full(pad, last)]) for x in (o, h, l, c))
            v = np.concatenate([v, np.zeros(pad)])
        off = max(0, (ts0 - created) // 60)                      # minutos entre el nacimiento y la 1.ª vela
        out.append({"pool": addr, "mint": meta.get("base_mint"), "name": meta.get("name", ""), "created": created,
                    "off": int(off), "o": o, "h": h, "l": l, "c": np.nan_to_num(c, nan=0.0), "v": np.nan_to_num(v)})
    return out


def features(L: dict, entry_min: int) -> dict | None:
    i = entry_min - L["off"]
    if i < 1 or i >= len(L["c"]) - 1:
        return None
    c, v = L["c"], L["v"]
    first = next((x for x in L["o"][:i] if x and x > 0), None)
    if not first or c[i] <= 0:
        return None
    j = max(0, i - 5)
    return {"i": i, "vol": float(v[:i + 1].sum()), "mom5": float(c[i] / c[j] - 1) if c[j] > 0 else 0.0,
            "runup": float(c[i] / first)}


def simulate(L: dict, i: int, ex: dict, cost: float = COST_SIDE) -> float:
    """Retorno de una apuesta entrando en la apertura de la vela i+1. Prudente: si en la misma vela se tocan
    el stop y el 2x, manda el stop."""
    o, h, lo, c = L["o"], L["h"], L["l"], L["c"]
    e = i + 1
    end = min(len(c), e + int(ex["hours"] * 60))
    if e >= end:
        return 0.0
    p0 = o[e] if o[e] > 0 else c[i]
    if p0 <= 0:
        return 0.0
    hh, ll = h[e:end] / p0, lo[e:end] / p0
    stop = 1 - ex["stop"]
    hit_stop = np.flatnonzero(ll <= stop)
    hit_2x = np.flatnonzero(hh >= 2.0)
    s_i = hit_stop[0] if len(hit_stop) else None
    d_i = hit_2x[0] if len(hit_2x) else None
    buy = 1 + cost
    if s_i is not None and (d_i is None or s_i <= d_i):
        return stop * (1 - cost) / buy - 1
    if d_i is None:
        return (c[end - 1] / p0) * (1 - cost) / buy - 1
    half = 0.5 * 2.0 * (1 - cost)                              # la mitad vendida a 2x
    peak = np.maximum.accumulate(hh[d_i:])
    trail_hit = np.flatnonzero(ll[d_i + 1:] <= peak[:-1] * (1 - ex["trail"]))
    if len(trail_hit):
        k = trail_hit[0]
        rest = peak[k] * (1 - ex["trail"])
    else:
        rest = c[end - 1] / p0
    return (half + 0.5 * rest * (1 - cost)) / buy - 1


def _stats(r: list[float]) -> dict:
    if not r:
        return {"n": 0, "mean_pct": None, "win_rate": None, "lcb_pct": None, "best_x": None, "median_pct": None}
    x = np.asarray(r)
    lcb = x.mean() - x.std(ddof=1) / math.sqrt(len(x)) if len(x) > 1 else None
    return {"n": len(x), "mean_pct": round(100 * float(x.mean()), 2), "win_rate": round(100 * float((x > 0).mean()), 1),
            "lcb_pct": round(100 * lcb, 2) if lcb is not None else None, "best_x": round(float(x.max()) + 1, 2),
            "median_pct": round(100 * float(np.median(x)), 2)}


def distribution(launches: list[dict], entry_min: int = 5) -> dict:
    peaks, finals = [], []
    for L in launches:
        f = features(L, entry_min)
        if not f:
            continue
        i = f["i"]
        p0 = L["c"][i]
        end = min(len(L["c"]), i + 24 * 60)
        peaks.append(float(np.nanmax(L["h"][i + 1:end]) / p0) if end > i + 1 else 1.0)
        finals.append(float(L["c"][end - 1] / p0))
    if not peaks:
        return {"n": 0}
    p, fn = np.asarray(peaks), np.asarray(finals)
    pct = lambda m: round(100 * float(m.mean()), 1)  # noqa: E731
    return {"n": len(p), "entry_min": entry_min, "reach_2x": pct(p >= 2), "reach_5x": pct(p >= 5),
            "reach_10x": pct(p >= 10), "reach_100x": pct(p >= 100), "dead_24h": pct(fn < 0.1),
            "below_entry_24h": pct(fn < 1)}


def rule_ok(f: dict, r: dict) -> bool:
    return (f["vol"] >= r["min_vol"] and (r["mom5"] is None or f["mom5"] >= r["mom5"])
            and f["runup"] <= r["runup_max"])


def study(launches: list[dict]) -> dict:
    res = {"ts": int(time.time()), "launches": len(launches), "cost_side_pct": 100 * COST_SIDE,
           "distribution": distribution(launches, 5), "distribution_30": distribution(launches, 30)}
    if len(launches) < 100:
        res.update({"edge": False, "note": f"Pocos lanzamientos con resultado conocido ({len(launches)} de 100 mínimos)."})
        return res
    split = float(np.quantile([L["created"] for L in launches], 0.7))
    # precálculo: rasgos y retorno de cada lanzamiento para cada (entrada, salida)
    pre = {}
    for E in ENTRY_MIN:
        rows = []
        for L in launches:
            f = features(L, E)
            if f:
                rows.append((L["created"] >= split, f, [simulate(L, f["i"], ex) for ex in EXITS]))
        pre[E] = rows
    best = None
    for E, mv, m5, rmax in itertools.product(ENTRY_MIN, MIN_VOL, MOM5, RUNUP_MAX):
        r = {"entry_min": E, "min_vol": mv, "mom5": m5, "runup_max": rmax}
        sel = [(oos, rets) for oos, f, rets in pre[E] if rule_ok(f, r)]
        ins = [rets for oos, rets in sel if not oos]
        if len(ins) < MIN_TRADES_IS:
            continue
        for k, ex in enumerate(EXITS):
            si = _stats([x[k] for x in ins])
            if best is None or si["lcb_pct"] > best[2]["lcb_pct"]:
                best = (r, k, si, sel)
    if best is None:
        res.update({"edge": False, "note": "Ninguna regla tiene suficientes operaciones en el periodo de aprendizaje."})
        return res
    r, k, si, sel = best
    so = _stats([rets[k] for oos, rets in sel if oos])
    edge = bool(so["n"] >= MIN_TRADES_OOS and (so["mean_pct"] or -1) > 0 and (si["mean_pct"] or -1) > 0)
    rule = {**r, **EXITS[k], "runup_max": None if math.isinf(r["runup_max"]) else r["runup_max"]}
    res.update({"rule": rule, "in_sample": si, "out_of_sample": so, "edge": edge,
                "note": ("La regla gana también en datos que no vio: el modo cazador puede avisar." if edge else
                         "Ninguna regla demuestra ganar después de costes en datos no vistos: el cazador solo "
                         "opera en simulado y en silencio, para seguir midiendo.")})
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data_launches")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    res = study(load(a.data))
    txt = json.dumps(res, indent=1, default=float)
    if a.out:
        Path(a.out).write_text(txt)
    print(txt)


if __name__ == "__main__":
    main()
