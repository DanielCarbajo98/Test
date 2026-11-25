"""Cuánto invertir en cada operación: lo decide el bot en cada momento, dentro de tus límites.

Idea (la de los gestores profesionales): arriesgar una fracción fija del saldo si salta el stop, y ajustarla por
  · la VENTAJA que tiene esa estrategia (lo que gana de media en datos no vistos, o lo que predice el modelo),
  · el MOMENTO del mercado (régimen alcista o no; racha de pérdidas del propio bot),
  · el RIESGO QUE YA HAY abierto (no acumular demasiadas apuestas a la vez),
  · y los COSTES (no abrir operaciones tan pequeñas que la comisión se coma la ganancia).
Nunca supera el tope por posición de tu config ni la liquidez disponible.
"""
from __future__ import annotations

import os


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def stock_amount(equity: float, cash: float, price: float, stop: float, expected: dict | None,
                 market_up: bool | None, open_positions: list[dict], fixed_fee_eur: float = 0.0) -> tuple[float, list[str]]:
    """Euros a invertir en una compra de acciones y por qué."""
    risk_pct = float(os.getenv("STOCKS_RISK_PCT", "1.0"))           # % del saldo que se pierde si salta el stop
    max_pct = float(os.getenv("STOCKS_MAX_POSITION_PCT", "10"))     # tope por posición
    heat_pct = float(os.getenv("STOCKS_MAX_HEAT_PCT", "8"))         # riesgo total abierto en acciones
    why = []
    dist = max(0.01, (price - stop) / price) if price > 0 else 0.05
    amount = equity * risk_pct / 100 / dist
    why.append(f"riesgo {risk_pct:g} % del saldo con el stop a −{100 * dist:.1f} % → {amount:,.0f} €")
    exp = (expected or {}).get("expectancy")
    if exp is not None:
        m = _clip(0.5 + exp / 2, 0.5, 1.5)
        amount *= m
        why.append(f"ventaja de la estrategia {exp:+.2f} % por operación → ×{m:.2f}")
    if market_up is False:
        amount *= 0.6
        why.append("mercado general bajista → ×0.6")
    open_risk = sum(p.get("amount", 0) * max(0.0, (p["entry"] - p["stop"]) / p["entry"]) for p in open_positions
                    if p.get("entry") and p.get("stop") is not None)
    room = equity * heat_pct / 100 - open_risk
    if amount * dist > room:
        amount = max(0.0, room / dist)
        why.append(f"ya hay {open_risk:,.0f} € en riesgo abierto (máx. {heat_pct:g} % del saldo) → {amount:,.0f} €")
    cap = equity * max_pct / 100
    if amount > cap:
        amount = cap
        why.append(f"tope por posición {max_pct:g} % del saldo → {cap:,.0f} €")
    if amount > cash:
        amount = max(0.0, cash)
        why.append(f"liquidez disponible → {amount:,.0f} €")
    minimum = max(100.0, fixed_fee_eur * 100)                       # que la comisión no pase del 1 % por lado
    if amount < minimum:
        why.append(f"menos de {minimum:,.0f} €: no compensa con las comisiones, no se compra")
        return 0.0, why
    return float(int(amount // 10 * 10)), why


def crypto_multiplier(pred: float | None, threshold: float | None, equity: float, start: float,
                      max_drawdown_pct: float = 0.0, daily_pnl: float = 0.0, day_start: float = 0.0) -> tuple[float, list[str]]:
    """Factor sobre el tamaño base (riesgo fijo) de cripto: confianza del modelo y racha del bot."""
    m, why = 1.0, []
    if pred is not None and threshold and threshold > 0:
        c = _clip(0.75 + 0.25 * pred / threshold, 0.5, 1.5)
        m *= c
        why.append(f"el modelo espera {100 * pred:+.1f} % (mínimo {100 * threshold:+.1f} %) → ×{c:.2f}")
    if start > 0 and equity < start:
        dd = 1 - equity / start
        ref = (max_drawdown_pct or 40) / 100
        c = 1 - 0.5 * _clip(dd / ref, 0, 1)
        if c < 0.999:
            m *= c
            why.append(f"el saldo está un {100 * dd:.1f} % por debajo del inicio → ×{c:.2f} (más prudente)")
    if day_start > 0 and daily_pnl < 0:                     # mal día: más pequeño, sin cortar del todo
        loss = -daily_pnl / day_start
        c = 1 - 0.75 * _clip(loss / 0.10, 0, 1)             # −10 % en el día → un 25 % del tamaño normal
        if c < 0.999:
            m *= c
            why.append(f"hoy va {100 * daily_pnl / day_start:+.1f} % → ×{c:.2f} (reduce tras pérdidas)")
    return m, why
