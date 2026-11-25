"""Costes reales de cada broker para acciones (lo usan el cerebro de acciones y la cartera simulada).

Elige el tuyo en .env con STOCKS_BROKER = trade_republic | xtb | degiro | ibkr (por defecto: xtb).
Tarifas aproximadas revisadas en noviembre de 2026; compruébalas siempre en la web oficial del broker.

Además de la comisión se cuentan:
  · diferencial de compra/venta y deslizamiento (0,10 % por lado)
  · cambio de divisa en acciones que no cotizan en euros (EE. UU., Reino Unido, Suiza...)
  · impuesto sobre transacciones financieras al COMPRAR grandes empresas de España (0,2 %),
    Francia (0,4 %) e Italia (0,1 %). Lo cobran TODOS los brokers.
"""
from __future__ import annotations

import os

SPREAD = 0.001
EUR_SUFFIXES = {"MC", "DE", "F", "PA", "AS", "MI", "BR", "HE", "LS", "VI", "IR"}
FTT_BUY = {"MC": 0.002, "PA": 0.004, "MI": 0.001}

# fee: lambda importe_eur, región -> € por orden ; fx: % por lado en acciones fuera del euro
BROKERS = {
    "trade_republic": {"name": "Trade Republic", "fx": 0.0,
                       "fee": lambda v, reg: 1.0,
                       "note": "1 € por orden; las acciones de EE. UU. se compran en euros en bolsas alemanas"},
    "xtb": {"name": "XTB", "fx": 0.005,
            "fee": lambda v, reg: 0.0,
            "note": "0 € hasta 100.000 €/mes; 0,5 % de cambio de divisa fuera del euro; 10 €/mes tras 1 año sin operar"},
    "degiro": {"name": "DEGIRO", "fx": 0.0025,
               "fee": lambda v, reg: 2.0,
               "note": "1 € + 1 € de tramitación por orden; 0,25 % de cambio de divisa; 2,50 €/año por bolsa extranjera"},
    "ibkr": {"name": "Interactive Brokers", "fx": 0.0003,
             "fee": lambda v, reg: (max(0.35, min(0.01 * v, 0.0035 * max(1.0, v / 50))) if reg == "US"
                                    else max(1.25, min(0.0005 * v, 29.0)) + 0.5),   # + tasas de bolsa aprox.
             "note": "EE. UU.: ~0,35 $ mínimo por orden; Europa: 0,05 % (mín. 1,25 €); cambio de divisa ~0,03 % "
                     "(cambia los euros a dólares de una vez, no en cada operación: cada cambio tiene un mínimo de 2 $)"},
}
DEFAULT_BROKER = "xtb"


def broker_key(key: str | None = None) -> str:
    k = (key or os.getenv("STOCKS_BROKER", DEFAULT_BROKER)).strip().lower().replace(" ", "_")
    return k if k in BROKERS else DEFAULT_BROKER


def suffix(sym: str) -> str:
    for sep in (".", "_"):
        if sep in sym:
            return sym.rsplit(sep, 1)[1].upper()
    return ""


def region(sym: str) -> str:
    s = suffix(sym)
    if not s:
        return "US"
    return "EU" if s in EUR_SUFFIXES else "OTRA"


REGION_NAMES = {"EU": "Europa (euros)", "US": "EE. UU.", "OTRA": "otras bolsas (otra divisa)"}


def costs(sym: str, amount_eur: float, broker: str | None = None) -> tuple[float, float]:
    """(coste de compra, coste de venta) como fracción del importe."""
    b = BROKERS[broker_key(broker)]
    reg = region(sym)
    fee = b["fee"](amount_eur, reg) / amount_eur if amount_eur > 0 else 0.0
    fx = b["fx"] if reg != "EU" else 0.0
    side = SPREAD + fee + fx
    return side + FTT_BUY.get(suffix(sym), 0.0), side


def fees_eur(sym: str, amount_eur: float, broker: str | None = None) -> float:
    """€ que se van en comisiones, divisa e impuestos en una compra + venta (sin contar el diferencial)."""
    buy, sell = costs(sym, amount_eur, broker)
    return amount_eur * (buy + sell - 2 * SPREAD)
