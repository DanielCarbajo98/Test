"""Descubrimiento de candidatos y precios vía la API pública de DexScreener."""
from __future__ import annotations

import logging
import time

import httpx

from .models import PairSnapshot

log = logging.getLogger(__name__)
BASE = "https://api.dexscreener.com"
CHAIN = "solana"


class DexScreener:
    def __init__(self, client: httpx.Client | None = None):
        self.http = client or httpx.Client(timeout=15, headers={"Accept": "application/json"})

    def _get(self, path: str):
        r = self.http.get(BASE + path)
        r.raise_for_status()
        return r.json()

    def latest_token_addresses(self, include_boosted: bool, include_profiles: bool) -> tuple[list[str], set[str]]:
        """Tokens con perfil/boost recientes. Devuelve (direcciones, conjunto_boosted)."""
        addrs: list[str] = []
        boosted: set[str] = set()
        sources = []
        if include_boosted:
            sources += [("/token-boosts/latest/v1", True), ("/token-boosts/top/v1", True)]
        if include_profiles:
            sources.append(("/token-profiles/latest/v1", False))
        for path, is_boost in sources:
            try:
                for item in self._get(path) or []:
                    if item.get("chainId") != CHAIN:
                        continue
                    a = item.get("tokenAddress")
                    if a and a not in addrs:
                        addrs.append(a)
                    if a and is_boost:
                        boosted.add(a)
            except httpx.HTTPError as e:
                log.warning("DexScreener %s falló: %s", path, e)
        return addrs, boosted

    def best_pairs(self, token_addresses: list[str], boosted: set[str] | None = None) -> dict[str, PairSnapshot]:
        """Para cada token, el par con más liquidez. Máx. 30 direcciones por llamada."""
        boosted = boosted or set()
        out: dict[str, PairSnapshot] = {}
        now_ms = int(time.time() * 1000)
        for i in range(0, len(token_addresses), 30):
            chunk = token_addresses[i:i + 30]
            try:
                pairs = self._get(f"/tokens/v1/{CHAIN}/{','.join(chunk)}") or []
            except httpx.HTTPError as e:
                log.warning("DexScreener tokens falló: %s", e)
                continue
            for p in pairs:
                try:
                    snap = PairSnapshot.from_dexscreener(p, now_ms, boosted=p["baseToken"]["address"] in boosted)
                except (KeyError, TypeError):
                    continue
                if snap.token_address not in chunk:
                    continue  # el token aparece como quote; ignorar
                cur = out.get(snap.token_address)
                if cur is None or snap.liquidity_usd > cur.liquidity_usd:
                    out[snap.token_address] = snap
        return out
