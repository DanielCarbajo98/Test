import json
import time

import httpx
import numpy as np

from bot import copytrade, hunter
from research import moonshot


def launch(path, created=1_000_000, pool="P", vol=100.0):
    c = np.asarray(path, float)
    return {"pool": pool, "mint": "M" + pool, "name": pool, "created": created, "off": 0,
            "o": c.copy(), "h": c * 1.01, "l": c * 0.99, "c": c, "v": np.full(len(c), vol)}


def test_simulate_half_at_2x_then_trailing():
    path = [1.0] * 5 + list(np.linspace(1, 5, 60)) + list(np.linspace(5, 1, 60)) + [1.0] * 100
    L = launch(path)
    r = moonshot.simulate(L, 4, {"stop": 0.5, "trail": 0.3, "hours": 6}, cost=0.0)
    assert r > 1.0                                    # mitad a 2x (+50 %) + resto vendido cerca de 3,5x


def test_simulate_stop_first():
    L = launch([1.0] * 5 + list(np.linspace(1, 0.1, 50)) + [0.1] * 50)
    r = moonshot.simulate(L, 4, {"stop": 0.5, "trail": 0.3, "hours": 6}, cost=0.0)
    assert abs(r - (-0.5)) < 1e-9


def test_study_reports_distribution_and_honest_no_edge():
    rng = np.random.default_rng(0)
    ls = []
    for k in range(150):                              # casi todas mueren: no debe encontrar ventaja
        path = [1.0] * 10 + list(np.linspace(1, rng.uniform(0.01, 0.5), 300)) + [0.05] * 1200
        ls.append(launch(path, created=1_000_000 + k * 600, pool=f"P{k}", vol=1e5))
    res = moonshot.study(ls)
    assert res["distribution"]["n"] == 150 and res["distribution"]["reach_2x"] == 0
    assert res["edge"] is False


def test_position_lifecycle():
    s = hunter.new_sleeve()
    rule = {"stop": 0.5, "trail": 0.3, "hours": 24}
    p = hunter.open_pos(s, "A", {"name": "A"}, 1.0, rule, 0, 0.0)
    assert p and s["cash"] == hunter.START_USD - hunter.BET_USD
    assert hunter.step_pos(s, "A", 2.1, 60, 0.0)["partial"]            # mitad a 2x
    assert hunter.step_pos(s, "A", 4.0, 120, 0.0) is None              # sigue subiendo
    c = hunter.step_pos(s, "A", 2.7, 180, 0.0)                         # cae >30 % desde 4x
    assert c and c["ret"] > 1.0 and not s["positions"]
    assert abs(hunter.sleeve_equity(s) - (hunter.START_USD + c["got_usd"] - c["cost_usd"])) < 1e-6


def test_hunt_buys_only_when_rule_and_rugcheck_pass(tmp_path):
    now = 2_000_000
    def handler(req: httpx.Request):
        u = str(req.url)
        if "geckoterminal" in u:
            return httpx.Response(200, json={"data": []})
        if "/pairs/solana/" in u:
            return httpx.Response(200, json={"pairs": [
                {"pairAddress": "GOOD", "priceUsd": "0.001", "volume": {"h1": 50000}, "priceChange": {"m5": 10, "h1": 80}},
                {"pairAddress": "RUG", "priceUsd": "0.001", "volume": {"h1": 50000}, "priceChange": {"m5": 10, "h1": 80}},
                {"pairAddress": "THIN", "priceUsd": "0.001", "volume": {"h1": 50}, "priceChange": {"m5": 10, "h1": 80}}]})
        if "rugcheck" in u:
            bad = "MRUG" in u
            return httpx.Response(200, json={"risks": [{"level": "danger" if bad else "warn"}]})
        return httpx.Response(404)
    h = hunter.Hunter(tmp_path, tmp_path, http=httpx.Client(transport=httpx.MockTransport(handler)))
    for k in ("GOOD", "RUG", "THIN"):
        h.db["watch"][k] = {"created": now - 15 * 60, "mint": "M" + k, "name": k, "checked": False}
    h.last["study"] = now
    h.tick(now)
    assert set(h.db["sleeves"]["cazador"]["positions"]) == {"GOOD"}
    assert json.loads((tmp_path / "hunter.json").read_text())["summary"]["cazador"]["open"] == 1


def tx(owner, mint, pre, post, err=None):
    return {"meta": {"err": err, "preTokenBalances": [{"owner": owner, "mint": mint, "uiTokenAmount": {"uiAmount": pre}}],
                     "postTokenBalances": [{"owner": owner, "mint": mint, "uiTokenAmount": {"uiAmount": post}}]},
            "transaction": {"message": {"accountKeys": [{"pubkey": owner, "signer": True}]}}}


