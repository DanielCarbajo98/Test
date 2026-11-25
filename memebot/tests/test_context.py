import time

import numpy as np

from bot import context as c
from bot.brain import CTX_FEATURES, MISSING, context_columns, feature_matrix


def test_headline_scoring_and_critical_alerts():
    assert c.score_headline("Bitcoin price surges to record high")["score"] > 0
    neg = c.score_headline("Solana outage halts network for hours")
    assert neg["score"] < 0 and neg["solana"] and neg["critical"]
    assert not c.score_headline("Exchange hack drains $10M in Ethereum")["critical"]   # no es Solana


def test_risk_off_rules():
    now = int(time.time())
    assert c.risk_off({"sol_h1": -6, "sol_h24": -2, "alerts": []}, 5, 15, True).startswith("SOL cae")
    assert c.risk_off({"sol_h1": -1, "sol_h24": -16, "alerts": []}, 5, 15, True).startswith("SOL cae")
    alert = {"sol_h1": 0, "sol_h24": 0, "alerts": [{"title": "Jupiter exploit", "ts": now}]}
    assert c.risk_off(alert, 5, 15, True).startswith("alerta")
    assert c.risk_off(alert, 5, 15, False) == ""
    assert c.risk_off({}, 5, 15, True) == ""


def test_context_aligned_by_time_without_lookahead():
    hist_ts = np.array([1000, 2000])
    rows = [{"ts": 1000, "fng": 20, "sol_h1": -1, "sol_h24": -3, "news_score": -0.5},
            {"ts": 2000, "fng": 80, "sol_h1": 2, "sol_h24": 5, "news_score": 0.5}]
    out = context_columns(np.array([500, 1500, 2500, 20000]), hist_ts, rows)
    assert np.isnan(out[0]).all()                 # antes del primer dato: desconocido
    assert out[1][0] == 20 and out[2][0] == 80    # usa el último dato ANTERIOR (nunca el futuro)
    assert np.isnan(out[3]).all()                 # dato demasiado viejo (> 2 h)


def test_feature_matrix_marks_missing_context():
    f = {k: np.array([1.0, 2.0]) for k in ("age_min", "vol_5m", "vol_60m", "ret_5m", "ret_60m", "ret_6h",
                                          "drawdown_6h", "price", "active_60m", "accel", "fdv")}
    f["high_prev_3h"] = np.array([np.inf, 1.0])
    X = feature_matrix(f, np.array([0, 1]))
    assert X.shape[1] == 11 + len(CTX_FEATURES)
    assert (X[:, -len(CTX_FEATURES):] == MISSING).all() and np.isfinite(X).all()


def test_small_solana_project_problems_do_not_trigger_the_brake():
    assert not c.score_headline("Solana DEX Velocity halts trading over bug just months after relaunch")["critical"]
    assert c.score_headline("Solana network outage halts block production")["critical"]
    assert c.score_headline("Jupiter exchange hit by exploit, users drained")["critical"]


def test_alert_messages_say_crypto_and_profit_or_loss():
    from types import SimpleNamespace as NS
    from bot.main import Bot
    win = Bot._sell_msg("MEME", NS(usd=80.0, price=0.002), "take profit +100%", 40.0, final=False)
    loss = Bot._sell_msg("MEME", NS(usd=30.0, price=0.0007), "stop loss -25%", -10.0, final=True)
    assert win.startswith("🪙 CRIPTO · ✅ VENTA PARCIAL CON BENEFICIO") and "+$40.00" in win
    assert loss.startswith("🪙 CRIPTO · ❌ CIERRE CON PÉRDIDA") and "−$10.00" in loss
