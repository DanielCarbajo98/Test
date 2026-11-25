import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from bot.dashboard import make_handler
from research.demo_state import main as demo_main


def _serve(state, token=""):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state, token))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _req(url, data=None, headers=None):
    r = urllib.request.Request(url, data=json.dumps(data).encode() if data else None, headers=headers or {},
                               method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_state_api_and_controls(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.argv", ["x", "--out", str(tmp_path)])
    demo_main()
    httpd, base = _serve(tmp_path)
    try:
        code, html = _req(base + "/")
        assert code == 200 and b"MemeBot" in html
        code, body = _req(base + "/api/state")
        st = json.loads(body)
        assert st["online"] and st["stats"]["n"] == 22 and len(st["status"]["positions"]) == 3 and st["equity"]
        code, _ = _req(base + "/api/control", {"action": "pause"})
        assert code == 200 and json.loads((tmp_path / "control.json").read_text())["paused"] is True
        code, _ = _req(base + "/api/control", {"action": "close_all"})
        assert json.loads((tmp_path / "control.json").read_text())["close_all"] is True
        assert _req(base + "/api/control", {"action": "rm -rf"})[0] == 400
    finally:
        httpd.shutdown()


def test_token_required(tmp_path):
    httpd, base = _serve(tmp_path, token="secreto")
    try:
        assert _req(base + "/api/state")[0] == 401
        assert _req(base + "/api/state?token=secreto")[0] == 200
        assert _req(base + "/api/control", {"action": "pause"})[0] == 401
    finally:
        httpd.shutdown()


def test_trade_detail_chart_and_value_now(tmp_path):
    import csv
    import httpx
    from bot.dashboard import trade_detail, _TRADE_CACHE
    _TRADE_CACHE.clear()
    with (tmp_path / "trades.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "side", "symbol", "mint", "qty", "usd", "price", "reason", "tx"])
        w.writerow(["2026-11-25T01:00:00+00:00", "BUY", "MEME", "MINT1", "1000", "40", "0.04", "momentum", "paper"])
        w.writerow(["2026-11-25T02:00:00+00:00", "SELL", "MEME", "MINT1", "500", "40", "0.08", "take profit +100%", "paper"])
        w.writerow(["2026-11-25T03:00:00+00:00", "SELL", "MEME", "MINT1", "500", "20", "0.04", "break-even stop", "paper"])

    def handler(req):
        if "dexscreener" in str(req.url):
            return httpx.Response(200, json=[{"baseToken": {"address": "MINT1"}, "pairAddress": "POOL1",
                                              "priceUsd": "0.12", "liquidity": {"usd": 90000}}])
        rows = [[1795000000 + 60 * i, 0.04, 0.05, 0.03, 0.045, 100] for i in range(50)]
        return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": rows}}})

    d = trade_detail(tmp_path, "MINT1", client=httpx.Client(transport=httpx.MockTransport(handler)))
    t = d["trade"]
    assert t["mint"] == "MINT1" and len(t["fills"]) == 3 and t["got"] == 60
    assert d["pool"] == "POOL1" and d["price_now"] == 0.12 and len(d["candles"]) == 50
    assert t["qty"] * d["price_now"] == 120          # mantenerlo valdría 120 $ frente a los 60 $ obtenidos
    assert trade_detail(tmp_path, "OTRO") is None
    # GeckoTerminal saturado: usa las velas guardadas en disco en la consulta anterior
    _TRADE_CACHE.clear()

    def busy(req):
        if "dexscreener" in str(req.url):
            return handler(req)
        return httpx.Response(429)
    import bot.dashboard as dash
    sleep = dash.time.sleep
    dash.time.sleep = lambda s: None
    try:
        d2 = trade_detail(tmp_path, "MINT1", client=httpx.Client(transport=httpx.MockTransport(busy)))
        assert len(d2["candles"]) == 50 and d2["source"] == "cache"
        (tmp_path / "trade_charts" / "MINT1.json").unlink()          # sin caché: usa el gráfico guardado por el bot
        (tmp_path / "charts").mkdir()
        (tmp_path / "charts" / "POOL1.json").write_text(json.dumps({"mint": "MINT1", "ts": 1, "candles": [[1, 1, 1, 1, 1, 1]] * 7}))
        _TRADE_CACHE.clear()
        d3 = trade_detail(tmp_path, "MINT1", client=httpx.Client(transport=httpx.MockTransport(busy)))
        assert len(d3["candles"]) == 7 and d3["source"] == "bot"
    finally:
        dash.time.sleep = sleep


def test_api_is_valid_json_even_with_infinite_profit_factor(tmp_path):
    import csv
    with (tmp_path / "trades.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "side", "symbol", "mint", "qty", "usd", "price", "reason", "tx"])
        w.writerow(["2026-11-25T01:00:00+00:00", "BUY", "A", "M1", "10", "40", "4", "x", "paper"])
        w.writerow(["2026-11-25T02:00:00+00:00", "SELL", "A", "M1", "10", "60", "6", "take profit", "paper"])
    httpd, base = _serve(tmp_path)
    try:
        code, body = _req(base + "/api/state")
        assert b"Infinity" not in body and b"NaN" not in body
        assert json.loads(body)["stats"]["profit_factor"] is None
    finally:
        httpd.shutdown()
