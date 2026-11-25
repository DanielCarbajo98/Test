"""Analista Claude con un cliente simulado (sin llamadas reales ni coste)."""
import json
import time
from types import SimpleNamespace as NS

import httpx

from bot.analyst import Analyst


class FakeClient:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        body = self.payloads.pop(0) if self.payloads else {"approve": True, "confidence": 0.5, "reason": "ok"}
        return NS(content=[NS(type="text", text=json.dumps(body))], stop_reason="end_turn", model=kw["model"],
                  usage=NS(input_tokens=5000, output_tokens=800, cache_creation_input_tokens=0,
                           cache_read_input_tokens=0, server_tool_use=NS(web_search_requests=2)))


REVIEW = {"regime": "desfavorable", "risk_multiplier": 1.7, "summary": "Miedo extremo.", "crypto_view": "-",
          "stocks_view": "-", "avoid_tokens": ["BAD"], "watch_tokens": [], "position_actions": [],
          "stock_ideas": [{"symbol": "AAPL", "action": "mantener", "reason": "tendencia"}], "news_highlights": []}


def test_review_is_saved_clamped_and_costed(tmp_path):
    fc = FakeClient([REVIEW])
    an = Analyst(str(tmp_path), client=fc)
    r = an.review()
    assert r["risk_multiplier"] == 1.0                     # nunca puede AUMENTAR el riesgo
    kw = fc.calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["fallbacks"] == "default"
    assert kw["output_config"]["format"]["type"] == "json_schema" and kw["tools"][0]["type"] == "web_search_20260209"
    data = json.loads((tmp_path / "analyst.json").read_text())
    assert data["review"]["regime"] == "desfavorable" and data["usage"]["calls_today"] == 1
    assert 0 < data["usage"]["cost_today"] < 1


def test_daily_budget_cap(tmp_path):
    an = Analyst(str(tmp_path), client=FakeClient([REVIEW, REVIEW, REVIEW]), max_calls_per_day=2)
    assert an.review() and an.review()
    assert an.review() is None                             # tope alcanzado: no gasta más


def test_veto_is_tracked_and_measured(tmp_path):
    an = Analyst(str(tmp_path), client=FakeClient([{"approve": False, "confidence": 0.8, "reason": "parabólica"}]))
    out = an.check_trade({"symbol": "X", "mint": "M", "price": 1.0})
    assert out["approve"] is False
    data = json.loads((tmp_path / "analyst.json").read_text())
    data["decisions"][0]["ts"] -= 3 * 3600
    (tmp_path / "analyst.json").write_text(json.dumps(data))
    an.record_outcomes(lambda mint: 0.7)                   # cayó un 30 %: el veto acertó
    t = json.loads((tmp_path / "analyst.json").read_text())["track"]
    assert t["vetos_medidos"] == 1 and t["acierto_pct"] == 100.0


def test_bot_respects_veto_avoid_and_multiplier(tmp_path):
    from tests.test_cycle import make_transport
    from bot.config import Config
    from bot.executor import Quoter
    from bot.main import Bot
    from bot.market import Candles
    from bot.safety import SolanaRPC
    from bot.scanner import DexScreener

    def make_bot(payloads):
        state = {"price": 0.001, "now_ms": int(time.time() * 1000)}
        client = httpx.Client(transport=make_transport(state))
        cfg = Config(state_dir=str(tmp_path))
        cfg.scanner.include_jupiter = False
        bot = Bot(cfg)
        bot.dex, bot.candles = DexScreener(client), Candles(client, min_interval=0)
        bot.safety.rpc, bot.safety.http = SolanaRPC("http://rpc", client), client
        bot.exe.quoter = Quoter("", 300, client)
        bot.analyst._client = FakeClient(payloads)
        return bot

    # 1) Claude veta -> no compra
    bot = make_bot([{"approve": False, "confidence": 0.9, "reason": "noticia negativa"}])
    bot.cycle(force_entries=True)
    assert not bot.pf.positions
    # 2) revisión con riesgo x0.5 y aprobación -> compra la MITAD
    for f in tmp_path.iterdir():
        if f.is_file():
            f.unlink()
    bot = make_bot([{"approve": True, "confidence": 0.7, "reason": "ok"}])
    (tmp_path / "analyst.json").write_text(json.dumps({"review": {**REVIEW, "risk_multiplier": 0.5, "avoid_tokens": [],
                                                                  "ts": int(time.time()), "id": "r1"}}))
    bot.cycle(force_entries=True)
    assert bot.pf.positions["MINT"].cost_usd == 20      # 40 $ x 0,5
    # 3) Claude pide cerrar la posición -> se cierra
    data = json.loads((tmp_path / "analyst.json").read_text())
    data["review"].update({"id": "r2", "position_actions": [{"mint": "MINT", "action": "cerrar", "reason": "riesgo"}]})
    (tmp_path / "analyst.json").write_text(json.dumps(data))
    bot.cycle()
    assert "MINT" not in bot.pf.positions
