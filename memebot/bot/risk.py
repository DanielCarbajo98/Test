"""Gestión de riesgo: tamaño de posición, límites globales y reglas de salida."""
from __future__ import annotations

from dataclasses import dataclass

from .config import ExitCfg, RiskCfg
from .models import Position


def position_size_usd(equity: float, cash: float, liquidity_usd: float, stop_loss_pct: float, r: RiskCfg) -> float:
    """Tamaño por riesgo fijo: si salta el stop, pierdes ~risk_per_trade_pct del capital.

    OJO: en memecoins el precio puede saltar por debajo del stop (gaps, rugs),
    así que la pérdida real puede ser mayor. Por eso hay topes adicionales.
    """
    if stop_loss_pct <= 0:
        return 0.0
    by_risk = equity * (r.risk_per_trade_pct / 100) / (stop_loss_pct / 100)
    by_cap = equity * r.max_position_pct / 100
    by_pool = liquidity_usd * r.max_pool_share_pct / 100
    return round(max(0.0, min(by_risk, by_cap, by_pool, cash)), 2)


def can_open(open_positions: int, daily_pnl_usd: float, day_start_equity: float, r: RiskCfg,
             equity: float | None = None) -> tuple[bool, str]:
    if r.max_drawdown_pct and equity is not None and equity < r.starting_equity_usd * (1 - r.max_drawdown_pct / 100):
        return False, f"freno total: saldo {equity:.2f} por debajo del {100 - r.max_drawdown_pct:.0f}% del inicial"
    if r.max_open_positions and open_positions >= r.max_open_positions:      # 0 = sin límite
        return False, "máximo de posiciones abiertas"
    if r.daily_loss_limit_pct and day_start_equity > 0 and -daily_pnl_usd >= day_start_equity * r.daily_loss_limit_pct / 100:
        return False, "límite de pérdida diaria alcanzado (kill-switch)"
    return True, ""


@dataclass
class ExitDecision:
    sell_qty: float
    reason: str
    new_tp_levels: list[int]


def evaluate_exit(pos: Position, price: float, liquidity_usd: float, now: float, e: ExitCfg) -> ExitDecision | None:
    """Decide si vender (total o parcial). Actualiza pos.peak_price."""
    if price <= 0 or pos.qty <= 0:
        return None
    pos.peak_price = max(pos.peak_price, price)
    gain_pct = (price / pos.entry_price - 1) * 100
    all_out = lambda reason: ExitDecision(pos.qty, reason, pos.tp_levels_hit)  # noqa: E731

    # 1) La liquidez se desploma: posible rug / retirada de LP. Salir ya.
    if pos.entry_liquidity > 0 and liquidity_usd < pos.entry_liquidity * (1 - e.liquidity_drop_exit_pct / 100):
        return all_out(f"liquidez -{e.liquidity_drop_exit_pct:.0f}% (posible rug)")

    # 2) Stop loss. Tras el primer take-profit el stop sube a break-even.
    stop = pos.entry_price if pos.tp_levels_hit else pos.entry_price * (1 - e.stop_loss_pct / 100)
    peak_gain = (pos.peak_price / pos.entry_price - 1) * 100
    lock = None
    if peak_gain >= e.lock_activation_pct:           # candado de beneficio: no devolver lo ganado
        lock = pos.entry_price * (1 + e.lock_keep_pct / 100 * peak_gain / 100)
        stop = max(stop, lock)
    if price <= stop:
        if lock is not None and stop == lock:
            return all_out(f"candado de beneficio ({gain_pct:+.1f}%, máximo {peak_gain:+.0f}%)")
        return all_out("break-even stop" if pos.tp_levels_hit else f"stop loss {gain_pct:.1f}%")

    # 3) Trailing stop una vez activado.
    if pos.peak_price >= pos.entry_price * (1 + e.trailing_activation_pct / 100):
        if price <= pos.peak_price * (1 - e.trailing_stop_pct / 100):
            return all_out(f"trailing stop (pico {pos.peak_price:.3g})")

    # 4) Time stop: si no se mueve, el capital está mejor en otra parte.
    minutes = (now - pos.opened_at) / 60
    # solo si NUNCA llegó a +time_stop_min_gain_pct: si subió y luego bajó, lo gestiona el candado
    if not pos.tp_levels_hit and minutes >= e.time_stop_minutes and peak_gain < e.time_stop_min_gain_pct:
        return all_out(f"time stop ({minutes:.0f} min, {gain_pct:+.1f}%)")

    # 5) Escalera de take-profits (porcentaje sobre la cantidad inicial).
    qty, hit = 0.0, list(pos.tp_levels_hit)
    for i, (gain, pct) in enumerate(e.take_profits):
        if i not in hit and gain_pct >= gain:
            qty += pos.initial_qty * pct / 100
            hit.append(i)
    if qty > 0:
        return ExitDecision(min(qty, pos.qty), f"take profit {gain_pct:+.0f}%", hit)
    return None
