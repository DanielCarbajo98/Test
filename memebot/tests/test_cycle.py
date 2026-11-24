"""Ciclo completo del bot con DexScreener, RPC y RugCheck simulados (sin red)."""
import httpx

from bot.config import Config
from bot.main import Bot
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

    bot.cycle()
    assert "MINT" in bot.pf.positions
    p = bot.pf.positions["MINT"]
    assert 39 <= p.cost_usd <= 40 and bot.pf.cash == 1000 - p.cost_usd

    state["price"] = 0.0025      # +150%: primer take profit (vende 50%)
    bot.cycle()
    p = bot.pf.positions["MINT"]
    assert p.tp_levels_hit == [0] and abs(p.qty - p.initial_qty / 2) < 1e-6
    assert bot.pf.daily_pnl() > 0

    state["price"] = 0.0009      # vuelve bajo la entrada: break-even stop cierra el resto
    bot.cycle()
    assert "MINT" not in bot.pf.positions
    assert bot.pf.equity() > 1000  # el TP parcial dejó la operación en positivo
