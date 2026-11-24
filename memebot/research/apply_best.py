"""Genera la configuración del bot a partir del resultado del backtest.

Elige la estrategia con mejor esperanza OUT-OF-SAMPLE entre las que son positivas
tanto in-sample como out-of-sample (y con suficientes operaciones), y vuelca sus
parámetros de entrada y salida a un YAML listo para paper trading.

  python -m research.apply_best --report reports/backtest.json --out config.paper.yaml
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml


def pick(results: dict, min_oos_trades: int = 15, exclude: tuple = ("baseline",)) -> tuple[str, dict] | None:
    cands = []
    for name, r in results.items():
        if name in exclude or "in_sample" not in r:
            continue
        si, so = r["in_sample"], r["out_of_sample"]
        if si["expectancy"] > 0 and so["expectancy"] > 0 and so["n"] >= min_oos_trades:
            cands.append((so["expectancy"], name, r))
    if not cands:
        return None
    _, name, r = max(cands)
    return name, r


def build_config(base: dict, name: str, r: dict) -> dict:
    ep, xp = r["entry"], r["exit"]
    cfg = dict(base)
    cfg["mode"] = "paper"
    cfg["strategy"] = {"name": name, "params": ep, "max_ohlcv_per_cycle": 8}
    f = dict(cfg.get("filters", {}))
    if "min_age" in ep:
        f["min_age_minutes"] = min(f.get("min_age_minutes", 30), ep["min_age"])
    vol = ep.get("min_vol_60m")
    if vol:
        f["min_volume_h1_usd"] = int(vol * 0.8)   # el prefiltro no debe ser más estricto que la señal
    cfg["filters"] = f
    cfg["exits"] = {
        **cfg.get("exits", {}),
        "stop_loss_pct": xp["sl"],
        "take_profits": xp["tp"],
        "trailing_stop_pct": xp["trail"],
        "trailing_activation_pct": xp["trail_act"],
        "time_stop_minutes": xp["time"],
        "time_stop_min_gain_pct": xp["time_gain"],
    }
    return cfg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default="reports/backtest.json")
    ap.add_argument("--base", default="config.example.yaml")
    ap.add_argument("--out", default="config.paper.yaml")
    ap.add_argument("--strategy", default=None, help="forzar una estrategia concreta")
    a = ap.parse_args()
    rep = json.loads(Path(a.report).read_text())["results"]
    if a.strategy:
        choice = (a.strategy, rep[a.strategy])
    else:
        choice = pick(rep)
    if choice is None:
        print("Ninguna estrategia es positiva in-sample Y out-of-sample. NO se genera config: "
              "no hay ventaja demostrada. Recoge más datos o revisa las estrategias.")
        raise SystemExit(1)
    name, r = choice
    cfg = build_config(yaml.safe_load(Path(a.base).read_text()), name, r)
    Path(a.out).write_text("# Generado por research/apply_best.py a partir de " + a.report + "\n"
                           + yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
    so = r["out_of_sample"]
    print(f"Estrategia: {name} | OOS: {so['n']} ops, acierto {so['win_rate']}%, esperanza {so['expectancy']:+.2f}%/op, "
          f"PF {so['profit_factor']} -> {a.out}")


if __name__ == "__main__":
    main()
