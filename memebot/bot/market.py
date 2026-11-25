"""Velas de 1 minuto en vivo (GeckoTerminal) para evaluar las señales."""
from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)
G = "https://api.geckoterminal.com/api/v2/networks/solana"


class Candles:
    def __init__(self, client: httpx.Client | None = None, min_interval: float = 2.1):
        self.http = client or httpx.Client(timeout=20, headers={"Accept": "application/json"})
        self.min_interval = min_interval
        self._last = 0.0

    def minute(self, pool: str, limit: int = 420, retries: int = 2) -> list[list[float]]:
        """Últimas `limit` velas de 1 min [ts,o,h,l,c,vol_usd], ascendente. 420 = 7 h (lo que usan las features)."""
        r = None
        for attempt in range(retries + 1):
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            try:
                r = self.http.get(f"{G}/pools/{pool}/ohlcv/minute",
                                  params={"aggregate": 1, "limit": limit, "currency": "usd", "token": "base"})
            except httpx.HTTPError as e:
                log.warning("GeckoTerminal falló para %s: %s", pool, e)
                return []
            if r.status_code != 429:
                break
            if attempt < retries:
                time.sleep(3 * (attempt + 1))     # límite de peticiones: esperar y reintentar
        if r is None or r.status_code == 429:
            log.info("GeckoTerminal sigue limitando; se reintenta este candidato en el próximo ciclo")
            return []
        try:
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("GeckoTerminal falló para %s: %s", pool, e)
            return []
        rows = ((r.json().get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        return sorted(([float(x) for x in row] for row in rows), key=lambda x: x[0])


QUOTE_MINTS = {"So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
               "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}


def _gecko_get(c: "Candles", path: str, params: dict) -> list[dict]:
    wait = c.min_interval - (time.time() - c._last)
    if wait > 0:
        time.sleep(wait)
    c._last = time.time()
    try:
        r = c.http.get(f"{G}/{path}", params=params)
        if r.status_code != 200:
            return []
        return r.json().get("data") or []
    except (httpx.HTTPError, ValueError):
        return []


def discover(c: "Candles", sources: list[tuple[str, dict]]) -> list[str]:
    """Mints base de pools en tendencia / más activas en GeckoTerminal (mismo universo que el backtest)."""
    mints: list[str] = []
    for path, params in sources:
        for p in _gecko_get(c, path, params):
            rel = p.get("relationships", {})
            base = ((rel.get("base_token") or {}).get("data") or {}).get("id", "").removeprefix("solana_")
            quote = ((rel.get("quote_token") or {}).get("data") or {}).get("id", "").removeprefix("solana_")
            if base and base not in QUOTE_MINTS and quote in QUOTE_MINTS and base not in mints:
                mints.append(base)
    return mints


DEFAULT_SOURCES = [("trending_pools", {"duration": "1h"}), ("trending_pools", {"duration": "6h"}),
                   ("pools", {"sort": "h24_tx_count_desc"})]
