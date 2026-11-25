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


def test_pullback_buy_then_stop_sell(tmp_path):
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
