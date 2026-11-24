import time

from bot.config import ExitCfg, FilterCfg, RiskCfg, load_config
from bot.executor import PaperExecutor
from bot.models import PairSnapshot, Position
from bot.portfolio import Portfolio
from bot.risk import can_open, evaluate_exit, position_size_usd
from bot.safety import top10_pct_excluding_pools
from bot.strategy import evaluate, score

NOW_MS = 1_800_000_000_000


def raw_pair(**over):
    p = {
        "chainId": "solana", "dexId": "raydium", "pairAddress": "PAIR",
        "baseToken": {"address": "MINT", "symbol": "MEME"},
        "priceUsd": "0.001", "liquidity": {"usd": 120_000}, "fdv": 900_000, "marketCap": 900_000,
        "pairCreatedAt": NOW_MS - 6 * 3600_000,
        "volume": {"m5": 25_000, "h1": 200_000, "h6": 600_000, "h24": 1_500_000},
        "txns": {"m5": {"buys": 140, "sells": 80}, "h1": {"buys": 1100, "sells": 800}},
        "priceChange": {"m5": 2.5, "h1": 28, "h6": 40, "h24": 120},
    }
    p.update(over)
    return p


def snap(**over):
    return PairSnapshot.from_dexscreener(raw_pair(**over), NOW_MS)


def pos(**over):
    d = dict(token_address="MINT", symbol="MEME", decimals=6, entry_price=1.0, qty=100.0, initial_qty=100.0,
             cost_usd=100.0, realized_usd=0.0, entry_liquidity=100_000, opened_at=time.time(), peak_price=1.0)
    d.update(over)
    return Position(**d)


# ---------------------------------------------------------------- estrategia
def test_parse_and_good_candidate_passes():
    s = snap()
    assert s.age_minutes == 360 and s.buys_m5 == 140
    sig = evaluate(s, FilterCfg())
    assert sig.passed, sig.reasons
    assert sig.score >= 60


def test_rejects_too_new_parabolic_and_dumping():
    f = FilterCfg()
    assert not evaluate(snap(pairCreatedAt=NOW_MS - 5 * 60_000), f).passed
    assert not evaluate(snap(priceChange={"m5": 1, "h1": 400, "h6": 500}), f).passed
    assert not evaluate(snap(priceChange={"m5": -12, "h1": 20, "h6": 30}), f).passed
    assert not evaluate(snap(liquidity={"usd": 8_000}), f).passed
    assert not evaluate(snap(dexId="unknowndex"), f).passed


def test_boost_penalised():
    assert score(PairSnapshot.from_dexscreener(raw_pair(), NOW_MS, boosted=True)) < score(snap())


# ---------------------------------------------------------------- riesgo
def test_position_size_caps():
    r = RiskCfg()
    # riesgo: 1000*1%/25% = 40 ; tope 5% = 50 ; pool 1% de 120k = 1200
    assert position_size_usd(1000, 1000, 120_000, 25, r) == 40
    # pool pequeña limita: 1% de 2000 = 20
    assert position_size_usd(1000, 1000, 2_000, 25, r) == 20
    # sin caja
    assert position_size_usd(1000, 3, 120_000, 25, r) == 3


def test_kill_switch():
    r = RiskCfg()
    assert can_open(0, -10, 1000, r)[0]
    assert not can_open(0, -50, 1000, r)[0]
    assert not can_open(4, 0, 1000, r)[0]


def test_stop_loss_and_liquidity_rug():
    e = ExitCfg()
    assert "stop loss" in evaluate_exit(pos(), 0.74, 100_000, time.time(), e).reason
    d = evaluate_exit(pos(), 1.1, 50_000, time.time(), e)
    assert d.sell_qty == 100 and "rug" in d.reason


def test_take_profit_ladder_then_breakeven():
    e = ExitCfg()
    p = pos()
    d = evaluate_exit(p, 2.05, 100_000, time.time(), e)
    assert d.sell_qty == 50 and d.new_tp_levels == [0]
    p.qty, p.tp_levels_hit = 50, d.new_tp_levels
    # vuelve al precio de entrada: stop en break-even
    p.peak_price = 1.2  # evita que salte antes el trailing
    d = evaluate_exit(p, 0.99, 100_000, time.time(), e)
    assert d.sell_qty == 50 and "break-even" in d.reason


def test_trailing_stop_and_time_stop():
    e = ExitCfg()
    p = pos(peak_price=1.8)
    assert "trailing" in evaluate_exit(p, 1.2, 100_000, time.time(), e).reason
    old = pos(opened_at=time.time() - 120 * 60)
    assert "time stop" in evaluate_exit(old, 1.02, 100_000, time.time(), e).reason
    assert evaluate_exit(pos(), 1.05, 100_000, time.time(), e) is None


# ---------------------------------------------------------------- seguridad
def test_top10_excludes_pda_owned_pool_vaults():
    # Owner fuera de curva (PDA) = vault de AMM -> se excluye
    pda_owner = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"  # authority Raydium AMM v4
    wallet = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
    holders = [{"address": "vault", "uiAmount": 600}, {"address": "w1", "uiAmount": 100},
               {"address": "w2", "uiAmount": 50}]
    owners = {"vault": pda_owner, "w1": wallet, "w2": wallet}
    assert top10_pct_excluding_pools(holders, owners, 1000, "PAIR") == 15.0
    # el par como owner también se excluye
    owners2 = {"vault": "PAIR", "w1": wallet, "w2": wallet}
    assert top10_pct_excluding_pools(holders, owners2, 1000, "PAIR") == 15.0


# ---------------------------------------------------------------- paper + cartera
def test_paper_roundtrip_loses_fees(tmp_path):
    exe = PaperExecutor()
    b = exe.buy("MINT", 6, 40, 0.001, 120_000)
    s = exe.sell("MINT", 6, b.qty, 0.001, 120_000)
    assert b.price > 0.001  # pagas impacto + fee
    assert 38 < s.usd < 40   # ida y vuelta a mismo precio cuesta dinero

    pf = Portfolio(str(tmp_path), 1000)
    p = pos(entry_price=b.price, qty=b.qty, initial_qty=b.qty, cost_usd=b.usd, peak_price=b.price)
    pf.open(p, "test", "paper")
    assert pf.cash == 960
    pnl = pf.reduce("MINT", b.qty, s.usd, s.price, "test", "paper")
    assert pnl < 0 and "MINT" not in pf.positions
    assert Portfolio(str(tmp_path), 0).cash == pf.cash  # persistido
    assert (tmp_path / "trades.csv").read_text().count("\n") == 3


def test_example_config_loads(monkeypatch):
    monkeypatch.chdir(__file__.rsplit("/tests/", 1)[0])
    cfg = load_config("config.example.yaml")
    assert cfg.mode == "paper" and cfg.exits.take_profits[0] == [100, 50]
