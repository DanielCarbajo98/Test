from bot.sizing import crypto_multiplier, stock_amount


def test_more_edge_and_tighter_stop_invest_more():
    base, _ = stock_amount(10_000, 10_000, 100, 80, {"expectancy": 1.0}, True, [])      # 500 €
    better, _ = stock_amount(10_000, 10_000, 100, 80, {"expectancy": 2.0}, True, [])
    wide, _ = stock_amount(10_000, 10_000, 100, 70, {"expectancy": 1.0}, True, [])
    bear, why = stock_amount(10_000, 10_000, 100, 80, {"expectancy": 1.0}, False, [])
    assert better > base > wide and bear < base and any("bajista" in w for w in why)
    tight, why = stock_amount(10_000, 10_000, 100, 98, {"expectancy": 1.0}, True, [])
    assert tight == 1000 and any("tope" in w for w in why)      # tope 10 % del saldo


def test_limits_cash_open_risk_and_fees():
    amt, why = stock_amount(10_000, 300, 100, 95, {"expectancy": 1.0}, True, [])
    assert amt == 300 and any("liquidez" in w for w in why)
    full = [{"entry": 100, "stop": 90, "amount": 8_000}]       # 800 € ya en riesgo (8 % del saldo)
    amt, why = stock_amount(10_000, 10_000, 100, 95, {"expectancy": 1.0}, True, full)
    assert amt == 0
    amt, why = stock_amount(10_000, 10_000, 100, 95, {"expectancy": 1.0}, True, [], fixed_fee_eur=5)
    assert amt >= 500 or amt == 0                              # con 5 € de comisión, mínimo 500 €


def test_crypto_multiplier():
    m, _ = crypto_multiplier(0.06, 0.02, 10_000, 10_000)
    assert m == 1.5
    m, _ = crypto_multiplier(0.02, 0.02, 10_000, 10_000)
    assert m == 1.0
    m, why = crypto_multiplier(None, None, 8_000, 10_000, 40)
    assert m < 1 and "prudente" in why[0]
