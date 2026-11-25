import json

import numpy as np

from bot import stocks as st


def bars(closes, vol=1e6, sym="TEST"):
    c = np.array(closes, dtype=float)
    ts = 1_700_000_000 + 86400 * np.arange(len(c))
    return st.Bars(sym, "Test Corp", "USD", ts, c, c * 1.005, c * 0.995, c, np.full(len(c), vol), float(c[-1]), True)


def test_indicators():
    x = np.arange(1, 301, dtype=float)
    assert st.sma(x, 50)[-1] == np.mean(x[-50:])
    assert st.rsi(x)[-1] == 100.0                  # solo sube
    down = st.rsi(x[::-1])
    assert down[-1] < 1


def activate_all(tmp_path):
    """Simula un cerebro de acciones que ya aprendió y dio por buenas ambas estrategias."""
    from bot.stocks_brain import DEFAULT_PARAMS
    (tmp_path / "stocks_brain.json").write_text(json.dumps({"setups": {
        k: {"params": v, "active": True} for k, v in DEFAULT_PARAMS.items()}}))


def test_no_alerts_before_the_brain_has_learned(tmp_path):
    up = list(np.linspace(100, 200, 260)) + [200 - 3 * i for i in range(1, 8)]
    s = st.run_once(tmp_path, ["TEST"], http=object(), fetcher=lambda h, sym: bars(up))
    assert s["watchlist"][0]["signal"] is None and not s["positions"]


def test_pullback_buy_then_stop_sell(tmp_path):
    activate_all(tmp_path)
    # tendencia alcista larga y retroceso brusco -> RSI bajo con precio aún sobre la media de 200
    up = list(np.linspace(100, 200, 260))
    pull = up + [200 - 3 * i for i in range(1, 8)]          # 197 … 179
    data = {"TEST": bars(pull)}
    fetcher = lambda http, sym: data.get(sym)  # noqa: E731
    s = st.run_once(tmp_path, ["TEST"], http=object(), fetcher=fetcher)
    row = s["watchlist"][0]
    assert row["signal"] == "Retroceso en tendencia" and row["status"] == "compra"
    assert "TEST" in s["positions"] and s["alerts"][-1]["side"] == "COMPRA"
    # misma señal el mismo día: no repite la alerta
    s = st.run_once(tmp_path, ["TEST"], http=object(), fetcher=fetcher)
    assert sum(1 for a in s["alerts"] if a["side"] == "COMPRA") == 1
    assert "TEST" in s["positions"]                  # no se vende en el mismo retroceso que la originó
    # se hunde bajo el stop -> alerta de venta con resultado
    data["TEST"] = bars(pull + [150])
    s = st.run_once(tmp_path, ["TEST"], http=object(), fetcher=fetcher)
    assert "TEST" not in s["positions"] and s["alerts"][-1]["side"] == "VENTA"
    assert s["closed"][-1]["ret"] < 0 and s["track"]["n"] == 1
    chart = json.loads((tmp_path / "stock_charts" / "TEST.json").read_text())
    assert len(chart["candles"]) == 180 and len(chart["alerts"]) == 2


def test_no_signal_in_downtrend(tmp_path):
    down = list(np.linspace(200, 100, 267))
    s = st.run_once(tmp_path, ["X"], http=object(), fetcher=lambda h, sym: bars(down, sym="X"))
    assert s["watchlist"][0]["signal"] is None and not s["positions"]


def test_breakout_not_given_when_overextended():
    base = list(np.linspace(100, 150, 250))
    spike = base + [150] * 10 + [190]                      # salto de +27 % en un día con RSI disparado
    a = st.analyze(bars(spike))
    assert a["signal"] is None
    assert any("salto brusco" in t and not ok for t, ok in a["checks"]["Ruptura"])


