"""Bucle principal: escanear -> filtrar -> verificar seguridad -> dimensionar -> comprar; gestionar salidas.

Uso:
  python -m bot.main --config config.yaml          # bucle continuo
  python -m bot.main --config config.yaml --once   # un solo ciclo (útil para depurar)
"""
from __future__ import annotations

import argparse
import logging
import time

import httpx

from .config import Config, load_config
from .executor import JupiterExecutor, PaperExecutor
from .models import Position
from .portfolio import Portfolio
from .risk import can_open, evaluate_exit, position_size_usd
from .safety import SafetyChecker, SolanaRPC
from .scanner import DexScreener
from .strategy import evaluate

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
                       json={"chat_id": self.chat_id, "text": text}, timeout=10)
        except httpx.HTTPError as e:
            log.warning("Telegram falló: %s", e)


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.dex = DexScreener()
        if cfg.mode == "live":
            self.exe = JupiterExecutor(cfg.jupiter_api_key, cfg.wallet_private_key, cfg.quote_mint,
                                       cfg.quote_decimals, cfg.risk.slippage_bps)
        else:
            self.exe = PaperExecutor()
        self.safety = SafetyChecker(cfg.safety, SolanaRPC(cfg.rpc_url),
                                    sell_probe=self.exe.can_sell if cfg.mode == "live" else None)
        self.pf = Portfolio(cfg.state_dir, cfg.risk.starting_equity_usd)
        self.notify = Notifier(cfg.telegram_token, cfg.telegram_chat_id)

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
                self.notify.send(f"⚠️ Venta fallida {pos.symbol}: {e}")
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
        addrs = [a for a in addrs if a not in self.pf.positions
                 and not self.pf.in_cooldown(a, c.risk.cooldown_minutes)][: c.scanner.max_candidates]
        snaps = self.dex.best_pairs(addrs, boosted)

        ranked = []
        for s in snaps.values():
            sig = evaluate(s, c.filters)
            if sig.passed:
                ranked.append((sig.score, s))
            else:
                log.debug("%s descartado: %s", s.symbol, "; ".join(sig.reasons))
        ranked.sort(key=lambda x: x[0], reverse=True)

        for score, s in ranked:
            if len(self.pf.positions) >= c.risk.max_open_positions:
                break
            rep = self.safety.check(s)
            if not rep.ok:
                log.info("%s no seguro: %s", s.symbol, "; ".join(rep.reasons))
                self.pf.cooldowns[s.token_address] = time.time()  # no re-verificar en cada ciclo
                continue
            size = position_size_usd(self.pf.equity(), self.pf.cash, s.liquidity_usd, c.exits.stop_loss_pct, c.risk)
            if size < 5:
                log.info("Tamaño insuficiente para %s (%.2f USD)", s.symbol, size)
                continue
            try:
                fill = self.exe.buy(s.token_address, rep.decimals or 6, size, s.price_usd, s.liquidity_usd)
            except Exception as e:  # noqa: BLE001
                self.notify.send(f"⚠️ Compra fallida {s.symbol}: {e}")
                continue
            pos = Position(token_address=s.token_address, symbol=s.symbol, decimals=rep.decimals or 6,
                           entry_price=fill.price, qty=fill.qty, initial_qty=fill.qty, cost_usd=fill.usd,
                           realized_usd=0.0, entry_liquidity=s.liquidity_usd, opened_at=time.time(),
                           peak_price=fill.price)
            self.pf.open(pos, f"score {score}", fill.tx)
            self.notify.send(f"🟢 BUY {s.symbol} ${fill.usd:.2f} @ {fill.price:.6g} | score {score} | "
                             f"liq ${s.liquidity_usd:,.0f} | top10 {rep.top10_pct}% | https://dexscreener.com/solana/{s.pair_address}")
        self.pf.save()

    def cycle(self):
        self.manage_positions()   # primero proteger lo que ya tienes
        self.find_entries()
        log.info("Equity ${:.2f} | caja ${:.2f} | posiciones {} | PnL hoy ${:+.2f}".format(
            self.pf.equity(), self.pf.cash, len(self.pf.positions), self.pf.daily_pnl()))

    def run(self, once: bool = False):
        self.notify.send(f"Bot iniciado en modo {self.cfg.mode.upper()}")
        while True:
            try:
                self.cycle()
            except Exception:  # noqa: BLE001
                log.exception("Error en ciclo")
            if once:
                return
            time.sleep(self.cfg.scanner.interval_seconds)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Bot(load_config(a.config)).run(once=a.once)


if __name__ == "__main__":
    main()
