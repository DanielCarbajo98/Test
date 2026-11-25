import json

from bot import joint, stocks as st


def test_joint_balance_shares_cash_between_crypto_and_stocks(tmp_path):
    (tmp_path / "status.json").write_text(json.dumps({"mode": "paper", "cash": 9000.0, "equity": 10000.0}))
    stocks = {"usd_per_eur": 1.1,
              "positions": {"SAP.DE": {"entry": 100, "price": 110, "amount": 500, "fees_eur": 0.0}},
              "closed": [{"ret": 10, "pnl_eur": 50.0}]}
    (tmp_path / "stocks.json").write_text(json.dumps(stocks))
    v = joint.crypto_view(tmp_path, 9000.0, 10000.0)
    assert abs(v["cash"] - (9000 - 500 * 1.1 + 50 * 1.1)) < 1e-6           # las acciones usan la misma liquidez
    assert abs(v["equity"] - (10000 + (50 + 50) * 1.1)) < 1e-6              # y su resultado suma al saldo
    assert abs(joint.stocks_cash_eur(tmp_path, stocks) - (9000 / 1.1 - 500 + 50)) < 1e-6
    (tmp_path / "status.json").write_text(json.dumps({"mode": "live", "cash": 50.0}))
    assert joint.stocks_cash_eur(tmp_path, stocks) is None                  # con dinero real no se mezcla


def test_stocks_do_not_buy_without_joint_cash(tmp_path):
    import numpy as np
    from tests.test_stocks import activate_all, bars
    activate_all(tmp_path)
    (tmp_path / "status.json").write_text(json.dumps({"mode": "paper", "cash": 100.0, "equity": 10000.0}))
    pull = list(np.linspace(100, 200, 260)) + [200 - 3 * i for i in range(1, 8)]
    s = st.run_once(tmp_path, ["TEST"], http=object(), fetcher=lambda h, sym: bars(pull))
    assert not s["positions"] and s["watchlist"][0]["status"] == "señal (sin liquidez)"
    (tmp_path / "status.json").write_text(json.dumps({"mode": "paper", "cash": 9000.0, "equity": 10000.0}))
    s = st.run_once(tmp_path, ["TEST"], http=object(), fetcher=lambda h, sym: bars(pull))
    assert "TEST" in s["positions"] and s["paper"]["joint"] and s["paper"]["cash"] < 9000 / 1.08
