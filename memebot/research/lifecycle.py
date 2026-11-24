"""Ciclo de vida de las memecoins del dataset: cuánto viven, cuánto suben, cuántas mueren.

  python -m research.lifecycle --data data
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from bot.signals import to_series


def analyze(data_dir: str) -> dict:
    d = Path(data_dir)
    index = json.loads((d / "pools.json").read_text())
    life_h, peak_x, peak_at_min, final_vs_peak, dead = [], [], [], [], 0
    for addr, meta in index.items():
        f = d / "ohlcv" / f"{addr}.json"
        if not f.exists():
            continue
        s = to_series(json.loads(f.read_text()))
        if s is None or len(s) < 10:
            continue
        c, v = s.c, s.v
        traded = np.flatnonzero(v > 0)
        life_h.append((s.ts[traded[-1]] - s.ts[0]) / 3600 if len(traded) else 0)
        first = c[min(5, len(c) - 1)]                  # precio a los ~5 min de vida
        pk = int(np.argmax(c))
        peak_x.append(c[pk] / first)
        peak_at_min.append((s.ts[pk] - s.ts[0]) / 60)
        fvp = c[-1] / c[pk]
        final_vs_peak.append(fvp)
        dead += fvp < 0.2
    n = len(life_h)
    if not n:
        return {"n": 0}
    pct = lambda a, q: round(float(np.percentile(a, q)), 2)  # noqa: E731
    return {
        "n": n,
        "life_hours_median": pct(life_h, 50), "life_hours_p90": pct(life_h, 90),
        "peak_multiple_median": pct(peak_x, 50), "peak_multiple_p90": pct(peak_x, 90),
        "peak_minute_median": pct(peak_at_min, 50),
        "pct_down_80_from_peak": round(100 * dead / n, 1),
        "final_vs_peak_median": pct(final_vs_peak, 50),
        "pct_never_2x_after_5min": round(100 * float(np.mean(np.array(peak_x) < 2)), 1),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    a = ap.parse_args()
    r = analyze(a.data)
    if not r["n"]:
        print("sin datos")
        return
    print(f"Tokens analizados: {r['n']}")
    print(f"Vida activa: mediana {r['life_hours_median']} h (p90 {r['life_hours_p90']} h)")
    print(f"Máximo respecto al precio a los 5 min: mediana x{r['peak_multiple_median']} (p90 x{r['peak_multiple_p90']}); "
          f"el máximo llega en la mediana al minuto {r['peak_minute_median']}")
    print(f"Nunca hacen x2 tras los 5 primeros minutos: {r['pct_never_2x_after_5min']}%")
    print(f"Acaban >80% por debajo de su máximo: {r['pct_down_80_from_peak']}%  (precio final/máximo mediano {r['final_vs_peak_median']})")


if __name__ == "__main__":
    main()
