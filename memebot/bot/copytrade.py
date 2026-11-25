"""Copiar carteras: ¿hay carteras que compran pronto las memecoins buenas, y copiarlas con retraso gana dinero?

1. APRENDER QUIÉN COMPRA PRONTO. Para lanzamientos de la muestra sin sesgo (data_launches) recién nacidos
   (20-90 min), lee en la blockchain (tu RPC de Helius) sus primeras ~100 operaciones y apunta qué carteras
   compraron y cuándo. Se descartan las del primer bloque (creador, «bundles», bots de francotirador).
2. PUNTUAR. Cuando se sabe cómo acabó cada token (7 h después), se simula COPIAR a cada cartera: entrar
   60 s después de su compra (lo que tarda de verdad una copia) con las salidas del cazador.
   Carteras elegidas con el periodo antiguo (70 %) y la copia se COMPRUEBA en el reciente (30 %).
3. VIGILAR. Las mejores carteras se miran cada 2 min; si compran un token, se copia en SIMULADO.
Consumo de Helius prudente (plan gratis: ~1 M créditos/mes): ~60 lanzamientos/día + 10 carteras vigiladas.
Las carteras son direcciones públicas de la blockchain.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from pathlib import Path

import httpx
import numpy as np

log = logging.getLogger("copytrade")
SKIP_MINTS = {"So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wGGkZwyTDt1v",
              "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}
COPY_DELAY_S = 60
MIN_BUYS_WALLET = 3
WATCH_MAX = 10


class RPC:
    def __init__(self, url: str, client: httpx.Client | None = None):
        self.url, self.http, self._id = url, client or httpx.Client(timeout=30), 0

    def call(self, method: str, params: list):
        return self.batch([(method, params)])[0]

    def batch(self, calls: list[tuple[str, list]]) -> list:
        body = []
        for m, p in calls:
            self._id += 1
            body.append({"jsonrpc": "2.0", "id": self._id, "method": m, "params": p})
        for attempt in range(4):
            r = self.http.post(self.url, json=body)
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            res = r.json()
            res = res if isinstance(res, list) else [res]
            by_id = {x.get("id"): x.get("result") for x in res}
            return [by_id.get(b["id"]) for b in body]
        raise RuntimeError("RPC saturado (429)")

    def signatures(self, addr: str, limit: int = 1000, before: str | None = None) -> list[dict]:
        opt = {"limit": limit}
        if before:
            opt["before"] = before
        return self.call("getSignaturesForAddress", [addr, opt]) or []

    def transactions(self, sigs: list[str]) -> list[dict | None]:
        out = []
        for k in range(0, len(sigs), 25):
            out += self.batch([("getTransaction", [s, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
                               for s in sigs[k:k + 25]])
        return out


def token_deltas(tx: dict) -> list[tuple[str, str, float]]:
    """(dueño, mint, cambio de saldo) de cada token en la transacción."""
    meta = (tx or {}).get("meta") or {}
    if meta.get("err"):
        return []
    acc = {}
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in meta.get(side) or []:
            k = (b.get("owner"), b.get("mint"))
            amt = float(((b.get("uiTokenAmount") or {}).get("uiAmount")) or 0)
            acc[k] = acc.get(k, 0.0) + sign * amt
    return [(o, m, d) for (o, m), d in acc.items() if o and m and abs(d) > 0]


def signer(tx: dict) -> str | None:
    try:
        k = tx["transaction"]["message"]["accountKeys"][0]
        return k["pubkey"] if isinstance(k, dict) else k
    except (KeyError, IndexError, TypeError):
        return None


def buys_in(tx: dict, mint: str | None = None) -> list[tuple[str, str]]:
    """Compras (cartera, mint) del firmante: su saldo de un token sube (excepto SOL/USDC/USDT)."""
    who = signer(tx)
    return [(o, m) for o, m, d in token_deltas(tx)
            if o == who and d > 0 and m not in SKIP_MINTS and (mint is None or m == mint)]


def early_buyers(rpc: RPC, pool: str, mint: str, max_pages: int = 3, n: int = 100) -> list[tuple[str, int]] | None:
    """Primeras compras del token en esa pool, sin el primer bloque. None si hay demasiadas operaciones."""
    sigs, before = [], None
    for _ in range(max_pages):
        page = rpc.signatures(pool, 1000, before)
        sigs += page
        if len(page) < 1000:
            break
        before = page[-1]["signature"]
    else:
        return None
    sigs = [s for s in sigs if not s.get("err")]
    if not sigs:
        return []
    sigs.sort(key=lambda s: (s.get("slot", 0), s.get("blockTime") or 0))
    first_slot = sigs[0].get("slot")
    early = [s for s in sigs if s.get("slot") != first_slot][:n]
    out = []
    for s, tx in zip(early, rpc.transactions([s["signature"] for s in early])):
        for w, _ in buys_in(tx, mint):
            out.append((w, int(s.get("blockTime") or (tx or {}).get("blockTime") or 0)))
    return out


# --------------------------------------------------------------------------- aprendizaje
def record(rpc: RPC, data_dir: Path, per_day: int = 60, now: float | None = None) -> int:
    """Apunta compradores tempranos de lanzamientos de 20-90 min (como mucho `per_day` al día)."""
    now = now or time.time()
    try:
        index = json.loads((data_dir / "pools.json").read_text())
    except (OSError, ValueError):
        return 0
    wf = data_dir / "wallets.json"
    db = json.loads(wf.read_text()) if wf.exists() else {"pools": {}, "days": {}}
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    done_today = db["days"].get(day, 0)
    added = 0
    for addr, meta in sorted(index.items(), key=lambda kv: -kv[1].get("created_at", 0)):
        if done_today >= per_day:
            break
        age = now - meta.get("created_at", 0)
        if addr in db["pools"] or not (20 * 60 <= age <= 90 * 60) or not meta.get("base_mint"):
            continue
        try:
            b = early_buyers(rpc, addr, meta["base_mint"])
        except (httpx.HTTPError, RuntimeError) as e:
            log.warning("RPC: %s", e)
            break
        db["pools"][addr] = {"ts": int(now), "buyers": b if b is not None else [], "busy": b is None}
        done_today += 1
        added += 1
    db["days"] = {day: done_today}
    wf.write_text(json.dumps(db))
    return added


def _stats(r: list[float]) -> dict:
    if not r:
        return {"n": 0, "mean_pct": None, "win_rate": None, "lcb_pct": None}
    x = np.asarray(r)
    lcb = x.mean() - x.std(ddof=1) / math.sqrt(len(x)) if len(x) > 1 else None
    return {"n": len(x), "mean_pct": round(100 * float(x.mean()), 2), "win_rate": round(100 * float((x > 0).mean()), 1),
            "lcb_pct": round(100 * lcb, 2) if lcb is not None else None}


def evaluate(db: dict, launches: list[dict], exit_rule: dict | None = None) -> dict:
    """Simula copiar a cada cartera con retraso y comprueba fuera de muestra."""
    from research import moonshot
    ex = exit_rule or moonshot.DEFAULT_EXIT
    by_pool = {L["pool"]: L for L in launches}
    obs = []                                              # (ts, cartera, retorno de copiarla)
    pools_seen = [p for p, v in db.get("pools", {}).items() if p in by_pool and not v.get("busy")]
    counts: dict[str, int] = {}
    for p in pools_seen:
        for w in {w for w, _ in db["pools"][p]["buyers"]}:
            counts[w] = counts.get(w, 0) + 1
    bots = {w for w, k in counts.items() if len(pools_seen) >= 10 and k > 0.5 * len(pools_seen)}
    for p in pools_seen:
        L = by_pool[p]
        first = {}
        for w, ts in db["pools"][p]["buyers"]:
            if w not in bots and ts and (w not in first or ts < first[w]):
                first[w] = ts
        for w, ts in first.items():
            i = int((ts + COPY_DELAY_S - L["created"]) // 60) - L["off"]
            if 0 <= i < len(L["c"]) - 2:
                obs.append((ts, w, moonshot.simulate(L, i, ex)))
    res = {"ts": int(time.time()), "pools": len(pools_seen), "observations": len(obs), "bots_excluded": len(bots),
           "delay_s": COPY_DELAY_S, "edge": False}
    if len(obs) < 100:
        res["note"] = f"Aún pocos datos: {len(pools_seen)} lanzamientos con resultado conocido y {len(obs)} compras."
        res["watch"] = []
        return res
    obs.sort()
    split = obs[int(0.7 * len(obs))][0]
    early: dict[str, list[float]] = {}
    for ts, w, r in obs:
        if ts < split:
            early.setdefault(w, []).append(r)
    good = {w for w, r in early.items() if len(r) >= MIN_BUYS_WALLET and np.mean(r) > 0}
    oos = [r for ts, w, r in obs if ts >= split and w in good]
    so = _stats(oos)
    allw: dict[str, list[float]] = {}
    for ts, w, r in obs:
        allw.setdefault(w, []).append(r)
    ranked = sorted((w for w, r in allw.items() if len(r) >= MIN_BUYS_WALLET and np.mean(r) > 0),
                    key=lambda w: -(np.mean(allw[w]) - np.std(allw[w]) / math.sqrt(len(allw[w]))))
    res.update({"qualified_in_sample": len(good), "copy_out_of_sample": so,
                "edge": bool(so["n"] >= 20 and (so["mean_pct"] or -1) > 0),
                "watch": [{"wallet": w, **_stats(allw[w])} for w in ranked[:WATCH_MAX]]})
    res["note"] = ("Copiar a las carteras elegidas gana también en datos no vistos." if res["edge"] else
                   "Copiar con 60 s de retraso no demuestra ganar en datos no vistos: se copia solo en simulado y en "
                   "silencio para seguir midiendo.")
    return res


def poll(rpc: RPC, wallets: list[str], seen: dict, max_age_s: int = 600, now: float | None = None) -> list[dict]:
    """Compras nuevas de las carteras vigiladas (de los últimos `max_age_s` segundos)."""
    now = now or time.time()
    out = []
    for w in wallets:
        try:
            sigs = rpc.signatures(w, 10)
        except (httpx.HTTPError, RuntimeError) as e:
            log.warning("RPC: %s", e)
            break
        new = [s for s in sigs if s["signature"] not in seen.get(w, []) and not s.get("err")
               and now - (s.get("blockTime") or 0) <= max_age_s]
        seen[w] = [s["signature"] for s in sigs]
        if not new:
            continue
        for s, tx in zip(new, rpc.transactions([s["signature"] for s in new])):
            for who, mint in buys_in(tx):
                if who == w:
                    out.append({"wallet": w, "mint": mint, "ts": int(s.get("blockTime") or now)})
    return out


def rpc_from_env() -> RPC | None:
    url = os.getenv("SOLANA_RPC_URL", "")
    if "helius" not in url and not os.getenv("COPY_ANY_RPC"):
        return None                       # el RPC público no aguanta este uso
    return RPC(url)


# --------------------------------------------------------------------------- ¿habilidad o suerte?
WSOL = "So11111111111111111111111111111111111111112"
PROFILE_TX = 150


def swap_of(tx: dict) -> dict | None:
    """Compra o venta del firmante contra SOL: {mint, side, sol, ts}. None si no es un swap claro."""
    who = signer(tx)
    meta = (tx or {}).get("meta") or {}
    if not who or meta.get("err"):
        return None
    try:
        native = (meta["postBalances"][0] - meta["preBalances"][0]) / 1e9
    except (KeyError, IndexError, TypeError):
        native = 0.0
    sol, tok = native, []
    for o, m, d in token_deltas(tx):
        if o != who:
            continue
        if m == WSOL:
            sol += d
        elif m not in SKIP_MINTS:
            tok.append((m, d))
    if len(tok) != 1:
        return None
    m, d = tok[0]
    if d > 0 and sol < -0.0005:
        return {"mint": m, "side": "buy", "sol": -sol, "ts": int(tx.get("blockTime") or 0)}
    if d < 0 and sol > 0.0005:
        return {"mint": m, "side": "sell", "sol": sol, "ts": int(tx.get("blockTime") or 0)}
    return None


def profile(rpc: RPC, wallet: str, limit: int = PROFILE_TX) -> list[dict]:
    """Últimos swaps de la cartera (compras y ventas con su SOL)."""
    sigs = [s for s in rpc.signatures(wallet, limit) if not s.get("err")]
    out = []
    for tx in rpc.transactions([s["signature"] for s in sigs]):
        sw = swap_of(tx)
        if sw:
            out.append(sw)
    return sorted(out, key=lambda x: x["ts"])


def _boot_p(r: np.ndarray, n_boot: int = 2000, seed: int = 0) -> float:
    """p-valor (bootstrap) de que su rentabilidad media real sea ≤ 0: cuanto más bajo, menos probable la suerte."""
    if len(r) < 2:
        return 1.0
    rng = np.random.default_rng(seed)
    centered = r - r.mean()
    sims = rng.choice(centered, size=(n_boot, len(r)), replace=True).mean(axis=1)
    return float((sims >= r.mean()).mean())


def trader_metrics(swaps: list[dict], alpha: list[float] | None = None) -> dict:
    """Métricas y veredicto. `alpha`: su resultado menos el de los demás compradores tempranos del mismo token."""
    by = {}
    for s in swaps:
        d = by.setdefault(s["mint"], {"spent": 0.0, "got": 0.0, "first": s["ts"], "last": s["ts"], "sells": 0})
        d["spent" if s["side"] == "buy" else "got"] += s["sol"]
        d["last"] = max(d["last"], s["ts"])
        d["sells"] += s["side"] == "sell"
    closed = {m: d for m, d in by.items() if d["spent"] > 0 and d["sells"] > 0}
    span_days = max(1 / 24, (swaps[-1]["ts"] - swaps[0]["ts"]) / 86400) if swaps else 1
    res = {"tokens": len(by), "closed": len(closed), "swaps": len(swaps), "per_day": round(len(swaps) / span_days, 1)}
    if not closed:
        return {**res, "verdict": "pocos datos", "why": "aún no tiene operaciones cerradas que medir"}
    roi = np.array([d["got"] / d["spent"] - 1 for d in closed.values()])
    pnl = np.array([d["got"] - d["spent"] for d in closed.values()])
    order = sorted(closed.values(), key=lambda d: d["first"])
    half = len(order) // 2
    h1 = sum(d["got"] - d["spent"] for d in order[:half])
    h2 = sum(d["got"] - d["spent"] for d in order[half:])
    best = int(np.argmax(pnl))
    no_best = float(pnl.sum() - pnl[best])
    hold = [d["last"] - d["first"] for d in closed.values()]
    p = _boot_p(np.clip(roi, -1, 20))               # recorta >21x para que un único bombazo no lo decida todo
    res.update({"pnl_sol": round(float(pnl.sum()), 3), "roi_pct": round(100 * float(pnl.sum() / sum(d["spent"] for d in closed.values())), 1),
                "win_rate": round(100 * float((roi > 0).mean()), 1), "median_roi_pct": round(100 * float(np.median(roi)), 1),
                "best_x": round(float(roi.max()) + 1, 2), "pnl_without_best_sol": round(no_best, 3),
                "best_share_pct": round(100 * float(pnl[best] / pnl.sum()), 0) if pnl.sum() > 0 else None,
                "halves_positive": bool(h1 > 0 and h2 > 0) if half else None, "p_value": round(p, 3),
                "median_hold_min": round(float(np.median(hold)) / 60, 1),
                "avg_size_sol": round(float(np.mean([d["spent"] for d in closed.values()])), 3)})
    if alpha:
        res["alpha_pct"] = round(100 * float(np.mean(alpha)), 1)
        res["alpha_n"] = len(alpha)
    # veredicto
    if res["per_day"] > 300 or res["median_hold_min"] < 0.5:
        v, why = "bot", "opera cientos de veces al día o vende en segundos: no es copiable"
    elif len(closed) < 10:
        v, why = "pocos datos", f"solo {len(closed)} tokens cerrados (mínimo 10)"
    elif pnl.sum() <= 0:
        v, why = "pierde", "en conjunto pierde dinero"
    elif p < 0.05 and no_best > 0 and res["halves_positive"] and (alpha is None or len(alpha) < 3 or res["alpha_pct"] > 0):
        v, why = "habilidad probable", ("gana de forma constante: también sin su mejor operación, en las dos mitades de "
                                        "su historial y con menos de un 5 % de probabilidad de ser azar")
    else:
        fails = []
        if no_best <= 0:
            fails.append(f"casi todo viene de una sola operación ({res['best_x']}x)")
        if not res["halves_positive"]:
            fails.append("no gana en las dos mitades de su historial")
        if p >= 0.05:
            fails.append(f"el resultado podría ser azar (p = {p:.2f})")
        if alpha and len(alpha) >= 3 and res["alpha_pct"] <= 0:
            fails.append("no lo hace mejor que los demás que compraron lo mismo a la vez")
        v, why = "posible suerte", "; ".join(fails) or "no supera las pruebas"
    return {**res, "verdict": v, "why": why}


def alpha_by_wallet(db: dict, launches: list[dict], exit_rule: dict | None = None) -> dict[str, list[float]]:
    """Por cartera: su resultado de copia menos la media de los demás compradores tempranos del mismo token."""
    from research import moonshot
    ex = exit_rule or moonshot.DEFAULT_EXIT
    by_pool = {L["pool"]: L for L in launches}
    out: dict[str, list[float]] = {}
    for p, v in (db.get("pools") or {}).items():
        L = by_pool.get(p)
        if not L or v.get("busy"):
            continue
        first = {}
        for w, ts in v["buyers"]:
            if ts and (w not in first or ts < first[w]):
                first[w] = ts
        rets = {}
        for w, ts in first.items():
            i = int((ts + COPY_DELAY_S - L["created"]) // 60) - L["off"]
            if 0 <= i < len(L["c"]) - 2:
                rets[w] = moonshot.simulate(L, i, ex)
        if len(rets) < 3:
            continue
        for w, r in rets.items():
            others = [x for k, x in rets.items() if k != w]
            out.setdefault(w, []).append(r - float(np.mean(others)))
    return out


def analyze_traders(rpc: RPC | None, db: dict, launches: list[dict], prev: dict, now: float, per_round: int = 15,
                    exit_rule: dict | None = None) -> dict:
    """Perfiles de las carteras que más aparecen comprando pronto. Se refresca cada 24 h por cartera."""
    counts: dict[str, int] = {}
    for v in (db.get("pools") or {}).values():
        for w in {w for w, _ in v.get("buyers", [])}:
            counts[w] = counts.get(w, 0) + 1
    alpha = alpha_by_wallet(db, launches, exit_rule)
    profiles = dict(prev.get("profiles") or {})
    candidates = [w for w, k in sorted(counts.items(), key=lambda kv: -kv[1]) if k >= 2][:200]
    done = 0
    for w in candidates:
        if done >= per_round or not rpc:
            break
        if w in profiles and now - profiles[w].get("ts", 0) < 86400:
            continue
        try:
            sw = profile(rpc, w)
        except (httpx.HTTPError, RuntimeError) as e:
            log.warning("RPC: %s", e)
            break
        profiles[w] = {"ts": int(now), "early_buys": counts[w], **trader_metrics(sw, alpha.get(w))}
        done += 1
    for w, pr in profiles.items():                    # refrescar alpha aunque no se vuelva a leer la cartera
        if w in alpha and pr.get("closed"):
            pr["alpha_pct"], pr["alpha_n"] = round(100 * float(np.mean(alpha[w])), 1), len(alpha[w])
    tally: dict[str, int] = {}
    for pr in profiles.values():
        tally[pr["verdict"]] = tally.get(pr["verdict"], 0) + 1
    return {"ts": int(now), "profiles": profiles, "tally": tally, "analyzed_now": done}
