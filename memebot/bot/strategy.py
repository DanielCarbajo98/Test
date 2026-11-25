"""Prefiltro en tiempo real y ranking de candidatos (datos de DexScreener).

La decisión de entrada la toma bot/signals.py sobre velas de 1 minuto: el mismo
código que se valida en el backtest. Aquí solo se descarta lo que no es operable
AHORA (liquidez real, DEX, edad, FDV) y se ordena qué candidatos analizar primero,
porque descargar velas tiene un límite de peticiones.
"""
from __future__ import annotations

from .config import FilterCfg
from .models import PairSnapshot


def _ratio(buys: int, sells: int) -> float:
    total = buys + sells
    return buys / total if total else 0.0


def prefilter(s: PairSnapshot, f: FilterCfg) -> list[str]:
    """Motivos de rechazo (vacío = pasa)."""
    r = []
    if f.allowed_dexes and s.dex_id not in f.allowed_dexes:
        r.append(f"dex {s.dex_id} no permitido")
    if not (f.min_liquidity_usd <= s.liquidity_usd <= f.max_liquidity_usd):
        r.append(f"liquidez {s.liquidity_usd:,.0f} fuera de rango")
    if not (f.min_age_minutes <= s.age_minutes <= f.max_age_minutes):
        r.append(f"edad {s.age_minutes:.0f} min fuera de rango")
    if s.volume_h1 < f.min_volume_h1_usd:
        r.append(f"volumen 1h {s.volume_h1:,.0f} bajo")
    if s.buys_h1 + s.sells_h1 < f.min_txns_h1:
        r.append("pocas transacciones 1h")
    if f.min_buy_ratio_m5 and _ratio(s.buys_m5, s.sells_m5) < f.min_buy_ratio_m5:
        r.append("presión compradora 5m insuficiente")
    if s.fdv and s.fdv > f.max_fdv_usd:
        r.append("FDV demasiado alta")
    if 0.97 <= s.price_usd <= 1.03 and abs(s.change_h24) < 2 and abs(s.change_h1) < 1:
        r.append("parece una stablecoin (no se mueve)")
    if s.price_usd <= 0 or s.fdv <= 0:
        r.append("sin precio/FDV")
    return r


def rank(s: PairSnapshot) -> float:
    """Prioridad para descargar velas: actividad reciente respecto al tamaño de la pool."""
    turnover = s.volume_h1 / s.liquidity_usd if s.liquidity_usd else 0
    accel = (s.volume_m5 * 12) / s.volume_h1 if s.volume_h1 else 0
    return turnover + accel + _ratio(s.buys_m5, s.sells_m5)
