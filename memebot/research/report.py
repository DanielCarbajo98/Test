"""Análisis del historial real (paper o live) de state/trades.csv.

  python -m research.report --state state

Agrupa compras y ventas por posición y calcula acierto, esperanza, profit factor,
peor operación, motivos de salida y curva de PnL. Compáralo con el backtest: si el
paper rinde mucho peor, el backtest era optimista (costes, latencia, datos).
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from pathlib import Path


def positions(trades_csv: Path) -> list[dict]:
    rows = list(csv.DictReader(trades_csv.open()))
    open_by_mint: dict[str, dict] = {}
    done = []
    for r in rows:
        m = r["mint"]
        if r["side"] == "BUY":
            open_by_mint[m] = {"symbol": r["symbol"], "opened": r["ts"], "cost": float(r["usd"]),
                               "qty": float(r["qty"]), "got": 0.0, "sold": 0.0, "reasons": []}
        elif m in open_by_mint:
            p = open_by_mint[m]
            p["got"] += float(r["usd"])
            p["sold"] += float(r["qty"])
            p["reasons"].append(r["reason"])
            if p["sold"] >= p["qty"] * 0.999:
                p["closed"] = r["ts"]
                p["ret"] = p["got"] / p["cost"] - 1
                done.append(open_by_mint.pop(m))
    return done


def summarize(done: list[dict]) -> dict:
    if not done:
        return {"n": 0}
    r = [d["ret"] for d in done]
    pnl = [d["got"] - d["cost"] for d in done]
    wins = [x for x in r if x > 0]
    losses = [x for x in r if x <= 0]
    gross_win = sum(p for p in pnl if p > 0)
    gross_loss = -sum(p for p in pnl if p <= 0)
    cum, peak, dd = 0.0, 0.0, 0.0
    for p in pnl:
        cum += p
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    reasons = Counter(d["reasons"][-1].split(" ")[0] if d["reasons"] else "?" for d in done)
    return {
        "n": len(r), "win_rate": 100 * len(wins) / len(r),
        "avg_win": 100 * sum(wins) / len(wins) if wins else 0,
        "avg_loss": 100 * sum(losses) / len(losses) if losses else 0,
        "expectancy": 100 * sum(r) / len(r),
        "profit_factor": gross_win / gross_loss if gross_loss else float("inf"),
        "pnl_usd": sum(pnl), "max_dd_usd": dd, "worst": 100 * min(r), "best": 100 * max(r),
        "exit_reasons": dict(reasons),
    }


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
