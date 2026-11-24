"""Estrategia de entrada: momentum filtrado ("segunda ola").

No compra en el bloque de lanzamiento (eso es terreno de snipers con Jito y
insiders). Espera a que el token sobreviva su primera fase, tenga liquidez real
y muestre presión compradora sostenida, sin perseguir velas parabólicas.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import FilterCfg
from .models import PairSnapshot


@dataclass
class Signal:
    passed: bool
    score: float
    reasons: list[str]


def _ratio(buys: int, sells: int) -> float:
    total = buys + sells
    return buys / total if total else 0.0


def hard_filters(s: PairSnapshot, f: FilterCfg) -> list[str]:
    """Devuelve la lista de motivos de rechazo (vacía = pasa)."""
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
    if _ratio(s.buys_m5, s.sells_m5) < f.min_buy_ratio_m5:
        r.append("presión compradora 5m insuficiente")
    if _ratio(s.buys_h1, s.sells_h1) < f.min_buy_ratio_h1:
        r.append("presión compradora 1h insuficiente")
    if not (f.min_change_h1 <= s.change_h1 <= f.max_change_h1):
        r.append(f"cambio 1h {s.change_h1:.1f}% fuera de rango")
    if s.change_m5 < f.min_change_m5:
        r.append(f"cayendo en 5m ({s.change_m5:.1f}%)")
    if s.fdv and s.fdv > f.max_fdv_usd:
        r.append("FDV demasiado alta")
    if s.price_usd <= 0:
        r.append("sin precio")
    return r


def score(s: PairSnapshot) -> float:
    """Puntuación 0-100 combinando señales de momentum y calidad."""
    pts = 0.0
    # 1) Rotación: volumen 1h / liquidez (actividad real frente a tamaño de pool). 0-25
    turnover = s.volume_h1 / s.liquidity_usd if s.liquidity_usd else 0
    pts += min(turnover / 2.0, 1.0) * 25
    # 2) Aceleración: el volumen de 5m anualizado a 1h supera la media de la última hora. 0-20
    accel = (s.volume_m5 * 12) / s.volume_h1 if s.volume_h1 else 0
    pts += min(max(accel - 0.8, 0) / 1.2, 1.0) * 20
    # 3) Presión compradora 5m. 0-20
    pts += min(max(_ratio(s.buys_m5, s.sells_m5) - 0.5, 0) / 0.2, 1.0) * 20
    # 4) Tendencia sana: 1h positivo y 6h no destruido. 0-15
    trend = 0.0
    if s.change_h1 > 0:
        trend += 0.6
    if s.change_h6 > -20:
        trend += 0.4
    pts += trend * 15
    # 5) Liquidez/FDV: pools profundas respecto a la valoración rug-pullean menos. 0-10
    liq_ratio = s.liquidity_usd / s.fdv if s.fdv else 0
    pts += min(liq_ratio / 0.15, 1.0) * 10
    # 6) Participación: nº de transacciones 1h. 0-10
    pts += min((s.buys_h1 + s.sells_h1) / 2000, 1.0) * 10
    # Penalización por boost pagado: atención comprada ≠ demanda orgánica
    if s.boosted:
        pts -= 5
    return round(max(0.0, min(100.0, pts)), 1)


def evaluate(s: PairSnapshot, f: FilterCfg) -> Signal:
    reasons = hard_filters(s, f)
    sc = score(s)
    if not reasons and sc < f.min_score:
        reasons.append(f"score {sc} < {f.min_score}")
    return Signal(passed=not reasons, score=sc, reasons=reasons)
