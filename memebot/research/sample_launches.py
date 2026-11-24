"""Muestra SIN sesgo de supervivencia: registra pools nuevas en el momento en que nacen.

El universo de collect.py (tokens con mucho volumen hoy) favorece a los ganadores.
Este script apunta lanzamientos al azar ANTES de saber cómo acaban; después se
descarga su histórico y se backtestean sobre ellos las mismas estrategias.

  python -m research.sample_launches --minutes 90 --every 120   # fase 1: muestrear
  python -m research.sample_launches --fetch                    # fase 2: descargar velas (horas después)
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime
from pathlib import Path

from .collect import G, QUOTES, Gecko, _token_id, fetch_ohlcv

log = logging.getLogger("launches")


def sample(g: Gecko, out: Path, minutes: float, every: float):
    idx_file = out / "pools.json"
    index = json.loads(idx_file.read_text()) if idx_file.exists() else {}
    end = time.time() + minutes * 60
    while time.time() < end:
        try:
            data = g.get(f"{G}/new_pools", {"page": 1}).get("data", [])
        except Exception as e:  # noqa: BLE001
            log.warning("%s", e)
            data = []
        added = 0
        for p in data:
            a, rel = p["attributes"], p.get("relationships", {})
            base, quote = _token_id(rel, "base_token"), _token_id(rel, "quote_token")
            if base in QUOTES or quote not in QUOTES or a["address"] in index:
                continue
            created = datetime.fromisoformat(a["pool_created_at"].replace("Z", "+00:00"))
            index[a["address"]] = {
                "pool": a["address"], "name": a["name"], "base_mint": base, "quote_mint": quote,
                "dex": (rel.get("dex", {}).get("data") or {}).get("id", ""),
                "created_at": int(created.timestamp()), "fdv_now": float(a.get("fdv_usd") or 0),
                "price_now": float(a.get("base_token_price_usd") or 0), "reserve_now": float(a.get("reserve_in_usd") or 0),
                "sources": ["new_pools"], "sampled_at": int(time.time()), "selected_at": int(created.timestamp()),
            }
            added += 1
        idx_file.write_text(json.dumps(index, indent=1))
        log.info("+%d pools (total %d)", added, len(index))
        time.sleep(every)


def fetch(g: Gecko, out: Path):
    index = json.loads((out / "pools.json").read_text())
    (out / "ohlcv").mkdir(exist_ok=True)
    for i, (addr, meta) in enumerate(index.items(), 1):
        f = out / "ohlcv" / f"{addr}.json"
        old = json.loads(f.read_text()) if f.exists() else []
        since = int(old[-1][0]) + 60 if old else meta["created_at"]
        try:
            rows = fetch_ohlcv(g, addr, since)
            # actualizar precio/FDV actuales para estimar el supply
            info = g.get(f"{G}/pools/{addr}").get("data", {}).get("attributes", {})
        except Exception as e:  # noqa: BLE001
            log.warning("%s: %s", meta["name"], e)
            continue
        if info:
            meta["price_now"] = float(info.get("base_token_price_usd") or meta["price_now"])
            meta["fdv_now"] = float(info.get("fdv_usd") or meta["fdv_now"])
            meta["reserve_now"] = float(info.get("reserve_in_usd") or 0)
        merged = {int(r[0]): r for r in old + rows}
        f.write_text(json.dumps([merged[k] for k in sorted(merged)]))
        if i % 20 == 0:
            (out / "pools.json").write_text(json.dumps(index, indent=1))
            log.info("%d/%d", i, len(index))
    (out / "pools.json").write_text(json.dumps(index, indent=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data_launches")
    ap.add_argument("--minutes", type=float, default=90)
    ap.add_argument("--every", type=float, default=120)
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--loop", type=float, default=0, help="con --fetch: repetir cada N minutos")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    g = Gecko(min_interval=2.5)
    if not a.fetch:
        sample(g, out, a.minutes, a.every)
        return
    while True:
        t0 = time.time()
        fetch(g, out)
        if not a.loop:
            break
        time.sleep(max(0, a.loop * 60 - (time.time() - t0)))


if __name__ == "__main__":
    main()