def test_stock_brain_learns_and_alerts_carry_sl_tp(tmp_path):
    from bot import stocks_brain
    rng = np.random.default_rng(1)
    bars = {}
    for k in range(40):                       # acciones con tendencia alcista y retrocesos que se recuperan
        c = [100.0]
        for i in range(700):
            c.append(c[-1] * (1 + 0.0012 + rng.normal(0, 0.012) - (0.05 if i % 60 == 30 else 0)))
        c = np.array(c)
        ts = 1_600_000_000 + 86400 * np.arange(len(c))
        bars[f"S{k}"] = {"ts": ts.tolist(), "o": c.tolist(), "h": (c * 1.01).tolist(), "l": (c * 0.99).tolist(),
                         "c": c.tolist(), "v": [1e6] * len(c)}
    res = stocks_brain.run(tmp_path, bars, [])
    pull = res["setups"]["Retroceso en tendencia"]
    assert pull["in_sample"]["n"] > 0 and "stop_atr" in pull["params"] and res["lessons"]
    params, active, info = stocks_brain.params_for(tmp_path, "Retroceso en tendencia")
    assert params["stop_atr"] == pull["params"]["stop_atr"]
    # una señal con estos parámetros lleva stop loss y take profit
    up = np.linspace(100, 200, 300)
    clean = {"ts": (1_600_000_000 + 86400 * np.arange(300)).tolist(), "o": up.tolist(), "h": (up * 1.01).tolist(),
             "l": (up * 0.99).tolist(), "c": up.tolist(), "v": [1e6] * 300}
    a = st.analyze(bars_obj(clean), {"Retroceso en tendencia": (dict(params, rsi_max=101), True, info)})
    assert a["signal"] == "Retroceso en tendencia" and a["plan"]["stop"] < a["price"] < a["plan"]["tp"]


def test_live_failures_suspend_a_strategy():
    from bot import stocks_brain
    learned = {"Retroceso en tendencia": {"out_of_sample": {"expectancy": 1.5}}, "Ruptura": {}}
    closed = [{"setup": "Retroceso en tendencia", "ret": -4.0}] * 9
    fb = stocks_brain.live_feedback(learned, closed)
    assert fb["Retroceso en tendencia"]["suspended"] and not fb["Ruptura"]["suspended"]


def bars_obj(b):
    a = {k: np.array(b[k], dtype=float) for k in ("ts", "o", "h", "l", "c", "v")}
    return st.Bars("S0", "S0", "USD", a["ts"], a["o"], a["h"], a["l"], a["c"], a["v"], float(a["c"][-1]), True)


def test_paper_portfolio_counts_euros_with_fees(tmp_path):
    activate_all(tmp_path)
    up = list(np.linspace(100, 200, 260))
    pull = up + [200 - 3 * i for i in range(1, 8)]
    data = {"TEST": bars(pull)}
    s = st.run_once(tmp_path, ["TEST"], http=object(), fetcher=lambda h, sym: data.get(sym))
    assert s["paper"]["open"] == 1 and s["paper"]["invested_open"] == st.PAPER_EUR
    data["TEST"] = bars(pull + [150])                       # salta el stop
    s = st.run_once(tmp_path, ["TEST"], http=object(), fetcher=lambda h, sym: data.get(sym))
    c = s["closed"][-1]
    assert abs(c["pnl_eur"] - (st.PAPER_EUR * c["ret"] / 100 - c["fees_eur"])) < 0.01
    assert s["paper"]["closed"] == 1 and s["paper"]["realized"] == c["pnl_eur"] and s["paper"]["total"] < 0


def test_mute_switch_stops_alerts(tmp_path):
    activate_all(tmp_path)
    (tmp_path / "control.json").write_text(json.dumps({"stocks_alerts": False}))
    pull = list(np.linspace(100, 200, 260)) + [200 - 3 * i for i in range(1, 8)]
    s = st.run_once(tmp_path, ["TEST"], http=object(), fetcher=lambda h, sym: bars(pull))
    assert not s["positions"] and s["watchlist"][0]["status"] == "señal (silenciada)"
