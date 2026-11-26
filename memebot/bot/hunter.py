"""Modo cazador y copia de carteras, SIEMPRE en simulado (dinero ficticio).

Cada minuto:
  · Cazador: vigila memecoins recién nacidas; a los E minutos, si pasan la regla aprendida en
    research/moonshot.py y RugCheck no ve peligros, hace una apuesta pequeña simulada. Al llegar a 2x vende la
    mitad (recupera lo invertido) y deja correr el resto con stop dinámico.
  · Copia: si una cartera vigilada (bot/copytrade.py) compra un token, lo copia en simulado.
Cada 6 h repite los estudios. Solo avisa por Telegram si el estudio demuestra ventaja en datos no vistos;
si no, sigue midiendo en silencio y lo ves en el panel. Nunca usa dinero real.

Se ejecuta dentro del servicio del cerebro (bot/brain.py lo arranca en segundo plano).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

import httpx

log = logging.getLogger("hunter")
DEX = "https://api.dexscreener.com/latest/dex"
GECKO_NEW = "https://api.geckoterminal.com/api/v2/networks/solana/new_pools"
RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"
START_USD = 1000.0
BET_USD = float(os.getenv("HUNTER_BET_USD", "10"))          # 1 % del saldo simulado por apuesta
MAX_OPEN = 25
GLITCH_X = 20          # un salto de ×20 entre dos lecturas seguidas se trata como dato erróneo si no lo confirma Jupiter
DEFAULT_RULE = {"entry_min": 15, "min_vol": 10_000, "mom5": 0.0, "runup_max": 10.0, "stop": 0.5, "trail": 0.4,
                "hours": 24}


def _read(p: Path, default=None):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {} if default is None else default


def _write(p: Path, data):
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=float))
    tmp.replace(p)


def new_sleeve() -> dict:
    return {"start": START_USD, "cash": START_USD, "positions": {}, "closed": []}


def sleeve_equity(s: dict, prices: dict | None = None) -> float:
    eq = s["cash"]
    for k, p in s["positions"].items():
        px = (prices or {}).get(k, p.get("last", p["entry"]))
        eq += p["qty"] * px
    return eq


def summary(s: dict) -> dict:
    rets = [c["ret"] for c in s["closed"]]
    eq = sleeve_equity(s)
    return {"equity": round(eq, 2), "pnl": round(eq - s["start"], 2), "open": len(s["positions"]),
            "closed": len(rets), "win_rate": round(100 * sum(r > 0 for r in rets) / len(rets), 1) if rets else None,
            "best_x": round(max(rets) + 1, 2) if rets else None, "worst_pct": round(100 * min(rets), 1) if rets else None,
            "mean_pct": round(100 * sum(rets) / len(rets), 2) if rets else None}


def open_pos(s: dict, key: str, info: dict, price: float, rule: dict, now: float, cost: float) -> dict | None:
    if key in s["positions"] or len(s["positions"]) >= MAX_OPEN or price <= 0:
        return None
    bet = min(BET_USD, s["cash"])
    if bet < 1:
        return None
    entry = price * (1 + cost)
    p = {**info, "entry": entry, "qty": bet / entry, "cost_usd": bet, "got_usd": 0.0, "ts": int(now), "half": False,
         "peak": 1.0, "last": price, "stop": rule["stop"], "trail": rule["trail"], "hours": rule["hours"],
         "missing_since": None}
    s["cash"] -= bet
    s["positions"][key] = p
    return p


def step_pos(s: dict, key: str, price: float | None, now: float, cost: float) -> dict | None:
    """Aplica las salidas del cazador. Devuelve la operación cerrada (o un aviso de venta parcial)."""
    p = s["positions"][key]
    if price is None:                                   # el par desaparece: si dura 1 h, se da por perdido
        p["missing_since"] = p.get("missing_since") or now
        if now - p["missing_since"] < 3600:
            return None
        price, why = 0.0, "desaparecido (posible rug)"
    else:
        p["missing_since"] = None
        why = None
    p["last"] = price
    r = price / p["entry"] if p["entry"] else 0
    if why is None:
        if not p["half"] and r <= 1 - p["stop"]:
            why = f"stop −{int(p['stop'] * 100)} %"
        elif not p["half"] and r >= 2:
            sell = p["qty"] / 2
            p["got_usd"] += sell * price * (1 - cost)
            s["cash"] += sell * price * (1 - cost)
            p["qty"] -= sell
            p["half"], p["peak"] = True, r
            return {"partial": True, **p}
        elif p["half"]:
            p["peak"] = max(p["peak"], r)
            if r <= p["peak"] * (1 - p["trail"]):
                why = f"stop dinámico ({int(p['trail'] * 100)} % desde el máximo {p['peak']:.1f}x)"
        if why is None and now - p["ts"] >= p["hours"] * 3600:
            why = f"plazo de {p['hours']} h"
    if why is None:
        return None
    got = p["qty"] * price * (1 - cost)
    s["cash"] += got
    p["got_usd"] += got
    c = {**p, "exit": price, "exit_ts": int(now), "reason": why, "ret": p["got_usd"] / p["cost_usd"] - 1}
    del s["positions"][key]
    s["closed"] = (s["closed"] + [c])[-300:]
    return c


class Hunter:
    def __init__(self, state: Path, data: Path, http: httpx.Client | None = None, rpc=None, notify=None):
        from research.moonshot import COST_SIDE
        self.state, self.data = state, data
        self.http = http or httpx.Client(timeout=20, headers={"Accept": "application/json"})
        self.rpc = rpc
        self.cost = COST_SIDE
        self.notify = notify or (lambda t: None)
        self.f = state / "hunter.json"
        self.db = _read(self.f) or {}
        self.db.setdefault("sleeves", {"cazador": new_sleeve(), "copia": new_sleeve()})
        self.repair()
        self.db.setdefault("watch", {})
        self.db.setdefault("seen_sigs", {})
        self.last = {"gecko": 0.0, "study": 0.0, "copy_poll": 0.0, "copy_record": 0.0}

    def repair(self):
        """Precios absurdos guardados (p. ej. un dato erróneo de DexScreener) inflaban el saldo simulado: las
        posiciones abiertas valoradas a más de ×50 de su entrada vuelven a valorarse a la entrada hasta tener
        un precio fiable, y se recalcula el resumen."""
        for s in self.db["sleeves"].values():
            for p in s["positions"].values():
                if p.get("entry") and p.get("last", 0) > p["entry"] * 50:
                    p["last"], p["peak"], p["suspect_price"] = p["entry"], 1.0, True
        self.db["summary"] = {k: summary(s) for k, s in self.db["sleeves"].items()}

    # ---------------------------------------------------------------- datos de mercado
    def pairs(self, addrs: list[str]) -> dict[str, dict]:
        out = {}
        for k in range(0, len(addrs), 30):
            try:
                r = self.http.get(f"{DEX}/pairs/solana/{','.join(addrs[k:k + 30])}")
                for p in (r.json().get("pairs") or []) if r.status_code == 200 else []:
                    out[p.get("pairAddress")] = p
            except (httpx.HTTPError, ValueError) as e:
                log.warning("DexScreener: %s", e)
        return out

    def best_pair_for(self, mint: str) -> dict | None:
        try:
            r = self.http.get(f"{DEX}/tokens/{mint}")
            ps = [p for p in (r.json().get("pairs") or []) if p.get("chainId") == "solana"]
        except (httpx.HTTPError, ValueError):
            return None
        return max(ps, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0) if ps else None

    def entry_price(self, mint: str, dex_price: float) -> float:
        """Precio de entrada: el de Jupiter (fiable y al momento) si lo hay; si no, DexScreener."""
        from .market import jupiter_prices
        try:
            return jupiter_prices([mint], client=self.http).get(mint) or dex_price
        except Exception:  # noqa: BLE001
            return dex_price

    def rug_ok(self, mint: str) -> bool:
        try:
            r = self.http.get(RUGCHECK.format(mint=mint))
            if r.status_code != 200:
                return False
            return not any((x.get("level") or "").lower() == "danger" for x in r.json().get("risks") or [])
        except (httpx.HTTPError, ValueError):
            return False

    def discover(self, now: float):
        """Lanzamientos nuevos: GeckoTerminal (1 consulta cada 2 min) + la muestra de data_launches."""
        w = self.db["watch"]
        if now - self.last["gecko"] >= 120:
            self.last["gecko"] = now
            try:
                r = self.http.get(GECKO_NEW, params={"page": 1})
                for p in r.json().get("data", []) if r.status_code == 200 else []:
                    a, rel = p["attributes"], p.get("relationships", {})
                    base = ((rel.get("base_token") or {}).get("data") or {}).get("id", "").replace("solana_", "")
                    from datetime import datetime
                    created = datetime.fromisoformat(a["pool_created_at"].replace("Z", "+00:00")).timestamp()
                    w.setdefault(a["address"], {"created": int(created), "mint": base, "name": a.get("name", ""),
                                                "checked": False})
            except (httpx.HTTPError, ValueError, KeyError) as e:
                log.warning("GeckoTerminal: %s", e)
        for addr, m in _read(self.data / "pools.json").items():
            if now - m.get("created_at", 0) < 2 * 3600:
                w.setdefault(addr, {"created": m["created_at"], "mint": m.get("base_mint"), "name": m.get("name", ""),
                                    "checked": False})
        for k in [k for k, v in w.items() if now - v["created"] > 3 * 3600]:
            del w[k]

    # ---------------------------------------------------------------- ciclo
    def rule(self) -> tuple[dict, bool]:
        st = _read(self.state / "moonshot.json")
        r = {**DEFAULT_RULE, **(st.get("rule") or {})}
        if r.get("runup_max") is None:              # sin límite de subida previa
            r["runup_max"] = float("inf")
        return r, bool(st.get("edge"))

    def hunt(self, now: float):
        rule, edge = self.rule()
        E = rule["entry_min"] * 60
        due = [a for a, v in self.db["watch"].items() if not v["checked"] and E <= now - v["created"] <= E + 300]
        if not due:
            return
        info = self.pairs(due)
        s = self.db["sleeves"]["cazador"]
        from research.moonshot import rule_ok
        for a in due:
            v = self.db["watch"][a]
            v["checked"] = True
            p = info.get(a)
            if not p:
                continue
            age_min = (now - v["created"]) / 60
            vol = (p.get("volume") or {}).get("h1" if age_min <= 60 else "h6") or 0
            pc = p.get("priceChange") or {}
            f = {"vol": float(vol), "mom5": float(pc.get("m5") or 0) / 100, "runup": 1 + float(pc.get("h1") or 0) / 100}
            if not rule_ok(f, rule) or not self.rug_ok(v["mint"]):
                continue
            price = self.entry_price(v["mint"], float(p.get("priceUsd") or 0))
            pos = open_pos(s, a, {"pair": a, "mint": v["mint"], "name": v["name"], "source": "cazador"}, price, rule,
                           now, self.cost)
            if pos and edge:
                self.notify(f"🎯 CAZADOR (simulado) · 🟢 COMPRA {v['name']}\nApuesta {pos['cost_usd']:.0f} $ a los "
                            f"{age_min:.0f} min de nacer · volumen {vol:,.0f} $\nPlan: vender la mitad a 2x, el resto "
                            f"con stop dinámico del {int(rule['trail'] * 100)} %; stop −{int(rule['stop'] * 100)} %; "
                            f"plazo {rule['hours']} h.\nhttps://dexscreener.com/solana/{a}")

    def copy(self, now: float):
        cp = _read(self.state / "copy.json")
        prof = _read(self.state / "traders.json").get("profiles") or {}
        skilled = sorted((w for w, v in prof.items() if v.get("verdict") == "habilidad probable"),
                         key=lambda w: -(prof[w].get("pnl_without_best_sol") or 0))
        bad = {w for w, v in prof.items() if v.get("verdict") in ("bot", "pierde")}
        wallets = (skilled + [w["wallet"] for w in cp.get("watch") or [] if w["wallet"] not in bad])
        wallets = list(dict.fromkeys(wallets))[:10]        # primero las de habilidad probada
        if not (self.rpc and wallets) or now - self.last["copy_poll"] < 120:
            return
        self.last["copy_poll"] = now
        from . import copytrade
        rule, _ = self.rule()
        s = self.db["sleeves"]["copia"]
        for b in copytrade.poll(self.rpc, wallets, self.db["seen_sigs"], now=now):
            pair = self.best_pair_for(b["mint"])
            if not pair or ((pair.get("liquidity") or {}).get("usd") or 0) < 5_000:
                continue
            key = pair["pairAddress"]
            name = (pair.get("baseToken") or {}).get("symbol", b["mint"][:6])
            pos = open_pos(s, key, {"pair": key, "mint": b["mint"], "name": name, "source": "copia",
                                    "wallet": b["wallet"]}, self.entry_price(b["mint"], float(pair.get("priceUsd") or 0)),
                                    rule, now, self.cost)
            if pos and cp.get("edge"):
                self.notify(f"👥 COPIA (simulado) · 🟢 COMPRA {name}\nLa cartera {b['wallet'][:4]}…{b['wallet'][-4:]} "
                            f"lo acaba de comprar · apuesta {pos['cost_usd']:.0f} $\n"
                            f"https://dexscreener.com/solana/{key}")

    def manage(self, now: float):
        keys = [k for s in self.db["sleeves"].values() for k in s["positions"]]
        if not keys:
            return
        info = self.pairs(keys)
        from .market import jupiter_prices                   # precio al momento y fiable (rutas reales de venta)
        mints = list({p.get("mint") for s in self.db["sleeves"].values() for p in s["positions"].values() if p.get("mint")})
        try:
            jup = jupiter_prices(mints, client=self.http)
        except Exception:  # noqa: BLE001
            jup = {}
        _, edge = self.rule()
        copy_edge = bool(_read(self.state / "copy.json").get("edge"))
        for name, s in self.db["sleeves"].items():
            for k in list(s["positions"]):
                p = info.get(k)
                pos = s["positions"][k]
                jpx = jup.get(pos.get("mint"))
                px = jpx or (float(p["priceUsd"]) if p and p.get("priceUsd") else None)
                ref = pos.get("last") or pos["entry"]
                if px and not jpx and (px > ref * GLITCH_X or px < ref / GLITCH_X):
                    continue                               # salto imposible sin confirmar: dato erróneo, se ignora
                pos.pop("suspect_price", None)
                ev = step_pos(s, k, px, now, self.cost)
                if not ev or not (edge if name == "cazador" else copy_edge):
                    continue
                tag = "🎯 CAZADOR" if name == "cazador" else "👥 COPIA"
                if ev.get("partial"):
                    self.notify(f"{tag} (simulado) · 💰 VENTA DE LA MITAD a 2x · {ev['name']}: lo invertido ya está "
                                f"recuperado; el resto sigue con stop dinámico.")
                else:
                    head = "✅ CIERRE CON BENEFICIO" if ev["ret"] >= 0 else "❌ CIERRE CON PÉRDIDA"
                    self.notify(f"{tag} (simulado) · {head} · {ev['name']}\n{ev['ret'] * 100:+.0f}% "
                                f"({ev['got_usd'] - ev['cost_usd']:+.2f} $) · motivo: {ev['reason']}")

    def studies(self, now: float):
        if now - self.last["study"] < 6 * 3600:
            return
        self.last["study"] = now
        from research import moonshot
        from . import copytrade
        launches = moonshot.load(self.data, now)
        res = moonshot.study(launches)
        _write(self.state / "moonshot.json", res)
        rule = res.get("rule")
        cp = copytrade.evaluate(_read(self.data / "wallets.json"), launches,
                                {k: rule[k] for k in ("stop", "trail", "hours")} if rule else None)
        cp["rpc"] = bool(self.rpc)
        _write(self.state / "copy.json", cp)
        # ¿habilidad o suerte? perfil real de cada cartera que compra pronto (compras y ventas en la blockchain)
        prev = _read(self.state / "traders.json")
        tr = copytrade.analyze_traders(self.rpc, _read(self.data / "wallets.json"), launches, prev, now,
                                       exit_rule={k: rule[k] for k in ("stop", "trail", "hours")} if rule else None)
        pr = tr["profiles"]
        if len(pr) > 500:                                  # quedarse con las más relevantes
            keep = sorted(pr, key=lambda w: (pr[w]["verdict"] == "habilidad probable", pr[w].get("ts", 0)))[-500:]
            tr["profiles"] = {w: pr[w] for w in keep}
        old = {w for w, v in (prev.get("profiles") or {}).items() if v.get("verdict") == "habilidad probable"}
        new = [w for w, v in tr["profiles"].items() if v.get("verdict") == "habilidad probable" and w not in old]
        _write(self.state / "traders.json", tr)
        for w in new[:3]:
            v = tr["profiles"][w]
            self.notify(f"🧠 TRADER CON HABILIDAD PROBABLE · {w[:4]}…{w[-4:]}\n{v['closed']} tokens cerrados · "
                        f"acierto {v['win_rate']}% · {v['pnl_sol']:+.2f} SOL ({v['roi_pct']:+.0f}%) · sin su mejor "
                        f"operación {v['pnl_without_best_sol']:+.2f} SOL · p = {v['p_value']}\n"
                        f"Se copia en simulado para comprobar si copiarle con retraso también gana.\n"
                        f"https://solscan.io/account/{w}")
        log.info("Estudios: cazador %s · copia %s", res.get("note", ""), cp.get("note", ""))

    def record_wallets(self, now: float):
        if not self.rpc or now - self.last["copy_record"] < 300:
            return
        self.last["copy_record"] = now
        from . import copytrade
        try:
            n = copytrade.record(self.rpc, self.data, int(os.getenv("COPY_POOLS_PER_DAY", "60")), now)
            if n:
                log.info("Copia: apuntados los primeros compradores de %d lanzamientos", n)
        except Exception as e:  # noqa: BLE001
            log.warning("Copia: %s", e)

    def tick(self, now: float | None = None):
        now = now or time.time()
        for fn in (self.studies, self.discover, self.hunt, self.copy, self.manage, self.record_wallets):
            try:
                fn(now)
            except Exception:  # noqa: BLE001
                log.exception("Cazador: fallo en %s", fn.__name__)
        self.db["summary"] = {k: summary(s) for k, s in self.db["sleeves"].items()}
        self.db["ts"] = int(now)
        self.db["watch_n"] = len(self.db["watch"])
        _write(self.f, self.db)


def start_background(state: str, data_dirs: list[str]):
    """Lo arranca el servicio del cerebro: un hilo que da una vuelta por minuto."""
    if os.getenv("HUNTER_ENABLED", "1") == "0":
        return None
    data = Path(next((d for d in data_dirs if "launch" in d), "data_launches"))

    def loop():
        from . import copytrade
        from .watchdog import notify
        h = Hunter(Path(state), data, rpc=copytrade.rpc_from_env(), notify=notify)
        while True:
            t0 = time.time()
            h.tick()
            time.sleep(max(5, 60 - (time.time() - t0)))

    t = threading.Thread(target=loop, name="hunter", daemon=True)
    t.start()
    return t