def test_buys_parsed_from_token_balances():
    assert copytrade.buys_in(tx("W", "TOK", 0, 100)) == [("W", "TOK")]
    assert copytrade.buys_in(tx("W", "TOK", 100, 0)) == []              # venta
    assert copytrade.buys_in(tx("W", "So11111111111111111111111111111111111111112", 0, 5)) == []
    assert copytrade.buys_in(tx("W", "TOK", 0, 1, err={"x": 1})) == []


def test_copy_evaluation_finds_a_good_wallet_out_of_sample():
    ls, pools = [], {}
    for k in range(60):
        good = k % 2 == 0
        path = [1.0] * 3 + (list(np.linspace(1, 6, 120)) if good else list(np.linspace(1, 0.1, 120))) + [1.0] * 1300
        if good:
            path = path[:123] + list(np.linspace(6, 3, 60)) + [3.0] * 1240
        L = launch(path, created=1_000_000 + k * 3600, pool=f"P{k}")
        ls.append(L)
        buyers = [["SMART", L["created"] + 60]] if good else []
        buyers += [[f"R{k}{j}", L["created"] + 60] for j in range(3)]
        pools[L["pool"]] = {"buyers": buyers}
    res = copytrade.evaluate({"pools": pools}, ls)
    assert res["observations"] >= 100 and res["watch"][0]["wallet"] == "SMART"
    assert res["copy_out_of_sample"]["n"] >= 1 and res["copy_out_of_sample"]["mean_pct"] > 0


def swaps(rois, t0=1_000_000, gap=7200, hold=1800, size=1.0):
    out = []
    for k, r in enumerate(rois):
        ts = t0 + k * gap
        out += [{"mint": f"T{k}", "side": "buy", "sol": size, "ts": ts},
                {"mint": f"T{k}", "side": "sell", "sol": size * (1 + r), "ts": ts + hold}]
    return out


def test_consistent_trader_is_skilled():
    rng = np.random.default_rng(1)
    m = copytrade.trader_metrics(swaps(list(rng.uniform(-0.2, 0.8, 30))), alpha=[0.1, 0.2, 0.05])
    assert m["verdict"] == "habilidad probable" and m["p_value"] < 0.05


def test_one_lucky_moonshot_is_not_skill():
    m = copytrade.trader_metrics(swaps([49.0] + [-0.6] * 20))
    assert m["pnl_sol"] > 0 and m["pnl_without_best_sol"] < 0
    assert m["verdict"] == "posible suerte" and "una sola operación" in m["why"]


def test_no_better_than_others_is_not_skill():
    rng = np.random.default_rng(2)
    m = copytrade.trader_metrics(swaps(list(rng.uniform(-0.2, 0.8, 30))), alpha=[-0.1, -0.2, -0.05, -0.1])
    assert m["verdict"] == "posible suerte" and "demás" in m["why"]


def test_bots_and_small_samples():
    assert copytrade.trader_metrics(swaps([0.1] * 30, gap=5, hold=2))["verdict"] == "bot"
    assert copytrade.trader_metrics(swaps([0.5] * 4))["verdict"] == "pocos datos"
    assert copytrade.trader_metrics(swaps([-0.3] * 15))["verdict"] == "pierde"


def test_swap_parsed_with_sol_amounts():
    t = tx("W", "TOK", 0, 100)
    t["meta"].update({"preBalances": [3_000_000_000], "postBalances": [2_000_000_000]})
    t["blockTime"] = 123
    assert copytrade.swap_of(t) == {"mint": "TOK", "side": "buy", "sol": 1.0, "ts": 123}
    t = tx("W", "TOK", 100, 0)
    t["meta"].update({"preBalances": [1_000_000_000], "postBalances": [3_500_000_000]})
    assert copytrade.swap_of(t)["side"] == "sell" and copytrade.swap_of(t)["sol"] == 2.5


def test_analyze_traders_profiles_frequent_early_buyers():
    class FakeRPC:
        def signatures(self, w, limit=1000, before=None):
            return [{"signature": f"{w}-{k}"} for k in range(4)]
        def transactions(self, sigs):
            out = []
            for k, s in enumerate(sigs):
                buy = k % 2 == 0
                t = tx("W1", f"T{k // 2}", 0 if buy else 10, 10 if buy else 0)
                t["meta"].update({"preBalances": [5e9], "postBalances": [4e9 if buy else 6.5e9]})
                t["blockTime"] = 1000 + k * 600
                out.append(t)
            return out
    db = {"pools": {"P1": {"buyers": [["W1", 1]]}, "P2": {"buyers": [["W1", 2]]}, "P3": {"buyers": [["W2", 3]]}}}
    res = copytrade.analyze_traders(FakeRPC(), db, [], {}, now=10_000)
    assert set(res["profiles"]) == {"W1"}                       # W2 solo aparece una vez
    p = res["profiles"]["W1"]
    assert p["closed"] == 2 and p["pnl_sol"] == 1.0 and p["verdict"] == "pocos datos"
