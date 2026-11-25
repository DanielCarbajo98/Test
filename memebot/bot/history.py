"""Lectura del historial de operaciones (state/trades.csv), compartida por el panel y los informes."""
from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path


def positions(trades_csv: Path) -> list[dict]:
    rows = list(csv.DictReader(trades_csv.open()))
    open_by_mint: dict[str, dict] = {}
    done = []
    for r in rows:
        m = r["mint"]
        fill = {"ts": r["ts"], "side": r["side"], "price": float(r["price"] or 0), "qty": float(r["qty"] or 0),
                "usd": float(r["usd"] or 0), "reason": r["reason"]}
        if r["side"] == "BUY":
            open_by_mint[m] = {"symbol": r["symbol"], "mint": m, "opened": r["ts"], "cost": float(r["usd"]),
                               "qty": float(r["qty"]), "got": 0.0, "sold": 0.0, "reasons": [],
                               "buy_price": float(r["price"] or 0), "fills": [fill]}
        elif m in open_by_mint:
            p = open_by_mint[m]
            p["fills"].append(fill)
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
