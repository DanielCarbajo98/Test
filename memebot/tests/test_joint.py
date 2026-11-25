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


def test_no_crypto_status_means_no_stock_buys_and_overspend_is_rebalanced(tmp_path):
    import numpy as np
    from tests.test_stocks import activate_all, bars
    activate_all(tmp_path)
    (tmp_path / "status.json").unlink()                      # cripto aún sin estado (o ilegible)
    pull = list(np.linspace(100, 200, 260)) + [200 - 3 * i for i in range(1, 8)]
    s = st.run_once(tmp_path, ["TEST"], http=object(), fetcher=lambda h, sym: bars(pull))
    assert not s["positions"]                                # ya no usa 10.000 € aparte
    # saldo conjunto en negativo: vende lo que peor va hasta volver a tener liquidez
    (tmp_path / "status.json").write_text(json.dumps({"mode": "paper", "cash": 1000.0, "equity": 10_000.0}))
    stocks = json.loads((tmp_path / "stocks.json").read_text())
    stocks["positions"] = {f"P{i}": {"entry": 10, "price": 10 - i * 0.1, "stop": 9, "amount": 500, "fees_eur": 0.0}
                           for i in range(4)}
    stocks["usd_per_eur"] = 1.0
    sold = st.rebalance(tmp_path, stocks)
    assert sold == ["P3", "P2", "P1"] and joint.stocks_cash_eur(tmp_path, stocks) >= 0   # las pérdidas también cuentan
