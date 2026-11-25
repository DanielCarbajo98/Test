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
