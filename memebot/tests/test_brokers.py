from bot import brokers, stocks_brain


def test_regions_and_costs():
    assert brokers.region("ACS.MC") == "EU" and brokers.region("SAP_DE") == "EU"
    assert brokers.region("AAPL") == "US" and brokers.region("HSBA.L") == "OTRA"
    tr_buy, tr_sell = brokers.costs("SAP.DE", 100, "trade_republic")
    assert abs(tr_sell - (0.001 + 0.01)) < 1e-9                 # 1 € con 100 € = 1 %
    x_buy, x_sell = brokers.costs("ACS.MC", 100, "xtb")
    assert abs(x_buy - (0.001 + 0.002)) < 1e-9 and abs(x_sell - 0.001) < 1e-9   # impuesto español solo al comprar
    assert brokers.costs("AAPL", 100, "xtb")[1] > brokers.costs("SAP.DE", 100, "xtb")[1]   # cambio de divisa
    assert brokers.broker_key("no-existe") == brokers.DEFAULT_BROKER


def test_alerts_only_in_regions_where_the_strategy_wins(tmp_path):
    import json
    import numpy as np
    from bot import stocks as st
    from tests.test_stocks import bars
    (tmp_path / "stocks_brain.json").write_text(json.dumps({"setups": {
        k: {"params": v, "active": True, "regions": ["EU"]} for k, v in stocks_brain.DEFAULT_PARAMS.items()}}))
    (tmp_path / "status.json").write_text(json.dumps({"mode": "paper", "cash": 10_000.0, "equity": 10_000.0}))
    pull = list(np.linspace(100, 200, 260)) + [200 - 3 * i for i in range(1, 8)]
    s = st.run_once(tmp_path, ["TEST", "TEST.DE"], http=object(), fetcher=lambda h, sym: bars(pull))
    assert set(s["positions"]) == {"TEST.DE"}                   # la de EE. UU. queda bloqueada
    assert s["positions"]["TEST.DE"]["fees_eur"] >= 0


def test_brain_learned_with_old_costs_is_paused_and_relearned(tmp_path, monkeypatch):
    import json
    import time
    from bot import stocks as st
    old = {"ts": int(time.time()), "meta": {"stocks": 147}, "setups": {
        k: {"params": v, "active": True} for k, v in stocks_brain.DEFAULT_PARAMS.items()}}
    (tmp_path / "stocks_brain.json").write_text(json.dumps(old))
    assert stocks_brain.params_for(tmp_path, "Ruptura")[1] is False          # no alerta con lo aprendido antes
    called = []
    monkeypatch.setattr(stocks_brain, "run", lambda *a: called.append(1) or {})
    (tmp_path / "stock_bars").mkdir()
    for k in range(3):
        (tmp_path / "stock_bars" / f"S{k}.json").write_text("{}")
    st.learn_daily(tmp_path, {}, min_stocks=1)
    assert called                                                           # reaprende aunque no hayan pasado 24 h
