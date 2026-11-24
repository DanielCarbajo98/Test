"""Análisis del historial real (paper o live) de state/trades.csv.

  python -m research.report --state state

Agrupa compras y ventas por posición y calcula acierto, esperanza, profit factor,
peor operación, motivos de salida y curva de PnL. Compáralo con el backtest: si el
paper rinde mucho peor, el backtest era optimista (costes, latencia, datos).
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

from bot.history import positions, summarize


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="state")
    a = ap.parse_args()
    f = Path(a.state) / "trades.csv"
    if not f.exists():
        print("Sin operaciones todavía.")
        return
    done = positions(f)
    s = summarize(done)
    if not s["n"]:
        print("Ninguna posición cerrada todavía.")
        return
    print(f"Posiciones cerradas: {s['n']}")
    print(f"Acierto: {s['win_rate']:.1f}%  |  ganancia media {s['avg_win']:+.1f}%  |  pérdida media {s['avg_loss']:+.1f}%")
    print(f"Esperanza: {s['expectancy']:+.2f}% por operación  |  profit factor {s['profit_factor']:.2f}")
    print(f"PnL total: ${s['pnl_usd']:+.2f}  |  drawdown máx ${s['max_dd_usd']:.2f}  |  peor {s['worst']:.0f}%  mejor {s['best']:+.0f}%")
    print("Motivos de salida:", s["exit_reasons"])
    by_day = defaultdict(float)
    for d in done:
        by_day[d["closed"][:10]] += d["got"] - d["cost"]
    print("PnL por día:", {k: round(v, 2) for k, v in sorted(by_day.items())})


if __name__ == "__main__":
    main()
