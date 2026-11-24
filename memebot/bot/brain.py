"""El "cerebro": aprende de las velas y de sus propias operaciones y evoluciona su estrategia.

Ciclo de aprendizaje (python -m bot.brain --loop 6  ->  cada 6 h):

  1. DATOS. Velas de 1 min recogidas a futuro (data/, data_launches/): cada minuto de
     cada token es un "ejemplo"; su etiqueta es lo que habría pasado si el bot hubiera
     entrado ahí (simulación completa con stops, take-profits y costes).
  2. MODELO. Un gradient boosting aprende qué situaciones de mercado acaban bien y
     cuáles mal. Se valida en el tiempo: entrena con el pasado, elige umbral con un
     periodo posterior y se evalúa con un periodo final que nunca ha visto.
  3. COMPETICIÓN. Campeón contra aspirantes: la estrategia actual compite con las
     estrategias de reglas reoptimizadas y con el modelo. Solo se cambia si el aspirante
     es mejor en el periodo no visto según el límite inferior de confianza (media − error),
     no la media a secas: así no se persigue la suerte.
  4. ERRORES. Compara lo que esperaba ganar con lo que ganó de verdad (paper/live). Si
     la realidad es peor, sube su estimación de costes y vuelve a evaluarlo todo con
     ella. Redacta lecciones legibles a partir de sus datos.
  5. MODO DEFENSIVO. Si ninguna estrategia demuestra ventaja, lo dice y, en dinero real,
     deja de abrir posiciones.

Límites que el cerebro NUNCA cruza: no toca el tamaño de posición, el stop diario ni el
número de posiciones. En modo live, por defecto solo PROPONE: tú apruebas desde el panel.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import time
from pathlib import Path

import numpy as np

from .history import positions as closed_positions
from .signals import STRATEGIES, features, to_series

log = logging.getLogger("brain")

FEATURES = ["age_h", "log_vol_5m", "log_vol_60m", "ret_5m", "ret_60m", "ret_6h", "drawdown_6h",
            "vs_high_3h", "active_60m", "accel", "log_fdv"]
NAMES_ES = {
    "age_h": "edad del token (h)", "log_vol_5m": "volumen 5 min", "log_vol_60m": "volumen 1 h",
    "ret_5m": "subida 5 min (%)", "ret_60m": "subida 1 h (%)", "ret_6h": "subida 6 h (%)",
    "drawdown_6h": "caída desde el máximo de 6 h (%)", "vs_high_3h": "distancia al máximo previo de 3 h (%)",
    "active_60m": "minutos con operaciones en 1 h", "accel": "aceleración del volumen", "log_fdv": "capitalización (FDV)",
}
DEFAULT_EXITS = {"sl": 25, "tp": [[100, 50], [300, 25]], "trail": 30, "trail_act": 50, "time": 90, "time_gain": 10}
MIN_SAMPLES = 3000
MIN_POOLS = 60
MIN_TEST_TRADES = 25
PROMOTE_MARGIN = 0.5          # puntos % de LCB que el aspirante debe superar al campeón
STRIDE = 3                    # un ejemplo cada 3 minutos por token


# --------------------------------------------------------------------------- features
def feature_matrix(f: dict, idx: np.ndarray) -> np.ndarray:
    hp = f["high_prev_3h"][idx]
    vs_high = np.where(np.isfinite(hp) & (hp > 0), (f["price"][idx] / np.where(hp > 0, hp, 1) - 1) * 100, 0.0)
    cols = [
        np.clip(f["age_min"][idx] / 60, 0, 96),
        np.log1p(f["vol_5m"][idx]), np.log1p(f["vol_60m"][idx]),
        np.clip(f["ret_5m"][idx], -100, 500), np.clip(f["ret_60m"][idx], -100, 2000), np.clip(f["ret_6h"][idx], -100, 5000),
        f["drawdown_6h"][idx], np.clip(vs_high, -100, 1000), f["active_60m"][idx],
        np.clip(f["accel"][idx], 0, 12), np.log1p(np.maximum(f["fdv"][idx], 0)),
    ]
    return np.column_stack(cols).astype(np.float64)


def activity_mask(f: dict) -> np.ndarray:
    """Momentos mínimamente operables (los mismos que exige el prefiltro en vivo)."""
    return f["valid"] & (f["vol_60m"] >= 10_000) & (f["active_60m"] >= 20) & (f["age_min"] >= 15)


# --------------------------------------------------------------------------- dataset
def build_dataset(pools, exits: dict, cost_penalty: float = 0.0, stride: int = STRIDE):
    from research.backtest import simulate

    tp_g = np.array([g / 100 for g, _ in exits["tp"]], dtype=np.float64)
    tp_p = np.array([p / 100 for _, p in exits["tp"]], dtype=np.float64)
    X, y, ts, pid = [], [], [], []
    for k, pd in enumerate(pools):
        mask = activity_mask(pd.f)
        cand = np.flatnonzero(mask[: len(pd.c) - 1])
        cand = cand[(cand >= pd.first_entry)]
        cand = cand[::stride]
        if not len(cand):
            continue
        for i in cand:
            r, _, _ = simulate(pd.o, pd.h, pd.l, pd.c, i + 1, pd.side_cost[i + 1] + cost_penalty / 2,
                               exits["sl"] / 100, tp_g, tp_p, exits["trail"] / 100, exits["trail_act"] / 100,
                               exits["time"], exits["time_gain"] / 100)
            y.append(r)
        X.append(feature_matrix(pd.f, cand))
        ts.append(pd.ts[cand])
        pid.append(np.full(len(cand), k))
    if not X:
        return None
    return np.vstack(X), np.array(y), np.concatenate(ts), np.concatenate(pid)


def dedupe(sel_idx: np.ndarray, ts: np.ndarray, pid: np.ndarray, gap: int = 3600) -> np.ndarray:
    """Una operación por token cada `gap` segundos (no se puede estar dentro dos veces)."""
    order = sel_idx[np.argsort(ts[sel_idx])]
    last: dict[int, int] = {}
    keep = []
    for i in order:
        p = int(pid[i])
        if ts[i] - last.get(p, -10**12) >= gap:
            keep.append(i)
            last[p] = int(ts[i])
    return np.array(keep, dtype=int)


def lcb(r: np.ndarray) -> float:
    """Límite inferior (~1 desviación) de la esperanza por operación, en %."""
    if len(r) < 2:
        return -math.inf
    return float(100 * (r.mean() - r.std(ddof=1) / math.sqrt(len(r))))


def summarize(r: np.ndarray) -> dict:
    if not len(r):
        return {"n": 0, "expectancy": 0, "win_rate": 0, "lcb": None}
    return {"n": int(len(r)), "expectancy": round(100 * float(r.mean()), 2),
            "win_rate": round(100 * float((r > 0).mean()), 1), "lcb": round(lcb(r), 2) if len(r) > 1 else None,
            "median": round(100 * float(np.median(r)), 1), "worst": round(100 * float(r.min()), 1)}


# --------------------------------------------------------------------------- modelo
def train_model(X, y, ts, pid):
    from sklearn.ensemble import HistGradientBoostingRegressor

    t1, t2 = np.quantile(ts, [0.6, 0.8])
    tr, va, te = ts < t1, (ts >= t1) & (ts < t2), ts >= t2
    model = HistGradientBoostingRegressor(max_depth=3, learning_rate=0.05, max_iter=250, min_samples_leaf=80,
                                          l2_regularization=1.0, random_state=0)
    model.fit(X[tr], np.clip(y[tr], -1, 3))
    # umbral elegido en VALIDACIÓN (nunca en test)
    pv = model.predict(X[va])
    best_thr, best_score = None, -math.inf
    for q in (50, 60, 70, 80, 85, 90, 95):
        thr = float(np.percentile(pv, q))
        sel = dedupe(np.flatnonzero(va)[pv >= thr], ts, pid)
        if len(sel) < 15:
            continue
        sc = lcb(y[sel])
        if sc > best_score:
            best_thr, best_score = thr, sc
    if best_thr is None:
        return None
    pt = model.predict(X[te])
    sel_te = dedupe(np.flatnonzero(te)[pt >= best_thr], ts, pid)
    base_te = dedupe(np.flatnonzero(te), ts, pid)
    imp = importance(model, X[te], y[te])
    return {"model": model, "threshold": best_thr, "val_lcb": best_score,
            "test": summarize(y[sel_te]), "test_baseline": summarize(y[base_te]),
            "importance": imp, "split": [float(t1), float(t2)]}


def importance(model, X, y, n: int = 3000) -> list[tuple[str, float]]:
    from sklearn.inspection import permutation_importance

    if len(X) > n:
        idx = np.random.default_rng(0).choice(len(X), n, replace=False)
        X, y = X[idx], y[idx]
    r = permutation_importance(model, X, np.clip(y, -1, 3), n_repeats=5, random_state=0)
    pairs = sorted(zip(FEATURES, r.importances_mean), key=lambda x: -x[1])
    return [(k, round(float(v), 5)) for k, v in pairs]


# --------------------------------------------------------------------------- reglas
def evaluate_rules(pools, t1: float, t2: float, cost_penalty: float):
    """Mejor configuración de cada estrategia de reglas: elegida antes de t1, evaluada después de t2."""
    from research.backtest import ENTRY_GRID, EXIT_GRID, grid, run

    for pd in pools:
        pd._orig_cost = getattr(pd, "_orig_cost", pd.side_cost)
        pd.side_cost = pd._orig_cost + cost_penalty / 2
    out = {}
    for strat in STRATEGIES:
        if strat == "baseline":
            continue
        best = None
        for ep in grid(ENTRY_GRID[strat]):
            for xp in grid(EXIT_GRID):
                tr = run(pools, strat, ep, xp)
                train = np.array([t[2] for t in tr if t[0] < t1])
                if len(train) < 20:
                    continue
                sc = lcb(train)
                if best is None or sc > best[0]:
                    test = np.array([t[2] for t in tr if t[0] >= t2])
                    best = (sc, ep, xp, test)
        if best:
            out[strat] = {"entry": best[1], "exit": best[2], "train_lcb": round(best[0], 2), "test": summarize(best[3]),
                          "_test_r": best[3]}
    return out


def evaluate_champion_rule(pools, champ: dict, t2: float) -> np.ndarray:
    from research.backtest import run

    tr = run(pools, champ["strategy"], champ.get("params", {}), champ["exits_bt"])
    return np.array([t[2] for t in tr if t[0] >= t2])


# --------------------------------------------------------------------------- aprender de errores
def reality_check(state: Path, brain: dict) -> dict:
    """Compara el rendimiento real (paper/live) con lo que el cerebro esperaba."""
    f = state / "trades.csv"
    if not f.exists():
        return {"n": 0}
    done = [d for d in closed_positions(f) if d.get("opened", "") >= brain.get("champion_since_iso", "")]
    if not done:
        return {"n": 0}
    r = np.array([d["ret"] for d in done])
    exp = (brain.get("champion") or {}).get("expected_expectancy")
    reasons: dict[str, list[float]] = {}
    for d in done:
        key = (d["reasons"][-1] if d["reasons"] else "?").split(" ")[0]
        reasons.setdefault(key, []).append(d["ret"])
    return {"n": len(r), "expectancy": round(100 * float(r.mean()), 2), "win_rate": round(100 * float((r > 0).mean()), 1),
            "expected": exp, "by_exit": {k: {"n": len(v), "avg": round(100 * float(np.mean(v)), 1)} for k, v in reasons.items()}}


def lessons_from(X, y, imp, reality: dict, cost_penalty: float) -> list[str]:
    out = []
    for name, _ in imp[:3]:
        j = FEATURES.index(name)
        col = X[:, j]
        qs = np.unique(np.quantile(col, [0, .2, .4, .6, .8, 1]))
        if len(qs) < 3:
            continue
        b = np.clip(np.searchsorted(qs, col, side="right") - 1, 0, len(qs) - 2)
        means = [(float(y[b == k].mean()), k) for k in range(len(qs) - 1) if (b == k).sum() >= 50]
        if len(means) < 2:
            continue
        hi, lo = max(means), min(means)
        fmt = ((lambda v: f"{math.expm1(v):,.0f} $".replace(",", ".")) if name.startswith("log_")
               else (lambda v: f"{v:.1f}".replace(".", ",")))
        pc = lambda v: f"{100 * v:+.1f}%".replace(".", ",")  # noqa: E731
        out.append(f"Con {NAMES_ES[name]} entre {fmt(qs[hi[1]])} y {fmt(qs[hi[1] + 1])} el resultado medio es "
                   f"{pc(hi[0])}; entre {fmt(qs[lo[1]])} y {fmt(qs[lo[1] + 1])}, {pc(lo[0])}.")
    if reality.get("n", 0) >= 10:
        be = reality["by_exit"]
        worst = min(be.items(), key=lambda kv: kv[1]["avg"] * kv[1]["n"])
        out.append(f"En operaciones reales, la salida que más resta es «{worst[0]}» "
                   f"({worst[1]['n']} veces, media {worst[1]['avg']:+.1f}%).")
    if cost_penalty > 0:
        out.append(f"La realidad rindió peor que la simulación: el cerebro añade {100 * cost_penalty:.1f} puntos de coste "
                   f"extra a todas sus pruebas para no volver a sobreestimarse.")
    return out


# --------------------------------------------------------------------------- ciclo completo
def learn(data_dirs: list[str], state_dir: str, full: bool = True) -> dict:
    from research.backtest import load

    state = Path(state_dir)
    bdir = state / "brain"
    bdir.mkdir(parents=True, exist_ok=True)
    bfile = state / "brain.json"
    brain = json.loads(bfile.read_text()) if bfile.exists() else {"version": 0, "history": [], "cost_penalty": 0.0}
    t0 = time.time()

    # 4) aprender de errores: calibrar costes con la realidad
    reality = reality_check(state, brain)
    exp = (brain.get("champion") or {}).get("expected_expectancy")
    if reality.get("n", 0) >= 20 and exp is not None:
        gap = (exp - reality["expectancy"]) / 100
        brain["cost_penalty"] = round(float(np.clip(0.5 * brain.get("cost_penalty", 0) + 0.5 * max(0.0, gap), 0, 0.08)), 4)
    pen = brain.get("cost_penalty", 0.0)

    pools = [p for d in data_dirs if Path(d, "pools.json").exists() for p in load(d)]
    brain.update({"updated_at": int(time.time()), "reality": reality, "pools": len(pools)})
    ds = build_dataset(pools, (brain.get("champion") or {}).get("exits_bt", DEFAULT_EXITS), pen) if pools else None
    n = 0 if ds is None else len(ds[1])
    brain["samples"] = n
    if ds is None or n < MIN_SAMPLES or len(pools) < MIN_POOLS:
        brain["status"] = "recopilando"
        brain["status_text"] = (f"Recopilando datos: {n:,} ejemplos de {MIN_SAMPLES:,} y {len(pools)} tokens de {MIN_POOLS} "
                                f"necesarios antes de decidir nada.").replace(",", ".")
        bfile.write_text(json.dumps(brain, indent=1))
        log.info(brain["status_text"])
        return brain
    X, y, ts, pid = ds

    # 2) modelo
    ml = train_model(X, y, ts, pid)
    t1, t2 = (ml["split"] if ml else np.quantile(ts, [0.6, 0.8]).tolist())
    # 3) aspirantes de reglas
    # reoptimizar las reglas es caro: se hace en los ciclos completos; en los rápidos se conserva lo último
    if full or not brain.get("_rules_cache"):
        rules = evaluate_rules(pools, t1, t2, pen)
        brain["_rules_cache"] = {k: {kk: vv for kk, vv in v.items() if kk != "_test_r"} for k, v in rules.items()}
        brain["last_full_learn"] = int(time.time())
    else:
        rules = brain["_rules_cache"]
    candidates = []
    for name, r in rules.items():
        candidates.append({"kind": "rule", "strategy": name, "params": r["entry"], "exits_bt": r["exit"],
                           "test": r["test"], "score": r["test"]["lcb"] if r["test"]["n"] >= MIN_TEST_TRADES else None})
    if ml and ml["test"]["n"] >= MIN_TEST_TRADES:
        candidates.append({"kind": "ml", "strategy": "ml", "params": {}, "exits_bt": DEFAULT_EXITS,
                           "threshold": ml["threshold"], "test": ml["test"], "score": ml["test"]["lcb"]})
    valid = [c for c in candidates if c["score"] is not None]
    best = max(valid, key=lambda c: c["score"]) if valid else None

    # campeón actual, re-evaluado en el MISMO periodo de test
    champ = brain.get("champion")
    champ_score = None
    if champ and champ.get("kind") == "rule":
        r = evaluate_champion_rule(pools, champ, t2)
        champ_score = lcb(r) if len(r) >= MIN_TEST_TRADES else None
    elif champ and champ.get("kind") == "ml" and ml:
        champ_score = ml["test"]["lcb"]      # el modelo se reentrena: su versión nueva es la referencia

    decision = "mantener"
    if best and best["score"] > 0 and (champ_score is None or best["score"] > champ_score + PROMOTE_MARGIN):
        brain["version"] += 1
        ver = brain["version"]
        if best["kind"] == "ml":
            import joblib
            path = bdir / f"model_v{ver}.joblib"
            joblib.dump(ml["model"], path)
            best["model_path"] = str(path.name)
        best["expected_expectancy"] = best["test"]["expectancy"]
        best["version"] = ver
        best["promoted_at"] = int(time.time())
        brain["champion"] = best
        brain["champion_since_iso"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        decision = "nueva"
    elif champ and champ.get("kind") == "ml" and ml and champ_score and champ_score > 0:
        import joblib                            # mismo tipo: actualizar el modelo reentrenado
        path = bdir / f"model_v{brain['version']}.joblib"
        joblib.dump(ml["model"], path)
        champ["threshold"], champ["test"] = ml["threshold"], ml["test"]
    if not best or best["score"] <= 0:
        if not champ_score or champ_score <= 0:
            decision = "defensivo"

    brain["status"] = decision
    if decision == "nueva":
        c = brain["champion"]
        brain["status_text"] = (f"Nueva estrategia v{brain['version']} ({c['strategy']}): esperanza "
                                f"{c['test']['expectancy']:+.2f}% por operación en datos no vistos.")
    elif decision == "defensivo":
        brain["status_text"] = ("Ninguna estrategia demuestra ventaja en datos no vistos. Modo defensivo: "
                                "en real no se abren posiciones.")
    else:
        brain["status_text"] = "La estrategia actual sigue siendo la mejor; ningún aspirante la supera con margen suficiente."
    brain["candidates"] = [{k: v for k, v in c.items() if k not in ("exits_bt",)} | {"exits": c["exits_bt"]} for c in candidates]
    brain["ml"] = ({"test": ml["test"], "test_baseline": ml["test_baseline"], "threshold": ml["threshold"],
                    "importance": [{"feature": k, "name": NAMES_ES[k], "value": v} for k, v in ml["importance"]]} if ml else None)
    brain["lessons"] = lessons_from(X, y, ml["importance"] if ml else [], reality, pen)
    brain["history"] = (brain.get("history", []) + [{"ts": int(time.time()), "decision": decision,
                        "version": brain["version"], "best": best["strategy"] if best else None,
                        "best_score": best["score"] if best else None, "samples": n}])[-50:]
    brain["learn_seconds"] = round(time.time() - t0, 1)
    brain["learn_mode"] = "completo" if full else "rápido"
    bfile.write_text(json.dumps(brain, indent=1, default=str))
    log.info(brain["status_text"])
    return brain


# --------------------------------------------------------------------------- uso en vivo
class BrainRuntime:
    """Lo usa el bot: recarga brain.json cuando cambia y aplica el campeón."""

    def __init__(self, state_dir: str):
        self.file = Path(state_dir) / "brain.json"
        self.dir = Path(state_dir) / "brain"
        self._mtime = 0.0
        self.data: dict = {}
        self.model = None

    def refresh(self):
        try:
            m = self.file.stat().st_mtime
        except OSError:
            return
        if m == self._mtime:
            return
        self._mtime = m
        try:
            self.data = json.loads(self.file.read_text())
        except ValueError:
            return
        champ = self.data.get("champion") or {}
        self.model = None
        if champ.get("kind") == "ml" and champ.get("model_path"):
            import joblib
            try:
                self.model = joblib.load(self.dir / champ["model_path"])
            except Exception as e:  # noqa: BLE001
                log.warning("No se pudo cargar el modelo: %s", e)

    @property
    def champion(self) -> dict | None:
        return self.data.get("champion")

    @property
    def version(self) -> int:
        return int((self.champion or {}).get("version", 0))

    @property
    def defensive(self) -> bool:
        return self.data.get("status") == "defensivo"

    def signal(self, rows, created_at: int, supply: float) -> tuple[bool, dict, float | None]:
        champ = self.champion
        s = to_series(rows)
        if s is None or not champ:
            return False, {}, None
        f = features(s, created_at, supply)
        last = {k: float(v[-1]) for k, v in f.items()}
        if champ["kind"] == "rule":
            return bool(STRATEGIES[champ["strategy"]](f, champ.get("params", {}))[-1]), last, None
        if self.model is None or not activity_mask(f)[-1]:
            return False, last, None
        pred = float(self.model.predict(feature_matrix(f, np.array([len(s) - 1])))[0])
        return pred >= champ["threshold"], last, pred

    def exits(self) -> dict | None:
        champ = self.champion
        if not champ:
            return None
        x = champ["exits_bt"]
        return {"stop_loss_pct": x["sl"], "take_profits": x["tp"], "trailing_stop_pct": x["trail"],
                "trailing_activation_pct": x["trail_act"], "time_stop_minutes": x["time"],
                "time_stop_min_gain_pct": x["time_gain"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data,data_launches")
    ap.add_argument("--state", default="state")
    ap.add_argument("--loop", type=float, default=0, help="repetir cada N horas (formato antiguo)")
    ap.add_argument("--every-min", type=float, default=0, help="aprendizaje continuo: reentrenar cada N minutos")
    ap.add_argument("--full-every", type=int, default=12, help="reoptimización completa de reglas cada N ciclos")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    period = a.every_min * 60 if a.every_min else a.loop * 3600
    k = 0
    while True:
        t0 = time.time()
        try:
            learn(a.data.split(","), a.state, full=(k % max(1, a.full_every) == 0) or not a.every_min)
        except Exception:  # noqa: BLE001
            log.exception("Fallo en el ciclo de aprendizaje")
        k += 1
        if not period:
            break
        time.sleep(max(30, period - (time.time() - t0)))


if __name__ == "__main__":
    main()
