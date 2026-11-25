"""El cerebro debe DESCUBRIR un patrón real y NO inventarse uno en ruido puro."""
import json

import numpy as np
import pytest

import bot.brain as brain
import research.backtest as bt


def make_dataset(d, n_pools=90, n_min=500, pattern=True, seed=0):
    rng = np.random.default_rng(seed)
    (d / "ohlcv").mkdir(parents=True)
    index = {}
    t0 = 1_790_000_000
    for k in range(n_pools):
        start = t0 + k * 1800
        p, rows = 0.001, []
        burst_at = set(rng.choice(np.arange(90, n_min - 60), 3, replace=False))
        pump = 0
        for i in range(n_min):
            vol = rng.uniform(300, 600)
            drift = -0.0008
            if i in burst_at:
                pump = 25
            if pump > 0:
                vol = 9000                      # ráfaga de volumen ...
                if pattern:
                    drift = 0.012              # ... que (si hay patrón) precede a subidas
                pump -= 1
            o = p
            p = p * (1 + drift + rng.normal(0, 0.004))
            rows.append([start + 60 * i, o, max(o, p) * 1.001, min(o, p) * 0.999, p, vol])
        rows.append(rows[-1][:])               # vela en curso (se descarta)
        addr = f"POOL{k}"
        (d / "ohlcv" / f"{addr}.json").write_text(json.dumps(rows))
        index[addr] = {"pool": addr, "name": f"T{k} / SOL", "created_at": start, "selected_at": start,
                       "fdv_now": 1e6, "price_now": 0.001, "sources": ["test"]}
    (d / "pools.json").write_text(json.dumps(index))


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(brain, "MIN_SAMPLES", 500)
    monkeypatch.setattr(brain, "MIN_POOLS", 20)
    monkeypatch.setattr(brain, "MIN_TEST_TRADES", 10)
    monkeypatch.setattr(bt, "ENTRY_GRID", {k: {list(v)[0]: [list(v.values())[0][0]]} for k, v in bt.ENTRY_GRID.items()})
    monkeypatch.setattr(bt, "EXIT_GRID", {"sl": [15], "tp": [[[30, 50], [80, 25]]], "trail": [20], "trail_act": [30],
                                          "time": [30], "time_gain": [10]})
    monkeypatch.setattr(brain, "DEFAULT_EXITS", {"sl": 15, "tp": [[30, 50], [80, 25]], "trail": 20, "trail_act": 30,
                                                 "time": 30, "time_gain": 10})


def test_brain_waits_for_enough_data(tmp_path):
    make_dataset(tmp_path / "data", n_pools=5, n_min=200)
    b = brain.learn([str(tmp_path / "data")], str(tmp_path / "state"))
    assert b["status"] == "recopilando" and "champion" not in b


def test_brain_discovers_real_pattern_and_bot_uses_it(tmp_path, fast):
    make_dataset(tmp_path / "data", pattern=True)
    b = brain.learn([str(tmp_path / "data")], str(tmp_path / "state"))
    assert b["status"] == "nueva", b["status_text"]
    assert b["champion"]["test"]["expectancy"] > 0
    assert b["lessons"]
    # el runtime lo carga y genera señales
    rt = brain.BrainRuntime(str(tmp_path / "state"))
    rt.refresh()
    assert rt.champion and rt.version == 1
    # las salidas son las aprendidas (las de la regla o el estilo que eligió el modelo en validación)
    assert rt.exits()["stop_loss_pct"] in {15} | {v["sl"] for v in brain.ML_EXIT_CHOICES.values()}
    if rt.champion["kind"] == "ml":
        assert rt.champion["exit_style"] and "enabled" in rt.champion["model_exit"]
    # una segunda pasada con los mismos datos no cambia de campeón sin mejora
    b2 = brain.learn([str(tmp_path / "data")], str(tmp_path / "state"))
    assert b2["version"] in (1, 2) and b2["status"] in ("mantener", "nueva")


def test_brain_goes_defensive_on_pure_noise(tmp_path, fast):
    make_dataset(tmp_path / "data", pattern=False, seed=3)
    b = brain.learn([str(tmp_path / "data")], str(tmp_path / "state"))
    assert b["status"] == "defensivo", b["status_text"]
    assert "champion" not in b or b["champion"] is None
