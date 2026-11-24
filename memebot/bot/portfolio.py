"""Estado persistente: caja, posiciones, cooldowns, PnL diario y registro de operaciones.

En modo live la "caja" es el presupuesto asignado al bot (starting_equity_usd),
no el saldo total del wallet: el bot nunca usa más de lo que le asignas.
"""
from __future__ import annotations

import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from .models import Position


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class Portfolio:
    def __init__(self, state_dir: str, starting_cash: float):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.file = self.dir / "portfolio.json"
        self.trades_file = self.dir / "trades.csv"
        self.cash = starting_cash
        self.positions: dict[str, Position] = {}
        self.cooldowns: dict[str, float] = {}
        self.daily: dict[str, float] = {}         # fecha -> PnL realizado
        self.day_start_equity: dict[str, float] = {}
        self.last_prices: dict[str, float] = {}
        self._load()

    # --- persistencia -------------------------------------------------------
    def _load(self):
        if not self.file.exists():
            return
        d = json.loads(self.file.read_text())
        self.cash = d["cash"]
        self.positions = {k: Position.from_dict(v) for k, v in d["positions"].items()}
        self.cooldowns = d.get("cooldowns", {})
        self.daily = d.get("daily", {})
        self.day_start_equity = d.get("day_start_equity", {})
        self.last_prices = d.get("last_prices", {})

    def save(self):
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "cash": self.cash,
            "positions": {k: v.to_dict() for k, v in self.positions.items()},
            "cooldowns": self.cooldowns,
            "daily": self.daily,
            "day_start_equity": self.day_start_equity,
            "last_prices": self.last_prices,
        }, indent=2))
        tmp.replace(self.file)

    def log_trade(self, side: str, pos: Position, qty: float, usd: float, price: float, reason: str, tx: str):
        new = not self.trades_file.exists()
        with self.trades_file.open("a", newline="") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["ts", "side", "symbol", "mint", "qty", "usd", "price", "reason", "tx"])
            w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"), side, pos.symbol,
                        pos.token_address, f"{qty:.6f}", f"{usd:.2f}", f"{price:.10g}", reason, tx])

    # --- métricas -----------------------------------------------------------
    def equity(self) -> float:
        return self.cash + sum(p.qty * self.last_prices.get(m, p.entry_price) for m, p in self.positions.items())

    def daily_pnl(self) -> float:
        return self.daily.get(_today(), 0.0)

    def start_equity_today(self) -> float:
        d = _today()
        if d not in self.day_start_equity:
            self.day_start_equity[d] = self.equity()
        return self.day_start_equity[d]

    def in_cooldown(self, mint: str, minutes: int) -> bool:
        return time.time() - self.cooldowns.get(mint, 0) < minutes * 60

    # --- operaciones --------------------------------------------------------
    def open(self, pos: Position, reason: str, tx: str):
        self.cash -= pos.cost_usd
        self.positions[pos.token_address] = pos
        self.cooldowns[pos.token_address] = time.time()
        self.last_prices[pos.token_address] = pos.entry_price
        self.log_trade("BUY", pos, pos.qty, pos.cost_usd, pos.entry_price, reason, tx)
        self.save()

    def reduce(self, mint: str, qty: float, usd: float, price: float, reason: str, tx: str) -> float:
        """Registra una venta (parcial o total). Devuelve el PnL realizado de esa venta."""
        pos = self.positions[mint]
        qty = min(qty, pos.qty)
        cost_share = pos.cost_usd * (qty / pos.initial_qty)
        pnl = usd - cost_share
        pos.qty -= qty
        pos.realized_usd += usd
        self.cash += usd
        d = _today()
        self.daily[d] = self.daily.get(d, 0.0) + pnl
        self.log_trade("SELL", pos, qty, usd, price, reason, tx)
        if pos.qty <= pos.initial_qty * 1e-6:
            del self.positions[mint]
        self.save()
        return pnl
