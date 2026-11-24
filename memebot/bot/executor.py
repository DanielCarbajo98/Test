"""Ejecución de órdenes.

* PaperExecutor:   simula con cotizaciones REALES de Jupiter cuando están disponibles
                   (impacto y ruta auténticos) y, si no, con un modelo de AMM.
* JupiterExecutor: swaps reales con Jupiter Swap API v2 (verificado con la doc oficial):
    GET  https://api.jup.ag/swap/v2/order?inputMint&outputMint&amount&taker&slippageBps
         -> transaction (base64), requestId, outAmount, priceImpact (en puntos %), errorCode/errorMessage
    POST https://api.jup.ag/swap/v2/execute {signedTransaction, requestId}
         -> status ("Success"|"Failed"), signature, code, totalInputAmount, totalOutputAmount
  Cabecera: x-api-key (gratis en portal.jup.ag). Jupiter se encarga de que la
  transacción entre en bloque (priority fees / Jito) y de reintentar.
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)
JUP_V2 = "https://api.jup.ag/swap/v2"
JUP_LITE_QUOTE = "https://lite-api.jup.ag/swap/v1/quote"   # sin clave; en retirada


@dataclass
class Fill:
    qty: float          # tokens (unidades UI) comprados o vendidos
    usd: float          # USD gastados (compra) o recibidos (venta)
    price: float        # precio medio efectivo
    tx: str = ""


class Quoter:
    """Cotizaciones de Jupiter (sin firmar nada)."""

    def __init__(self, api_key: str = "", slippage_bps: int = 300, client: httpx.Client | None = None):
        self.api_key = api_key
        self.slippage_bps = slippage_bps
        self.http = client or httpx.Client(timeout=20)

    def quote(self, input_mint: str, output_mint: str, amount_raw: int) -> dict | None:
        params = {"inputMint": input_mint, "outputMint": output_mint, "amount": str(amount_raw),
                  "slippageBps": str(self.slippage_bps)}
        try:
            if self.api_key:
                r = self.http.get(f"{JUP_V2}/order", params=params, headers={"x-api-key": self.api_key})
            else:
                r = self.http.get(JUP_LITE_QUOTE, params=params)
            if r.status_code != 200:
                return None
            q = r.json()
            return q if int(q.get("outAmount") or 0) > 0 else None
        except (httpx.HTTPError, ValueError):
            return None


def impact_pct(q: dict) -> float:
    """Impacto de precio en % (positivo = en tu contra)."""
    if "priceImpact" in q and q["priceImpact"] is not None:
        return abs(float(q["priceImpact"]))
    return abs(float(q.get("priceImpactPct") or 0)) * 100


class PaperExecutor:
    def __init__(self, quote_mint: str, quote_decimals: int, quoter: Quoter | None = None,
                 fee_pct: float = 0.25, network_cost_usd: float = 0.05, haircut_pct: float = 0.5):
        self.quote_mint, self.quote_decimals = quote_mint, quote_decimals
        self.quoter = quoter
        self.fee_pct = fee_pct
        self.network_cost_usd = network_cost_usd
        self.haircut = haircut_pct / 100   # slippage extra frente a la cotización (realidad < cotización)

    @staticmethod
    def _impact(usd: float, liquidity_usd: float) -> float:
        reserve = max(liquidity_usd / 2, 1.0)
        return usd / (reserve + usd)

    def buy(self, mint: str, decimals: int, usd: float, price: float, liquidity_usd: float) -> Fill:
        q = self.quoter.quote(self.quote_mint, mint, int(usd * 10 ** self.quote_decimals)) if self.quoter else None
        if q:
            qty = int(q["outAmount"]) / 10 ** decimals * (1 - self.haircut)
            qty *= max(0.0, 1 - self.network_cost_usd / usd)
            return Fill(qty=qty, usd=usd, price=usd / qty, tx="paper:jupiter")
        net = usd * (1 - self.fee_pct / 100) - self.network_cost_usd
        eff_price = price / (1 - self._impact(net, liquidity_usd))
        qty = max(net, 0) / eff_price
        return Fill(qty=qty, usd=usd, price=usd / qty if qty else price, tx="paper:model")

    def sell(self, mint: str, decimals: int, qty: float, price: float, liquidity_usd: float) -> Fill:
        q = self.quoter.quote(mint, self.quote_mint, int(qty * 10 ** decimals)) if self.quoter else None
        if q:
            usd = int(q["outAmount"]) / 10 ** self.quote_decimals * (1 - self.haircut) - self.network_cost_usd
            usd = max(usd, 0.0)
            return Fill(qty=qty, usd=usd, price=usd / qty if qty else price, tx="paper:jupiter")
        gross = qty * price
        eff = gross * (1 - self._impact(gross, liquidity_usd))
        usd = max(eff * (1 - self.fee_pct / 100) - self.network_cost_usd, 0)
        return Fill(qty=qty, usd=usd, price=usd / qty if qty else price, tx="paper:model")

    def can_sell(self, mint: str, decimals: int) -> bool:
        if not self.quoter:
            return True
        return self.quoter.quote(mint, self.quote_mint, 10 ** decimals * 1000) is not None


class JupiterExecutor:
    def __init__(self, api_key: str, private_key_b58: str, quote_mint: str, quote_decimals: int,
                 slippage_bps: int, max_impact_pct: float = 3.0, client: httpx.Client | None = None, rpc=None):
        from solders.keypair import Keypair  # import diferido: solo en modo live

        self.kp = Keypair.from_base58_string(private_key_b58)
        self.taker = str(self.kp.pubkey())
        self.quote_mint, self.quote_decimals = quote_mint, quote_decimals
        self.slippage_bps = slippage_bps
        self.max_impact_pct = max_impact_pct
        self.http = client or httpx.Client(timeout=45, headers={"x-api-key": api_key})
        self.quoter = Quoter(api_key, slippage_bps, self.http)
        self.rpc = rpc

    def token_balance(self, mint: str) -> float | None:
        """Saldo real del token en el wallet (UI), o None si no se puede leer."""
        if self.rpc is None:
            return None
        try:
            res = self.rpc.call("getTokenAccountsByOwner", [self.taker, {"mint": mint}, {"encoding": "jsonParsed"}])
            return sum(float(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
                       for a in res["value"])
        except Exception as e:  # noqa: BLE001
            log.warning("No se pudo leer saldo de %s: %s", mint, e)
            return None

    def _order(self, input_mint: str, output_mint: str, amount_raw: int) -> dict:
        r = self.http.get(f"{JUP_V2}/order", params={
            "inputMint": input_mint, "outputMint": output_mint, "amount": str(amount_raw),
            "slippageBps": str(self.slippage_bps), "taker": self.taker})
        if r.status_code != 200:
            raise RuntimeError(f"order HTTP {r.status_code}: {r.text[:200]}")
        o = r.json()
        if not o.get("transaction"):
            raise RuntimeError(f"Jupiter no pudo construir la tx (router={o.get('router')}, "
                               f"errorCode={o.get('errorCode')}): {o.get('errorMessage') or o.get('error')}")
        return o

    def _execute(self, order: dict) -> dict:
        from solders.transaction import VersionedTransaction

        tx = VersionedTransaction.from_bytes(base64.b64decode(order["transaction"]))
        signed = VersionedTransaction(tx.message, [self.kp])
        r = self.http.post(f"{JUP_V2}/execute", json={
            "signedTransaction": base64.b64encode(bytes(signed)).decode(),
            "requestId": order["requestId"],
        })
        res = r.json() if r.content else {}
        if r.status_code != 200 or res.get("status") != "Success":
            raise RuntimeError(f"Swap fallido (code={res.get('code')}): {res.get('error') or r.text[:200]} "
                               f"sig={res.get('signature', '')}")
        return res

    def _swap(self, input_mint: str, output_mint: str, amount_raw: int, check_impact: bool) -> tuple[int, int, str]:
        order = self._order(input_mint, output_mint, amount_raw)
        imp = impact_pct(order)
        if check_impact and imp > self.max_impact_pct:
            raise RuntimeError(f"impacto de precio {imp:.2f}% > {self.max_impact_pct}%")
        res = self._execute(order)
        in_raw = int(res.get("totalInputAmount") or res.get("inputAmountResult") or amount_raw)
        out_raw = int(res.get("totalOutputAmount") or res.get("outputAmountResult") or order["outAmount"])
        return in_raw, out_raw, res.get("signature", "")

    def buy(self, mint: str, decimals: int, usd: float, price: float, liquidity_usd: float) -> Fill:
        in_raw, out_raw, sig = self._swap(self.quote_mint, mint, int(usd * 10 ** self.quote_decimals), True)
        qty = out_raw / 10 ** decimals
        spent = in_raw / 10 ** self.quote_decimals
        return Fill(qty=qty, usd=spent, price=spent / qty if qty else price, tx=sig)

    def sell(self, mint: str, decimals: int, qty: float, price: float, liquidity_usd: float) -> Fill:
        # En las ventas NO se bloquea por impacto: salir es prioritario (stop/rug).
        bal = self.token_balance(mint)
        if bal is not None:
            qty = min(qty, bal)   # evita fallar por redondeos o fees en el token
        in_raw, out_raw, sig = self._swap(mint, self.quote_mint, int(qty * 10 ** decimals), False)
        got = out_raw / 10 ** self.quote_decimals
        sold = in_raw / 10 ** decimals
        return Fill(qty=sold, usd=got, price=got / sold if sold else price, tx=sig)

    def can_sell(self, mint: str, decimals: int) -> bool:
        """Anti-honeypot: ¿existe ruta para vender?"""
        return self.quoter.quote(mint, self.quote_mint, 10 ** decimals * 1000) is not None

    def reclaim_rent(self, mint: str) -> int:
        """Cierra las cuentas de token VACÍAS de `mint` y recupera su depósito (~0,002 SOL cada una).

        Cada token comprado crea una cuenta que bloquea SOL de "rent". Con capital pequeño
        y muchas operaciones eso se come una parte enorme del saldo; aquí se devuelve.
        Devuelve cuántas cuentas se cerraron.
        """
        from solders.hash import Hash
        from solders.instruction import AccountMeta, Instruction
        from solders.message import Message
        from solders.pubkey import Pubkey
        from solders.transaction import Transaction

        if self.rpc is None:
            return 0
        res = self.rpc.call("getTokenAccountsByOwner", [self.taker, {"mint": mint}, {"encoding": "jsonParsed"}])
        owner = self.kp.pubkey()
        ixs = []
        for a in res["value"]:
            info = a["account"]["data"]["parsed"]["info"]
            if info["tokenAmount"]["amount"] != "0":
                continue                                   # nunca cerrar una cuenta con saldo
            program = Pubkey.from_string(a["account"]["owner"])   # Token o Token-2022
            ixs.append(Instruction(program, bytes([9]), [      # 9 = CloseAccount
                AccountMeta(Pubkey.from_string(a["pubkey"]), is_signer=False, is_writable=True),
                AccountMeta(owner, is_signer=False, is_writable=True),     # destino del SOL
                AccountMeta(owner, is_signer=True, is_writable=False),     # propietario
            ]))
        if not ixs:
            return 0
        bh = Hash.from_string(self.rpc.call("getLatestBlockhash", [{"commitment": "finalized"}])["value"]["blockhash"])
        tx = Transaction([self.kp], Message.new_with_blockhash(ixs, owner, bh), bh)
        self.rpc.call("sendTransaction", [base64.b64encode(bytes(tx)).decode(), {"encoding": "base64"}])
        return len(ixs)
