"""Ejecución de órdenes: simulada (paper) o real vía Jupiter Swap API v2.

Flujo Jupiter v2 (comprueba los nombres exactos en developers.jup.ag antes de
usar dinero real; la API cambia a menudo):
  GET  {base}/order?inputMint&outputMint&amount&taker&slippageBps  -> transaction (base64), requestId, outAmount
  POST {base}/execute {signedTransaction, requestId}               -> status, signature, ...
Jupiter envía la transacción con su propia infraestructura (priority fees /
landing), lo que evita mantener tu propio sistema de envío y Jito.
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)
JUP_BASE = "https://api.jup.ag/swap/v2"


@dataclass
class Fill:
    qty: float          # tokens (unidades UI) comprados o vendidos
    usd: float          # USD gastados (compra) o recibidos (venta)
    price: float        # precio medio efectivo
    tx: str = ""


class PaperExecutor:
    """Simula ejecución con impacto de precio de AMM, fee y coste de red."""

    def __init__(self, fee_pct: float = 0.25, network_cost_usd: float = 0.05):
        self.fee_pct = fee_pct
        self.network_cost_usd = network_cost_usd

    @staticmethod
    def _impact(usd: float, liquidity_usd: float) -> float:
        # Pool x*y=k: la reserva del lado quote es ~liquidez/2.
        reserve = max(liquidity_usd / 2, 1.0)
        return usd / (reserve + usd)

    def buy(self, mint: str, decimals: int, usd: float, price: float, liquidity_usd: float) -> Fill:
        net = usd * (1 - self.fee_pct / 100) - self.network_cost_usd
        eff_price = price / (1 - self._impact(net, liquidity_usd))
        qty = max(net, 0) / eff_price
        return Fill(qty=qty, usd=usd, price=usd / qty if qty else price, tx="paper")

    def sell(self, mint: str, decimals: int, qty: float, price: float, liquidity_usd: float) -> Fill:
        gross = qty * price
        eff = gross * (1 - self._impact(gross, liquidity_usd))
        usd = max(eff * (1 - self.fee_pct / 100) - self.network_cost_usd, 0)
        return Fill(qty=qty, usd=usd, price=usd / qty if qty else price, tx="paper")

    def can_sell(self, mint: str, decimals: int) -> bool:
        return True


class JupiterExecutor:
    def __init__(self, api_key: str, private_key_b58: str, quote_mint: str, quote_decimals: int,
                 slippage_bps: int, client: httpx.Client | None = None):
        from solders.keypair import Keypair  # import diferido: solo en modo live

        self.kp = Keypair.from_base58_string(private_key_b58)
        self.taker = str(self.kp.pubkey())
        self.quote_mint = quote_mint
        self.quote_decimals = quote_decimals
        self.slippage_bps = slippage_bps
        self.http = client or httpx.Client(timeout=30, headers={"x-api-key": api_key})

    def _order(self, input_mint: str, output_mint: str, amount_raw: int, with_taker: bool = True) -> dict:
        params = {"inputMint": input_mint, "outputMint": output_mint, "amount": str(amount_raw),
                  "slippageBps": str(self.slippage_bps)}
        if with_taker:
            params["taker"] = self.taker
        r = self.http.get(f"{JUP_BASE}/order", params=params)
        r.raise_for_status()
        return r.json()

    def _sign_and_execute(self, order: dict) -> dict:
        from solders.transaction import VersionedTransaction

        if not order.get("transaction"):
            raise RuntimeError(f"Jupiter no devolvió transacción: {order.get('errorMessage') or order}")
        tx = VersionedTransaction.from_bytes(base64.b64decode(order["transaction"]))
        signed = VersionedTransaction(tx.message, [self.kp])
        r = self.http.post(f"{JUP_BASE}/execute", json={
            "signedTransaction": base64.b64encode(bytes(signed)).decode(),
            "requestId": order["requestId"],
        })
        r.raise_for_status()
        res = r.json()
        if res.get("status") != "Success":
            raise RuntimeError(f"Swap fallido: {res}")
        return res

    def _swap(self, input_mint: str, output_mint: str, amount_raw: int) -> tuple[int, int, str]:
        order = self._order(input_mint, output_mint, amount_raw)
        res = self._sign_and_execute(order)
        in_raw = int(res.get("inputAmountResult") or res.get("totalInputAmount") or amount_raw)
        out_raw = int(res.get("outputAmountResult") or res.get("totalOutputAmount") or order["outAmount"])
        return in_raw, out_raw, res.get("signature", "")

    def buy(self, mint: str, decimals: int, usd: float, price: float, liquidity_usd: float) -> Fill:
        in_raw, out_raw, sig = self._swap(self.quote_mint, mint, int(usd * 10 ** self.quote_decimals))
        qty = out_raw / 10 ** decimals
        spent = in_raw / 10 ** self.quote_decimals
        return Fill(qty=qty, usd=spent, price=spent / qty if qty else price, tx=sig)

    def sell(self, mint: str, decimals: int, qty: float, price: float, liquidity_usd: float) -> Fill:
        in_raw, out_raw, sig = self._swap(mint, self.quote_mint, int(qty * 10 ** decimals))
        got = out_raw / 10 ** self.quote_decimals
        sold = in_raw / 10 ** decimals
        return Fill(qty=sold, usd=got, price=got / sold if sold else price, tx=sig)

    def can_sell(self, mint: str, decimals: int) -> bool:
        """Anti-honeypot: ¿existe ruta para vender una cantidad pequeña?"""
        try:
            o = self._order(mint, self.quote_mint, 10 ** decimals, with_taker=False)
            return int(o.get("outAmount") or 0) > 0
        except Exception as e:  # noqa: BLE001
            log.info("Sin ruta de venta para %s: %s", mint, e)
            return False
