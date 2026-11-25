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


# GeckoTerminal: pocas fuentes (su cupo se reserva para las velas); Jupiter aporta el grueso del universo
DEFAULT_SOURCES = [("trending_pools", {"duration": "5m"}), ("trending_pools", {"duration": "1h"}),
                   ("pools", {"sort": "h24_tx_count_desc"})]


JUP_LISTS = [("toptrending", "5m"), ("toptrending", "1h"), ("toptrending", "6h"), ("toptrending", "24h"),
             ("toptraded", "1h"), ("toptraded", "24h"), ("toporganicscore", "1h"), ("toporganicscore", "24h")]


def jupiter_discover(api_key: str = "", client: httpx.Client | None = None) -> tuple[list[str], dict[str, dict]]:
    """Cientos de tokens activos de Solana desde las listas de Jupiter (no gasta el cupo de velas).
    Devuelve (mints, info) con info[mint] = {organic, holders, verified}."""
    http = client or httpx.Client(timeout=20)
    base, headers = (("https://api.jup.ag/tokens/v2", {"x-api-key": api_key}) if api_key
                     else ("https://lite-api.jup.ag/tokens/v2", {}))
    mints: list[str] = []
    info: dict[str, dict] = {}
    for cat, interval in JUP_LISTS:
        try:
            r = http.get(f"{base}/{cat}/{interval}", params={"limit": 100}, headers=headers)
            if r.status_code != 200 and api_key:          # clave sin acceso a esta API: probar sin clave
                r = http.get(f"https://lite-api.jup.ag/tokens/v2/{cat}/{interval}", params={"limit": 100})
            data = r.json() if r.status_code == 200 else []
        except (httpx.HTTPError, ValueError):
            continue
        for t in data or []:
            m = t.get("id")
            if not m or m in QUOTE_MINTS:
                continue
            if m not in info:
                mints.append(m)
            info[m] = {"organic": t.get("organicScore"), "holders": t.get("holderCount"),
                       "verified": bool(t.get("isVerified"))}
    return mints, info
