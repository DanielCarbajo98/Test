"""Bucle principal.

  1. Gestionar posiciones abiertas (salidas)  -> primero proteger lo que ya tienes
  2. Descubrir candidatos (DexScreener) y prefiltrar con datos en tiempo real
  3. Descargar velas de 1 min (GeckoTerminal) y aplicar la estrategia validada (bot/signals.py)
  4. Comprobaciones anti-rug -> tamaño de posición -> compra

Uso:
  python -m bot.main --config config.yaml          # bucle continuo
  python -m bot.main --config config.yaml --once   # un solo ciclo
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from collections import deque
from datetime import datetime, timezone

import httpx

from . import context as mctx
from . import version
from .brain import BrainRuntime
from .config import Config, ExitCfg, load_config
from .executor import JupiterExecutor, PaperExecutor, Quoter
from .market import DEFAULT_SOURCES, Candles, discover
from .models import Position
from .portfolio import Portfolio
from .risk import can_open, evaluate_exit, position_size_usd
from .safety import SafetyChecker, SolanaRPC
from .scanner import DexScreener
from .signals import explain, latest_signal
from .strategy import prefilter, rank

log = logging.getLogger("memebot")


class Notifier:
    def __init__(self, token: str, chat_id: str):
        self.token, self.chat_id = token, chat_id
        self.events: deque = deque(maxlen=50)   # se muestran en el panel

    def send(self, text: str):
        log.info(text)
        self.events.appendleft({"ts": int(time.time()), "text": text})
        if not (self.token and self.chat_id):
            return
        try:
            httpx.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                       json={"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}, timeout=10)
        except httpx.HTTPError as e:
            log.warning("Telegram falló: %s", e)


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.dex = DexScreener()
        self.candles = Candles()
        self.rpc = SolanaRPC(cfg.rpc_url)
        if cfg.mode == "live":
            self.exe = JupiterExecutor(cfg.jupiter_api_key, cfg.wallet_private_key, cfg.quote_mint,
                                       cfg.quote_decimals, cfg.risk.slippage_bps, cfg.risk.max_price_impact_pct,
                                       rpc=self.rpc)
        else:
            self.exe = PaperExecutor(cfg.quote_mint, cfg.quote_decimals, Quoter(cfg.jupiter_api_key, cfg.risk.slippage_bps))
        self.safety = SafetyChecker(cfg.safety, self.rpc, sell_probe=self.exe.can_sell)
        self.pf = Portfolio(cfg.state_dir, cfg.risk.starting_equity_usd)
        self.notify = Notifier(cfg.telegram_token, cfg.telegram_chat_id)
        self._last_summary_day = ""
        self._last_entry_scan = 0.0
        self._last_analyzed: dict[str, float] = {}
        self._chart_index: dict[str, dict] = {}
        self._analysis: dict[str, tuple] = {}     # pool -> (signal, feat, checks, pred) del último análisis
        self.ctx: dict = {}
        self._ctx_ts = 0.0
        self.risk_off = ""
        self._disc: tuple = ()
        self._disc_ts = 0.0
        self._last_equity_log = 0.0
        self.started_at = time.time()
        self.last_scan: dict = {}
        self.liquidity: dict[str, float] = {}
        self.paused = False
        self.pending_reclaim: dict[str, int] = {}   # mint -> intentos de recuperar el rent
        self.brain = BrainRuntime(cfg.state_dir)
        self._brain_version_seen = 0

    # ------------------------------------------------------------------ cerebro
    def brain_active(self) -> bool:
        b = self.cfg.brain
        return b.enabled and self.brain.champion is not None and (self.cfg.mode == "paper" or b.auto_apply_live)

    def exit_cfg(self) -> ExitCfg:
        if self.brain_active():
            ov = self.brain.exits()
            if ov:
                base = self.cfg.exits
                return ExitCfg(**{**base.__dict__, **ov})
        return self.cfg.exits

    def brain_blocks_entries(self) -> bool:
        if not self.cfg.brain.enabled or not self.brain.defensive:
            return False
        return self.cfg.mode == "live" or self.cfg.brain.defensive_in_paper

    def strategy_label(self) -> str:
        if self.brain_active():
            c = self.brain.champion
            return f"{c['strategy']} v{c['version']} (cerebro)"
        return self.cfg.strategy.name

    # ------------------------------------------------------------------ control desde el panel
    def read_control(self):
        """state/control.json lo escribe el panel: {"paused": bool, "close_all": bool}."""
        f = self.pf.dir / "control.json"
        try:
            ctl = json.loads(f.read_text()) if f.exists() else {}
        except (OSError, ValueError):
            return
        if bool(ctl.get("paused")) != self.paused:
            self.paused = bool(ctl.get("paused"))
            self.notify.send("⏸️ Entradas en pausa (desde el panel)" if self.paused else "▶️ Entradas reanudadas")
        if ctl.get("close_all"):
            ctl["close_all"] = False
            f.write_text(json.dumps(ctl))
            self.close_all("cierre manual")

    def close_all(self, reason: str):
        if not self.pf.positions:
            return
        snaps = self.dex.best_pairs(list(self.pf.positions))
        for mint, pos in list(self.pf.positions.items()):
            s = snaps.get(mint)
            price = s.price_usd if s else self.pf.last_prices.get(mint, pos.entry_price)
            liq = s.liquidity_usd if s else pos.entry_liquidity
            try:
                fill = self.exe.sell(mint, pos.decimals, pos.qty, price, liq)
            except Exception as e:  # noqa: BLE001
                self.notify.send(f"⚠️ No se pudo cerrar {pos.symbol}: {e}")
                continue
            pnl = self.pf.reduce(mint, fill.qty, fill.usd, fill.price, reason, fill.tx)
            self.notify.send(f"🔴 SELL {pos.symbol} por ${fill.usd:.2f} — {reason} (PnL ${pnl:+.2f})")
            self.pending_reclaim[mint] = 0

    # ------------------------------------------------------------------ salidas
    def manage_positions(self):
        if not self.pf.positions:
            return
        snaps = self.dex.best_pairs(list(self.pf.positions))
        now = time.time()
        for mint, pos in list(self.pf.positions.items()):
            s = snaps.get(mint)
            if s is None:
                log.warning("Sin datos para %s; se reintentará", pos.symbol)
                continue
            self.pf.last_prices[mint] = s.price_usd
            self.liquidity[mint] = s.liquidity_usd
            d = evaluate_exit(pos, s.price_usd, s.liquidity_usd, now, self.exit_cfg())
            if d is None:
                continue
            try:
                fill = self.exe.sell(mint, pos.decimals, d.sell_qty, s.price_usd, s.liquidity_usd)
            except Exception as e:  # noqa: BLE001
                self.notify.send(f"⚠️ Venta fallida {pos.symbol} ({d.reason}): {e}")
                continue
            pos.tp_levels_hit = d.new_tp_levels
            pnl = self.pf.reduce(mint, fill.qty, fill.usd, fill.price, d.reason, fill.tx)
            self.notify.send(f"🔴 SELL {pos.symbol} {fill.qty:.4g} por ${fill.usd:.2f} — {d.reason} (PnL ${pnl:+.2f})")
            if mint not in self.pf.positions:
                self.pending_reclaim[mint] = 0
        self.pf.save()

    def reclaim(self):
        """Recupera el SOL bloqueado en cuentas de token vacías (solo live)."""
        if not hasattr(self.exe, "reclaim_rent"):
            self.pending_reclaim.clear()
            return
        for mint, tries in list(self.pending_reclaim.items()):
            try:
                n = self.exe.reclaim_rent(mint)
                if n:
                    log.info("Recuperado el depósito de %d cuenta(s) de %s", n, mint)
                    del self.pending_reclaim[mint]
                    continue
            except Exception as e:  # noqa: BLE001
                log.info("Recuperación de rent pendiente para %s: %s", mint, e)
            self.pending_reclaim[mint] = tries + 1
            if tries + 1 >= 5:
                del self.pending_reclaim[mint]

    # ------------------------------------------------------------------ entradas
    def _discover(self) -> tuple[list[str], set[str]]:
        """Universo: DexScreener (perfiles/boosts) + GeckoTerminal (tendencia y más activos). Caché 2 min."""
        c = self.cfg
        if time.time() - self._disc_ts < c.scanner.discovery_cache_seconds and self._disc:
            return self._disc
        addrs, boosted = self.dex.latest_token_addresses(c.scanner.include_boosted, c.scanner.include_profiles)
        if c.scanner.include_gecko:
            addrs += [m for m in discover(self.candles, DEFAULT_SOURCES) if m not in addrs]
        self._disc, self._disc_ts = (addrs, boosted), time.time()
        return self._disc

    def _explain(self, feat: dict, pred: float | None) -> list[dict]:
        """Qué condiciones cumple y cuáles no la estrategia activa (para el panel)."""
        if not feat:
            return [{"label": "Velas de 1 minuto", "ok": False, "value": "no disponibles", "rule": "se reintenta"}]
        if self.brain_active():
            champ = self.brain.champion
            if champ["kind"] == "rule":
                return explain(champ["strategy"], feat, champ.get("params", {}))
            rows = [
                {"label": "Volumen última hora", "ok": feat.get("vol_60m", 0) >= 10_000,
                 "value": f"{feat.get('vol_60m', 0) / 1e3:.0f} k$", "rule": "≥ 10 k$"},
                {"label": "Minutos activos en 1 h", "ok": feat.get("active_60m", 0) >= 20,
                 "value": f"{feat.get('active_60m', 0):.0f}", "rule": "≥ 20"},
            ]
            if pred is not None:
                thr = champ.get("threshold", 0)
                rows.append({"label": "Resultado esperado por el modelo", "ok": pred >= thr,
                             "value": f"{100 * pred:+.1f}%", "rule": f"≥ {100 * thr:+.1f}%"})
            return rows
        return explain(self.cfg.strategy.name, feat, self.cfg.strategy.params)

    def _save_chart(self, pool: str, mint: str, symbol: str, rows: list, signal: bool, feat: dict,
                    checks: list[dict], pred: float | None, extra: dict | None = None):
        """Guarda velas + análisis para el gráfico en directo del panel (sin peticiones extra)."""
        if not rows:
            return
        try:
            d = self.pf.dir / "charts"
            d.mkdir(exist_ok=True)
            data = {"pool": pool, "mint": mint, "symbol": symbol, "ts": int(time.time()), "signal": signal,
                    "strategy": self.strategy_label(), "checks": checks, "pred": pred,
                    "features": {k: round(v, 6) for k, v in feat.items() if k != "valid"},
                    "candles": rows[-420:], **(extra or {})}
            tmp = d / f"{pool}.tmp"
            tmp.write_text(json.dumps(data))
            tmp.replace(d / f"{pool}.json")
            self._chart_index[pool] = {"pool": pool, "mint": mint, "symbol": symbol, "ts": data["ts"],
                                       "signal": signal, "position": bool((extra or {}).get("position")),
                                       "failed": sum(1 for c in checks if not c["ok"]), "checks": len(checks),
                                       "price": float(rows[-1][4])}
            keep = {p.pool for p in self.pf.positions.values()}
            for k in sorted(self._chart_index, key=lambda k: self._chart_index[k]["ts"])[:-60]:
                if k not in keep:                # máximo 60 gráficos guardados
                    self._chart_index.pop(k, None)
                    (d / f"{k}.json").unlink(missing_ok=True)
            for k, v in self._chart_index.items():
                v["position"] = k in keep
            tmp = d / "index.tmp"
            tmp.write_text(json.dumps(list(self._chart_index.values())))
            tmp.replace(d / "index.json")
        except OSError as e:
            log.debug("No se pudo guardar el gráfico: %s", e)

    def refresh_context(self):
        """Noticias, miedo/codicia y tendencia de SOL; activa o levanta el freno por contexto."""
        c = self.cfg.context
        if not c.enabled or time.time() - self._ctx_ts < c.refresh_minutes * 60:
            return
        self._ctx_ts = time.time()
        try:
            self.ctx = mctx.build()
            mctx.save(self.pf.dir / "context.json", self.ctx)
        except Exception as e:  # noqa: BLE001
            log.warning("Contexto no disponible: %s", e)
            return
        why = mctx.risk_off(self.ctx, c.pause_sol_h1_drop, c.pause_sol_h24_drop, c.pause_on_critical_news)
        if why and not self.risk_off:
            self.notify.send(f"🛑 Freno por contexto de mercado: {why}. No se abren posiciones nuevas.")
        elif self.risk_off and not why:
            self.notify.send("✅ El contexto de mercado se ha normalizado: se reanudan las entradas.")
        self.risk_off = why

    def refresh_position_charts(self):
        """Velas de las posiciones abiertas (para ver en directo dónde está cada una)."""
        for mint, pos in self.pf.positions.items():
            if not pos.pool:
                continue
            rows = self.candles.minute(pos.pool, limit=240)
            sig, feat, checks, pred = self._analysis.get(pos.pool, (False, {}, [], None))
            self._save_chart(pos.pool, mint, pos.symbol, rows, sig, feat, checks, pred, {"position": True})

    def find_entries(self):
        c = self.cfg
        ok, why = can_open(len(self.pf.positions), self.pf.daily_pnl(), self.pf.start_equity_today(), c.risk,
                           self.pf.equity())
        if ok and self.risk_off:
            ok, why = False, f"freno por contexto: {self.risk_off}"
        if not ok:
            log.info("No se abren posiciones (%s); se sigue analizando el mercado", why)
        addrs, boosted = self._discover()
        addrs = [a for a in addrs if a not in self.pf.positions
                 and not self.pf.in_cooldown(a, c.risk.cooldown_minutes)][: c.scanner.max_candidates]
        snaps = self.dex.best_pairs(addrs, boosted)

        passed = []
        for s in snaps.values():
            reasons = prefilter(s, c.filters)
            if reasons:
                log.debug("%s descartado: %s", s.symbol, "; ".join(reasons))
            else:
                passed.append(s)
        # Por turnos: primero los que llevan más tiempo sin analizarse; a igualdad, los más activos.
        now = time.time()
        passed.sort(key=lambda x: (self._last_analyzed.get(x.pair_address, 0), -rank(x)))
        log.info("Candidatos: %d descubiertos, %d con datos, %d pasan el prefiltro", len(addrs), len(snaps), len(passed))
        self.last_scan = {"ts": int(now), "discovered": len(addrs), "with_data": len(snaps),
                          "prefilter": len(passed), "analyzed": 0, "signals": 0,
                          "queue": max(0, len(passed) - c.strategy.max_ohlcv_per_cycle)}

        for s in passed[: c.strategy.max_ohlcv_per_cycle]:
            rows = self.candles.minute(s.pair_address)
            self._last_analyzed[s.pair_address] = time.time()
            supply = s.fdv / s.price_usd
            created = int(time.time() - s.age_minutes * 60)
            pred = None
            if self.brain_active():
                signal, feat, pred = self.brain.signal(rows, created, supply, self.ctx)
            else:
                signal, feat = latest_signal(c.strategy.name, rows, created, supply, c.strategy.params)
            if pred is not None:
                feat["ml_pred"] = pred
            checks = self._explain(feat, pred)
            self._analysis[s.pair_address] = (signal, feat, checks, pred)
            if len(self._analysis) > 300:
                self._analysis.pop(next(iter(self._analysis)))
            self._log_decision(s, signal, feat, checks)
            self._save_chart(s.pair_address, s.token_address, s.symbol, rows, signal, feat, checks, pred,
                             {"liquidity": s.liquidity_usd, "fdv": s.fdv})
            self.last_scan["analyzed"] += 1
            self.last_scan["signals"] += int(signal)
            if not signal or not ok or len(self.pf.positions) >= c.risk.max_open_positions:
                continue
            rep = self.safety.check(s)
            if not rep.ok:
                log.info("%s con señal pero NO seguro: %s", s.symbol, "; ".join(rep.reasons))
                self.pf.cooldowns[s.token_address] = time.time()
                continue
            size = position_size_usd(self.pf.equity(), self.pf.cash, s.liquidity_usd, self.exit_cfg().stop_loss_pct, c.risk)
            if size < 5:
                log.info("Tamaño insuficiente para %s (%.2f USD)", s.symbol, size)
                continue
            try:
                fill = self.exe.buy(s.token_address, rep.decimals or 6, size, s.price_usd, s.liquidity_usd)
            except Exception as e:  # noqa: BLE001
                self.notify.send(f"⚠️ Compra fallida {s.symbol}: {e}")
                self.pf.cooldowns[s.token_address] = time.time()
                continue
            pos = Position(token_address=s.token_address, symbol=s.symbol, decimals=rep.decimals or 6,
                           entry_price=fill.price, qty=fill.qty, initial_qty=fill.qty, cost_usd=fill.usd,
                           realized_usd=0.0, entry_liquidity=s.liquidity_usd, opened_at=time.time(),
                           peak_price=fill.price, pool=s.pair_address)
            self.pf.open(pos, self.strategy_label(), fill.tx)
            self.notify.send(
                f"🟢 BUY {s.symbol} ${fill.usd:.2f} @ {fill.price:.6g} [{c.mode}] | {self.strategy_label()} | "
                f"liq ${s.liquidity_usd:,.0f} | 1h {feat.get('ret_60m', 0):+.0f}% | top10 {rep.top10_pct}% | "
                f"https://dexscreener.com/solana/{s.pair_address}")
        self.pf.save()

    def _log_decision(self, s, signal: bool, feat: dict, checks: list | None = None):
        """Registro de cada candidato evaluado (para validar filtros en tiempo real a posteriori)."""
        try:
            with open(self.pf.dir / "decisions.jsonl", "a") as fh:
                fh.write(json.dumps({"ts": int(time.time()), "mint": s.token_address, "pool": s.pair_address,
                                     "symbol": s.symbol, "signal": signal, "liq": s.liquidity_usd, "fdv": s.fdv,
                                     "buys_m5": s.buys_m5, "sells_m5": s.sells_m5, "buys_h1": s.buys_h1,
                                     "sells_h1": s.sells_h1, "price": s.price_usd,
                                     "failed": [x["label"] for x in (checks or []) if not x["ok"]],
                                     **{k: round(v, 4) for k, v in feat.items() if k != "valid"}}) + "\n")
        except OSError:
            pass

    def daily_summary(self):
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if day != self._last_summary_day and datetime.now(timezone.utc).hour == 0:
            self._last_summary_day = day
            self.notify.send(f"📊 Resumen: equity ${self.pf.equity():.2f}, caja ${self.pf.cash:.2f}, "
                             f"{len(self.pf.positions)} posiciones abiertas")

    def write_status(self):
        """state/status.json + state/equity.csv: lo que lee el panel."""
        now = time.time()
        positions = []
        for m, p in self.pf.positions.items():
            price = self.pf.last_prices.get(m, p.entry_price)
            value = p.qty * price
            cost_left = p.cost_usd * p.qty / p.initial_qty
            positions.append({
                "symbol": p.symbol, "mint": m, "pool": p.pool, "entry_price": p.entry_price, "price": price,
                "qty": p.qty, "cost": p.cost_usd, "value": value, "realized": p.realized_usd,
                "pnl_usd": value + p.realized_usd - p.cost_usd,
                "pnl_pct": 100 * (value + p.realized_usd - p.cost_usd) / p.cost_usd if p.cost_usd else 0,
                "unrealized_pct": 100 * (value / cost_left - 1) if cost_left else 0,
                "opened_at": p.opened_at, "tp_hit": len(p.tp_levels_hit), "peak": p.peak_price,
                "stop": p.entry_price if p.tp_levels_hit else p.entry_price * (1 - self.exit_cfg().stop_loss_pct / 100),
                "liquidity": self.liquidity.get(m, p.entry_liquidity),
            })
        eq = self.pf.equity()
        status = {
            "ts": int(now), "started_at": int(self.started_at), "mode": self.cfg.mode,
            "strategy": self.strategy_label(), "paused": self.paused,
            "brain": {"enabled": self.cfg.brain.enabled, "active": self.brain_active(),
                      "status": self.brain.data.get("status"), "version": self.brain.version,
                      "blocks_entries": self.brain_blocks_entries()},
            "equity": eq, "cash": self.pf.cash, "start_equity": self.cfg.risk.starting_equity_usd,
            "daily_pnl": self.pf.daily_pnl(), "day_start_equity": self.pf.start_equity_today(),
            "max_open": self.cfg.risk.max_open_positions,
            "daily_loss_limit_pct": self.cfg.risk.daily_loss_limit_pct,
            "positions": positions, "last_scan": self.last_scan, "events": list(self.notify.events)[:30],
            "interval": self.cfg.scanner.interval_seconds,
            "risk_off": self.risk_off,
        }
        try:
            tmp = self.pf.dir / "status.tmp"
            tmp.write_text(json.dumps(status))
            tmp.replace(self.pf.dir / "status.json")
            if now - self._last_equity_log >= 60:
                self._last_equity_log = now
                f = self.pf.dir / "equity.csv"
                new = not f.exists()
                with f.open("a") as fh:
                    if new:
                        fh.write("ts,equity,cash\n")
                    fh.write(f"{int(now)},{eq:.4f},{self.pf.cash:.4f}\n")
        except OSError as e:
            log.warning("No se pudo escribir el estado: %s", e)

    def cycle(self, force_entries: bool = False):
        self.read_control()
        self.refresh_context()
        self.brain.refresh()
        if self.brain.version != self._brain_version_seen:
            if self._brain_version_seen or self.brain.version:
                self.notify.send(f"🧠 Cerebro: {self.brain.data.get('status_text', '')}")
            self._brain_version_seen = self.brain.version
        self.manage_positions()
        self.reclaim()
        if self.brain_blocks_entries():
            log.info("Cerebro en modo defensivo: no se abren posiciones")
        elif not self.paused and (force_entries or
                                time.time() - self._last_entry_scan >= self.cfg.scanner.entry_interval_seconds):
            self._last_entry_scan = time.time()
            self.find_entries()
            self.refresh_position_charts()
        self.daily_summary()
        self.write_status()
        log.info("Equity ${:.2f} | caja ${:.2f} | posiciones {} | PnL hoy ${:+.2f}".format(
            self.pf.equity(), self.pf.cash, len(self.pf.positions), self.pf.daily_pnl()))

    def run(self, once: bool = False):
        self.notify.send(f"Bot iniciado en modo {self.cfg.mode.upper()} — estrategia {self.cfg.strategy.name}")
        while True:
            t0 = time.time()
            try:
                self.cycle(force_entries=once)
            except Exception:  # noqa: BLE001
                log.exception("Error en ciclo")
            if once:
                return
            if version.changed():
                self.notify.send(f"🔄 Nueva versión instalada ({version.read()[:7]}): reiniciando el bot…")
                self.pf.save()
                return                      # Docker lo vuelve a arrancar con el código nuevo
            time.sleep(max(1.0, self.cfg.scanner.interval_seconds - (time.time() - t0)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    Bot(load_config(a.config)).run(once=a.once)


if __name__ == "__main__":
    main()
