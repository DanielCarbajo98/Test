"""Genera una carpeta de estado de DEMOSTRACIÓN para ver el panel con datos.

  python -m research.demo_state --out state-demo
  python -m bot.dashboard --state state-demo

Los datos son sintéticos (no son resultados del bot); solo sirven para probar la interfaz.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import time
from datetime import datetime, timezone
from pathlib import Path

SYMS = ["PEPE2", "BONKY", "WIFHAT", "GROKCAT", "MOODENG", "CHILLGUY", "POPDOG", "SOLAMA", "FWOG", "MICHI",
        "GIGA", "RETARDIO", "ZEREBRO", "LUCE", "PNUT"]


def mint(rnd):
    return "".join(rnd.choice("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz") for _ in range(44))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="state-demo")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    rnd = random.Random(a.seed)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    start = 1000.0

    # operaciones cerradas en las últimas 30 h
    trades, eq_events = [], []
    t = now - 30 * 3600
    for i in range(22):
        t += rnd.randint(40, 90) * 60
        sym = rnd.choice(SYMS)
        m = mint(rnd)
        cost = round(rnd.uniform(28, 45), 2)
        r = rnd.choice([-0.26, -0.24, -0.27, -0.08, -0.04, 0.03, 0.12, 0.35, 0.62, 1.1, -0.25, 0.18])
        reason = ("stop loss -25.3%" if r <= -0.2 else "time stop (90 min, +2.1%)" if r < 0.05
                  else "take profit +100%" if r > 0.5 else "trailing stop (pico 0.00031)")
        price = rnd.uniform(0.00001, 0.002)
        qty = cost / price
        close_t = t + rnd.randint(15, 120) * 60
        trades.append([t, "BUY", sym, m, qty, cost, price, "momentum", "paper:jupiter"])
        trades.append([close_t, "SELL", sym, m, qty, cost * (1 + r), price * (1 + r), reason, "paper:jupiter"])
        eq_events.append((close_t, cost * r))
    with (out / "trades.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "side", "symbol", "mint", "qty", "usd", "price", "reason", "tx"])
        for row in sorted(trades):
            w.writerow([datetime.fromtimestamp(row[0], timezone.utc).isoformat(timespec="seconds"), *row[1:5],
                        f"{row[5]:.2f}", f"{row[6]:.10g}", row[7], row[8]])

    # curva de saldo minuto a minuto con ruido de posiciones abiertas
    eq_events.sort()
    with (out / "equity.csv").open("w") as fh:
        fh.write("ts,equity,cash\n")
        realized, k = 0.0, 0
        for ts in range(now - 30 * 3600, now, 60):
            while k < len(eq_events) and eq_events[k][0] <= ts:
                realized += eq_events[k][1]
                k += 1
            noise = rnd.gauss(0, 1.2)
            fh.write(f"{ts},{start + realized + noise:.4f},{start + realized - 80:.4f}\n")

    realized = sum(e[1] for e in eq_events)
    positions = []
    for sym, entry, px, tp, age in [("GIGA", 0.000412, 0.000871, 1, 74), ("FWOG", 0.0000213, 0.0000189, 0, 23),
                                    ("LUCE", 0.00382, 0.00411, 0, 51)]:
        cost = 40.0
        qty = cost / entry
        left = qty * (0.5 if tp else 1)
        realized_pos = cost * 0.5 * 2.0 * 0.99 if tp else 0
        value = left * px
        positions.append({"symbol": sym, "mint": mint(rnd), "pool": mint(rnd), "entry_price": entry, "price": px,
                          "qty": left, "cost": cost, "value": value, "realized": realized_pos,
                          "pnl_usd": value + realized_pos - cost, "pnl_pct": 100 * (value + realized_pos - cost) / cost,
                          "opened_at": now - age * 60, "tp_hit": tp, "peak": px * (1.18 if tp else 1.04),
                          "stop": entry if tp else entry * 0.75, "liquidity": rnd.uniform(60_000, 300_000)})
    invested = sum(p["value"] for p in positions)
    equity = start + realized + sum(p["pnl_usd"] for p in positions)
    status = {
        "ts": now, "started_at": now - 30 * 3600, "mode": "paper", "strategy": "momentum (DEMO)", "paused": False,
        "equity": equity, "cash": equity - invested, "start_equity": start, "daily_pnl": 18.4,
        "day_start_equity": equity - 18.4, "max_open": 4, "daily_loss_limit_pct": 5.0, "positions": positions,
        "last_scan": {"ts": now - 12, "discovered": 87, "with_data": 81, "prefilter": 9, "analyzed": 5, "signals": 1},
        "events": [{"ts": now - 23 * 60, "text": "🟢 BUY FWOG $40.00 @ 2.13e-05 [paper] | momentum | liq $84,120 | 1h +34% | top10 18.2% | https://dexscreener.com/solana/demo"},
                   {"ts": now - 51 * 60, "text": "🟢 BUY LUCE $40.00 @ 0.00382 [paper] | momentum | liq $212,400 | 1h +21% | top10 22.9%"},
                   {"ts": now - 60 * 60, "text": "🔴 SELL GIGA 48543 por $39.60 — take profit +100% (PnL $+19.80)"}],
        "interval": 20, "demo": True,
    }
    (out / "status.json").write_text(json.dumps(status))
    with (out / "decisions.jsonl").open("w") as fh:
        for i in range(15):
            fh.write(json.dumps({"ts": now - i * 60 - 30, "symbol": rnd.choice(SYMS), "mint": mint(rnd),
                                 "signal": i == 3, "liq": rnd.uniform(40_000, 400_000),
                                 "ret_60m": rnd.uniform(-20, 60), "accel": rnd.uniform(0.3, 2.2),
                                 "fdv": rnd.uniform(2e5, 5e6)}) + "\n")
    # gráficos de velas (posiciones + tokens analizados)
    from bot.signals import explain, latest_signal
    (out / "charts").mkdir(exist_ok=True)
    index = []
    extra_trades = []
    def series(n, p0, drift_fn, vol_fn):
        rows, p = [], p0
        t0 = (now // 60) * 60 - (n - 1) * 60
        for i in range(n):
            o = p
            p = max(p * (1 + drift_fn(i) + rnd.gauss(0, 0.012)), 1e-12)
            hi, lo = max(o, p) * (1 + abs(rnd.gauss(0, 0.006))), min(o, p) * (1 - abs(rnd.gauss(0, 0.006)))
            rows.append([t0 + 60 * i, o, hi, lo, p, vol_fn(i)])
        return rows
    for k, pos in enumerate(positions):
        age = int((now - pos["opened_at"]) / 60)
        n = 300
        e = n - age
        tgt = pos["price"] / pos["entry_price"]
        rows = series(n, pos["entry_price"] * 0.6, lambda i: (0.004 if i < e else math.log(tgt) / max(1, age)), lambda i: rnd.uniform(800, 4000))
        # alinear la vela de entrada con el precio de entrada
        scale = pos["entry_price"] / rows[e][4]
        rows = [[r[0], r[1] * scale, r[2] * scale, r[3] * scale, r[4] * scale, r[5]] for r in rows]
        sig, feat = latest_signal("momentum", rows, rows[0][0], 1e9, {})
        data = {"pool": pos["pool"], "mint": pos["mint"], "symbol": pos["symbol"], "ts": now - 20, "signal": False,
                "strategy": "momentum (DEMO)", "checks": [], "pred": None, "features": {k2: v for k2, v in feat.items() if k2 != "valid"},
                "candles": rows, "position": True}
        (out / "charts" / f"{pos['pool']}.json").write_text(json.dumps(data))
        index.append({"pool": pos["pool"], "mint": pos["mint"], "symbol": pos["symbol"], "ts": now - 20, "signal": False,
                      "position": True, "failed": 0, "checks": 0, "price": pos["price"]})
        extra_trades.append([rows[e][0] + 20, "BUY", pos["symbol"], pos["mint"], pos["cost"] / pos["entry_price"], pos["cost"], pos["entry_price"], "momentum", "paper:jupiter"])
        if pos["tp_hit"]:
            j = e + int(age * 0.6)
            extra_trades.append([rows[j][0] + 30, "SELL", pos["symbol"], pos["mint"], pos["cost"] / pos["entry_price"] / 2, pos["cost"], pos["entry_price"] * 2, "take profit +100%", "paper:jupiter"])
    for k in range(9):
        sym = SYMS[k]
        pool, m = mint(rnd), mint(rnd)
        up = k in (0, 3)
        rows = series(360, rnd.uniform(0.00002, 0.003), lambda i, up=up: (0.0035 if up and i > 300 else rnd.uniform(-0.002, 0.002)),
                      lambda i, up=up: (9000 if up and i > 300 else rnd.uniform(300, 2500)))
        sig, feat = latest_signal("momentum", rows, rows[0][0] - 3 * 3600, 3e8 / max(rows[-1][4], 1e-12) * 0.003, {})
        checks = explain("momentum", feat, {})
        data = {"pool": pool, "mint": m, "symbol": sym, "ts": now - 60 * k, "signal": sig, "strategy": "momentum (DEMO)",
                "checks": checks, "pred": None, "features": {k2: v for k2, v in feat.items() if k2 != "valid"},
                "candles": rows, "liquidity": rnd.uniform(60_000, 400_000), "fdv": feat.get("fdv")}
        (out / "charts" / f"{pool}.json").write_text(json.dumps(data))
        index.append({"pool": pool, "mint": m, "symbol": sym, "ts": now - 60 * k, "signal": sig, "position": False,
                      "failed": sum(1 for c in checks if not c["ok"]), "checks": len(checks), "price": rows[-1][4]})
    (out / "charts" / "index.json").write_text(json.dumps(index))
    with (out / "trades.csv").open("a", newline="") as fh:
        w = csv.writer(fh)
        for row in extra_trades:
            w.writerow([datetime.fromtimestamp(row[0], timezone.utc).isoformat(timespec="seconds"), *row[1:5],
                        f"{row[5]:.2f}", f"{row[6]:.10g}", row[7], row[8]])
    print(f"Estado de demostración en {out}/  ->  python -m bot.dashboard --state {out}")


if __name__ == "__main__":
    main()
