"""Ciclo completo del bot con DexScreener, RPC y RugCheck simulados (sin red)."""
import httpx

from bot.config import Config
from bot.executor import Quoter
from bot.main import Bot
from bot.market import Candles
from bot.scanner import DexScreener
from bot.safety import SolanaRPC
from tests.test_logic import raw_pair

WALLET = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
PDA = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"


def make_transport(state):
    def handler(req: httpx.Request):
        url = str(req.url)
        if "token-boosts" in url or "token-profiles" in url:
            return httpx.Response(200, json=[{"chainId": "solana", "tokenAddress": "MINT"},
                                             {"chainId": "ethereum", "tokenAddress": "0xabc"}])
        if "/tokens/v1/solana/" in url:
            p = raw_pair(pairCreatedAt=state["now_ms"] - 6 * 3600_000, priceUsd=str(state["price"]))
            return httpx.Response(200, json=[p])
        if "trending_pools" in url or "/pools?" in url:
            return httpx.Response(200, json={"data": []})
        if "ohlcv/minute" in url:
            # tendencia que acelera en la última hora -> señal momentum
            t0, p, rows = state["now_ms"] // 1000 - 400 * 60, 0.0005, []
            for i in range(400):
                o = p
                p *= 1.003 if i >= 340 else 1.0005
                rows.append([t0 + 60 * i, o, p, o, p, 8000 if i >= 340 else 500])
            rows.reverse()
            return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": rows}}})
        if "quote" in url:
            q = dict(req.url.params)
            amt = int(q["amount"])
            if q["inputMint"] == "MINT":   # venta: tokens(6 dec) -> USDC(6 dec)
                out = int(amt * state["price"] * 0.99)
            else:                            # compra: USDC -> tokens
                out = int(amt / state["price"] * 0.99)
            return httpx.Response(200, json={"outAmount": str(out), "priceImpactPct": "0.004"})
        if "rugcheck" in url:
            return httpx.Response(200, json={"score_normalised": 3, "risks": [{"name": "Low LP", "level": "warn"}]})
        body = req.read()
        import json
        m = json.loads(body)["method"]
        if m == "getAccountInfo":
            info = {"decimals": 6, "supply": str(1_000_000_000 * 10**6), "mintAuthority": None, "freezeAuthority": None}
            return httpx.Response(200, json={"result": {"value": {"data": {"parsed": {"info": info}}}}})
        if m == "getTokenLargestAccounts":
            return httpx.Response(200, json={"result": {"value": [
                {"address": "vault", "uiAmount": 400_000_000}, {"address": "w1", "uiAmount": 30_000_000}]}})
        if m == "getMultipleAccounts":
            return httpx.Response(200, json={"result": {"value": [
                {"data": {"parsed": {"info": {"owner": PDA}}}}, {"data": {"parsed": {"info": {"owner": WALLET}}}}]}})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def test_full_cycle_buy_then_take_profit(tmp_path, monkeypatch):
    import time
    state = {"price": 0.001, "now_ms": int(time.time() * 1000)}
    client = httpx.Client(transport=make_transport(state))
    bot = Bot(Config(state_dir=str(tmp_path)))
    bot.dex = DexScreener(client)
    bot.safety.rpc = SolanaRPC("http://rpc", client)
    bot.safety.http = client
    bot.candles = Candles(client, min_interval=0)
    bot.exe.quoter = Quoter("", 300, client)

    bot.cycle(force_entries=True)
    assert "MINT" in bot.pf.positions
    p = bot.pf.positions["MINT"]
    assert p.cost_usd == 40 and bot.pf.cash == 1000 - p.cost_usd

    state["price"] = 0.0025      # +150%: primer take profit (vende 50%)
    bot.cycle(force_entries=True)
    p = bot.pf.positions["MINT"]
    assert p.tp_levels_hit == [0] and abs(p.qty - p.initial_qty / 2) < 1e-6
    assert bot.pf.daily_pnl() > 0

    state["price"] = 0.0009      # vuelve bajo la entrada: break-even stop cierra el resto
    bot.cycle(force_entries=True)
    assert "MINT" not in bot.pf.positions
    assert bot.pf.equity() > 1000  # el TP parcial dejó la operación en positivo
