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
from datetime import datetime, timezone

import httpx

from .config import Config, load_config
from .executor import JupiterExecutor, PaperExecutor, Quoter
from .market import DEFAULT_SOURCES, Candles, discover
from .models import Position
from .portfolio import Portfolio
from .risk import can_open, evaluate_exit, position_size_usd
from .safety import SafetyChecker, SolanaRPC
from .scanner import DexScreener
from .signals import latest_signal
from .strategy import prefilter, rank

log = logging.getLogger("memebot")


class Notifier:
    def __init__(self, token: str, chat_id: str):
        self.token, self.chat_id = token, chat_id

    def send(self, text: str):
        log.info(text)
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
            d = evaluate_exit(pos, s.price_usd, s.liquidity_usd, now, self.cfg.exits)
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
        self.pf.save()

    # ------------------------------------------------------------------ entradas
    def find_entries(self):
        c = self.cfg
        ok, why = can_open(len(self.pf.positions), self.pf.daily_pnl(), self.pf.start_equity_today(), c.risk)
        if not ok:
            log.info("No se abren posiciones: %s", why)
            return
        addrs, boosted = self.dex.latest_token_addresses(c.scanner.include_boosted, c.scanner.include_profiles)
        if c.scanner.include_gecko:
            addrs += [m for m in discover(self.candles, DEFAULT_SOURCES) if m not in addrs]
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
        passed.sort(key=rank, reverse=True)
        log.info("Candidatos: %d descubiertos, %d con datos, %d pasan el prefiltro", len(addrs), len(snaps), len(passed))

        for s in passed[: c.strategy.max_ohlcv_per_cycle]:
            if len(self.pf.positions) >= c.risk.max_open_positions:
                break
            rows = self.candles.minute(s.pair_address)
            supply = s.fdv / s.price_usd
            created = int(time.time() - s.age_minutes * 60)
            signal, feat = latest_signal(c.strategy.name, rows, created, supply, c.strategy.params)
            self._log_decision(s, signal, feat)
            if not signal:
                log.debug("%s sin señal %s (ret60 %.1f%%, accel %.2f)", s.symbol, c.strategy.name,
                          feat.get("ret_60m", 0), feat.get("accel", 0))
                continue
            rep = self.safety.check(s)
            if not rep.ok:
                log.info("%s con señal pero NO seguro: %s", s.symbol, "; ".join(rep.reasons))
                self.pf.cooldowns[s.token_address] = time.time()
                continue
            size = position_size_usd(self.pf.equity(), self.pf.cash, s.liquidity_usd, c.exits.stop_loss_pct, c.risk)
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
                           peak_price=fill.price)
            self.pf.open(pos, f"{c.strategy.name}", fill.tx)
            self.notify.send(
                f"🟢 BUY {s.symbol} ${fill.usd:.2f} @ {fill.price:.6g} [{c.mode}] | {c.strategy.name} | "
                f"liq ${s.liquidity_usd:,.0f} | 1h {feat.get('ret_60m', 0):+.0f}% | top10 {rep.top10_pct}% | "
                f"https://dexscreener.com/solana/{s.pair_address}")
        self.pf.save()

    def _log_decision(self, s, signal: bool, feat: dict):
        """Registro de cada candidato evaluado (para validar filtros en tiempo real a posteriori)."""
        try:
            with open(self.pf.dir / "decisions.jsonl", "a") as fh:
                fh.write(json.dumps({"ts": int(time.time()), "mint": s.token_address, "pool": s.pair_address,
                                     "symbol": s.symbol, "signal": signal, "liq": s.liquidity_usd, "fdv": s.fdv,
                                     "buys_m5": s.buys_m5, "sells_m5": s.sells_m5, "buys_h1": s.buys_h1,
                                     "sells_h1": s.sells_h1, "price": s.price_usd,
                                     **{k: round(v, 4) for k, v in feat.items() if k != "valid"}}) + "\n")
        except OSError:
            pass

    def daily_summary(self):
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if day != self._last_summary_day and datetime.now(timezone.utc).hour == 0:
            self._last_summary_day = day
            self.notify.send(f"📊 Resumen: equity ${self.pf.equity():.2f}, caja ${self.pf.cash:.2f}, "
                             f"{len(self.pf.positions)} posiciones abiertas")

    def cycle(self, force_entries: bool = False):
        self.manage_positions()
        if force_entries or time.time() - self._last_entry_scan >= self.cfg.scanner.entry_interval_seconds:
            self._last_entry_scan = time.time()
            self.find_entries()
        self.daily_summary()
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
