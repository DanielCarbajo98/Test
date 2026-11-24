"""Filtros anti-rug: comprobaciones on-chain + RugCheck + simulación de venta.

Ninguna comprobación garantiza nada: reduce la probabilidad de caer en los
fraudes más comunes (mint infinito, congelación de cuentas, supply concentrado,
honeypot sin ruta de venta).
"""
from __future__ import annotations

import logging

import httpx
from solders.pubkey import Pubkey

from .config import SafetyCfg
from .models import PairSnapshot, SafetyReport

log = logging.getLogger(__name__)
RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"


class SolanaRPC:
    def __init__(self, url: str, client: httpx.Client | None = None):
        self.url = url
        self.http = client or httpx.Client(timeout=15)
        self._id = 0

    def call(self, method: str, params: list, retries: int = 3):
        import time as _t
        for attempt in range(retries + 1):
            self._id += 1
            r = self.http.post(self.url, json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
            if r.status_code == 429 and attempt < retries:
                _t.sleep(1.5 * (attempt + 1))
                continue
            break
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"RPC {method}: {body['error']}")
        return body["result"]

    def mint_info(self, mint: str) -> dict:
        res = self.call("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        return res["value"]["data"]["parsed"]["info"]

    def largest_accounts(self, mint: str) -> list[dict]:
        return self.call("getTokenLargestAccounts", [mint])["value"]

    def token_account_owners(self, accounts: list[str]) -> dict[str, str]:
        res = self.call("getMultipleAccounts", [accounts, {"encoding": "jsonParsed"}])
        out = {}
        for addr, acc in zip(accounts, res["value"]):
            try:
                out[addr] = acc["data"]["parsed"]["info"]["owner"]
            except (TypeError, KeyError):
                pass
        return out


def top10_pct_excluding_pools(holders: list[dict], owners: dict[str, str], supply_ui: float, pair_address: str) -> float:
    """% de supply en los 10 mayores holders, excluyendo cuentas de pools/lockers.

    Heurística: las vaults de AMM y lockers pertenecen a PDAs (direcciones fuera
    de la curva ed25519) o a la propia dirección del par. Los wallets de personas
    están en la curva.
    """
    total = 0.0
    counted = 0
    for h in holders:
        owner = owners.get(h["address"])
        if owner:
            if owner == pair_address:
                continue
            try:
                if not Pubkey.from_string(owner).is_on_curve():
                    continue
            except ValueError:
                pass
        total += float(h.get("uiAmount") or 0)
        counted += 1
        if counted == 10:
            break
    return 100 * total / supply_ui if supply_ui else 100.0


class SafetyChecker:
    def __init__(self, cfg: SafetyCfg, rpc: SolanaRPC, http: httpx.Client | None = None, sell_probe=None):
        self.cfg = cfg
        self.rpc = rpc
        self.http = http or httpx.Client(timeout=15)
        self.sell_probe = sell_probe  # callable(mint, decimals) -> bool

    def check(self, snap: PairSnapshot) -> SafetyReport:
        rep = SafetyReport(ok=True)
        mint = snap.token_address
        try:
            info = self.rpc.mint_info(mint)
            rep.decimals = int(info["decimals"])
            if self.cfg.require_mint_revoked and info.get("mintAuthority"):
                rep.reasons.append("mint authority activa (pueden imprimir tokens)")
            if self.cfg.require_freeze_revoked and info.get("freezeAuthority"):
                rep.reasons.append("freeze authority activa (pueden congelar tu saldo)")
            supply_ui = int(info["supply"]) / 10 ** rep.decimals
            holders = self.rpc.largest_accounts(mint)
            owners = self.rpc.token_account_owners([h["address"] for h in holders])
            rep.top10_pct = round(top10_pct_excluding_pools(holders, owners, supply_ui, snap.pair_address), 1)
            if rep.top10_pct > self.cfg.max_top10_pct:
                rep.reasons.append(f"top-10 holders tienen {rep.top10_pct}% del supply")
        except Exception as e:  # noqa: BLE001 — si no podemos verificar, no compramos
            rep.reasons.append(f"no se pudo verificar on-chain: {e}")

        if self.cfg.use_rugcheck:
            try:
                r = self.http.get(RUGCHECK.format(mint=mint))
                r.raise_for_status()
                data = r.json()
                rep.rugcheck_score = data.get("score_normalised", data.get("score"))
                dangers = [x.get("name", "?") for x in data.get("risks", []) if x.get("level") == "danger"]
                if len(dangers) > self.cfg.max_rugcheck_danger:
                    rep.reasons.append("RugCheck: " + ", ".join(dangers))
            except Exception as e:  # noqa: BLE001
                rep.reasons.append(f"RugCheck no disponible: {e}")

        if self.cfg.simulate_sell and self.sell_probe and rep.decimals is not None and not rep.reasons:
            if not self.sell_probe(mint, rep.decimals):
                rep.reasons.append("sin ruta de venta (posible honeypot)")

        rep.ok = not rep.reasons
        return rep
