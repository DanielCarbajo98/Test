"""Recolector de histórico para backtesting (GeckoTerminal, gratis, sin clave).

Universo: memecoins de Solana con actividad alta en las últimas 24 h (ordenadas por
volumen y por nº de transacciones) + trending. Este criterio selecciona por
"atención", no por resultado: incluye tokens que ya hicieron rug o murieron, que es
justo lo que un backtest honesto necesita.

IMPORTANTE (sesgo de selección): elegir tokens "en tendencia ahora" significa elegir
tokens que están en su máximo. Su pasado no sirve para backtestear: diría que el
momentum siempre funciona. Por eso cada pool guarda `selected_at` (cuándo entró en
la muestra) y el backtest solo permite ENTRADAS posteriores a ese instante: datos
que no existían cuando se eligió el token.

Modo bucle (recomendado, en un VPS 24/7): cada `--loop` minutos añade pools nuevas y
sigue actualizando las que siguen vivas, acumulando datos "a futuro":
  python -m research.collect --loop 20
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

log = logging.getLogger("collect")
G = "https://api.geckoterminal.com/api/v2/networks/solana"
QUOTES = {"So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
          "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}
EXCLUDE_BASE = QUOTES | {"USDG", "USDC", "USDT", "SOL", "JitoSOL", "mSOL", "cbBTC", "WBTC", "WETH", "JUP", "JLP"}


class Gecko:
    def __init__(self, min_interval: float = 2.2):
        self.http = httpx.Client(timeout=30, headers={"Accept": "application/json"})
        self.min_interval = min_interval
        self._last = 0.0

    def get(self, url: str, params: dict | None = None) -> dict:
        for attempt in range(6):
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            try:
                r = self.http.get(url, params=params)
            except httpx.HTTPError as e:
                log.warning("red: %s", e)
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code == 429:
                time.sleep(15 * (attempt + 1))
                continue
            if r.status_code == 404:
                return {}
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"demasiados reintentos: {url}")


def _token_id(rel: dict, key: str) -> str:
    return (rel.get(key, {}).get("data") or {}).get("id", "").removeprefix("solana_")


def universe(g: Gecko, pages: int, max_age_h: float) -> dict[str, dict]:
    pools: dict[str, dict] = {}
    now = datetime.now(timezone.utc)
    sources = [("pools", {"sort": "h24_volume_usd_desc"}), ("pools", {"sort": "h24_tx_count_desc"})]
    for path, params in sources:
        for page in range(1, pages + 1):
            data = g.get(f"{G}/{path}", {**params, "page": page}).get("data", [])
            for p in data:
                _add(pools, p, now, max_age_h, f"{params['sort']}")
    for dur in ("1h", "6h", "24h"):
        for page in range(1, 4):
            for p in g.get(f"{G}/trending_pools", {"duration": dur, "page": page}).get("data", []):
                _add(pools, p, now, max_age_h, f"trending_{dur}")
    return pools


def _add(pools, p, now, max_age_h, source):
    a, rel = p["attributes"], p.get("relationships", {})
    base, quote = _token_id(rel, "base_token"), _token_id(rel, "quote_token")
    if base in QUOTES or quote not in QUOTES:
        return
    if a["name"].split(" / ")[0] in EXCLUDE_BASE:
        return
    created = datetime.fromisoformat(a["pool_created_at"].replace("Z", "+00:00"))
    age_h = (now - created).total_seconds() / 3600
    if age_h > max_age_h:
        return
    addr = a["address"]
    if addr in pools:
        pools[addr]["sources"].append(source)
        return
    pools[addr] = {
        "pool": addr, "name": a["name"], "base_mint": base, "quote_mint": quote,
        "dex": (rel.get("dex", {}).get("data") or {}).get("id", ""),
        "created_at": int(created.timestamp()), "fdv_now": float(a.get("fdv_usd") or 0),
        "price_now": float(a.get("base_token_price_usd") or 0),
        "reserve_now": float(a.get("reserve_in_usd") or 0), "sources": [source],
    }


def fetch_ohlcv(g: Gecko, pool: str, since_ts: int) -> list[list[float]]:
    """Velas de 1 min [ts, o, h, l, c, vol_usd] en orden ascendente, desde since_ts."""
    out: dict[int, list[float]] = {}
    before = None
    for _ in range(5):
        params = {"aggregate": 1, "limit": 1000, "currency": "usd", "token": "base"}
        if before:
            params["before_timestamp"] = before
        d = g.get(f"{G}/pools/{pool}/ohlcv/minute", params)
        rows = ((d.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        if not rows:
            break
        for r in rows:
            out[int(r[0])] = [float(x) for x in r]
        oldest = min(int(r[0]) for r in rows)
        if oldest <= since_ts or len(rows) < 1000:
            break
        before = oldest
    return [out[k] for k in sorted(out) if k >= since_ts]


def collect_round(g: Gecko, out: Path, pages: int, hours: float, max_pools: int, alive_hours: float = 4):
    idx_file = out / "pools.json"
    index = json.loads(idx_file.read_text()) if idx_file.exists() else {}
    now = int(time.time())
    new = universe(g, pages, hours)
    added = 0
    for addr, meta in list(new.items())[:max_pools]:
        if addr in index:
            index[addr]["sources"] = sorted(set(index[addr]["sources"]) | set(meta["sources"]))
            index[addr]["fdv_now"], index[addr]["price_now"] = meta["fdv_now"], meta["price_now"]
            continue
        meta["selected_at"] = now
        index[addr] = meta
        added += 1
    # actualizar todas las pools que siguen vivas (operaron en las últimas `alive_hours` h)
    todo = [a for a, m in index.items() if now - m.get("last_trade", now) < alive_hours * 3600]
    log.info("Ronda: %d pools nuevas, %d a actualizar, %d en el índice", added, len(todo), len(index))
    for i, addr in enumerate(todo, 1):
        meta = index[addr]
        f = out / "ohlcv" / f"{addr}.json"
        old = json.loads(f.read_text()) if f.exists() else []
        since = int(old[-1][0]) + 60 if old else meta["created_at"]
        try:
            rows = fetch_ohlcv(g, addr, since)
        except Exception as e:  # noqa: BLE001
            log.warning("%s falló: %s", meta["name"], e)
            continue
        merged = {int(r[0]): r for r in old + rows}
        data = [merged[k] for k in sorted(merged)]
        f.write_text(json.dumps(data))
        meta["collected_at"] = int(time.time())
        meta["last_trade"] = int(data[-1][0]) if data else meta["created_at"]
        if i % 20 == 0:
            idx_file.write_text(json.dumps(index, indent=1))
            log.info("%d/%d actualizadas", i, len(todo))
    idx_file.write_text(json.dumps(index, indent=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data")
    ap.add_argument("--pages", type=int, default=10)
    ap.add_argument("--hours", type=float, default=48, help="antigüedad máxima de las pools nuevas")
    ap.add_argument("--max-pools", type=int, default=400)
    ap.add_argument("--loop", type=float, default=0, help="repetir cada N minutos (0 = una vez)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    out = Path(a.out)
    (out / "ohlcv").mkdir(parents=True, exist_ok=True)
    g = Gecko()
    while True:
        t0 = time.time()
        collect_round(g, out, a.pages, a.hours, a.max_pools)
        if not a.loop:
            break
        time.sleep(max(0, a.loop * 60 - (time.time() - t0)))


if __name__ == "__main__":
    main()
