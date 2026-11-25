import json
import time
from datetime import datetime, timezone

from bot import watchdog as w


def test_madrid_time_handles_summer_and_winter():
    winter = datetime(2026, 1, 15, 20, 0, tzinfo=timezone.utc).timestamp()
    summer = datetime(2026, 7, 15, 19, 0, tzinfo=timezone.utc).timestamp()
    assert w.madrid_now(winter).hour == 21 and w.madrid_now(summer).hour == 21


def test_alerts_once_when_a_service_stops_and_when_it_recovers(tmp_path):
    sent = []
    now = time.time()
    (tmp_path / "status.json").write_text(json.dumps({"ts": now - 30 * 60}))
    memo = {}
    w.check_health(tmp_path, memo, now, sent.append)
    w.check_health(tmp_path, memo, now + 60, sent.append)          # no repite
    assert len(sent) == 1 and "bot de cripto" in sent[0]
    (tmp_path / "status.json").write_text(json.dumps({"ts": now + 100}))
    w.check_health(tmp_path, memo, now + 120, sent.append)
    assert len(sent) == 2 and sent[1].startswith("✅")


def test_daily_summary_once_a_day_at_21(tmp_path):
    (tmp_path / "status.json").write_text(json.dumps({"ts": 1, "equity": 1050, "start_equity": 1000, "mode": "paper",
                                                      "daily_pnl": 12.5, "positions": [], "strategy": "momentum"}))
    sent, memo = [], {}
    at_20 = datetime(2026, 1, 15, 19, 0, tzinfo=timezone.utc).timestamp()    # 20:00 en Madrid
    at_21 = datetime(2026, 1, 15, 20, 5, tzinfo=timezone.utc).timestamp()    # 21:05 en Madrid
    assert not w.maybe_daily_summary(tmp_path, memo, at_20, sent.append)
    assert w.maybe_daily_summary(tmp_path, memo, at_21, sent.append)
    assert not w.maybe_daily_summary(tmp_path, memo, at_21 + 600, sent.append)
    assert "RESUMEN DEL DÍA" in sent[0] and "+5.0%" in sent[0]


def test_market_radar(tmp_path):
    import httpx
    (tmp_path / "context.json").write_text(json.dumps({"fng": 25, "fng_text": "Fear", "headlines": [
        {"title": "Bitcoin ETF inflows surge", "score": 0.6}, {"title": "meh", "score": 0.0}]}))
    (tmp_path / "stocks.json").write_text(json.dumps({"market_up": True}))
    cg = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
        "bitcoin": {"usd": 90000, "usd_24h_change": 1.5}, "solana": {"usd": 150, "usd_24h_change": -2}})))
    txt = "\n".join(w.market_radar(tmp_path, cg))
    assert "Miedo y codicia: 25" in txt and "BTC 90,000 $ (+1.5%)" in txt and "SOL 150 $ (-2.0%)" in txt
    assert "alcista" in txt and "ETF inflows" in txt and "meh" not in txt
