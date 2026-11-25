"""Señales de entrada calculadas sobre velas de 1 minuto.

Este módulo lo usan TANTO el backtester como el bot en vivo, para que lo que se
prueba sea exactamente lo que se ejecuta. Las features en el índice i solo usan
velas 0..i (sin mirar al futuro). Todo está vectorizado con numpy: el backtest
calcula la serie completa de una vez y el bot en vivo toma el último valor.
"""
from __future__ import annotations

from dataclasses import dataclass

import math

import numpy as np


@dataclass
class Series:
    """Velas de 1 min continuas (los minutos sin operaciones se rellenan)."""
    ts: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray  # noqa: E741
    c: np.ndarray
    v: np.ndarray

    def __len__(self):
        return len(self.ts)


def clean_rows(rows: list[list[float]], drop_last: bool = True, spike: float = 4.0) -> list[list[float]]:
    """Quita la vela en curso (incompleta) y los picos aislados hacia ARRIBA.

    Los datos de pools casi sin liquidez traen velas con precios absurdos (x20-x60)
    que generarían beneficios ficticios. Los picos hacia abajo se dejan: solo
    pueden hacer el backtest más pesimista, nunca más optimista.
    """
    rows = sorted(rows, key=lambda r: r[0])
    if drop_last and rows:
        rows = rows[:-1]
    if len(rows) < 3:
        return rows
    c = np.array([r[4] for r in rows])
    out = []
    for i, r in enumerate(rows):
        lo, hi = max(0, i - 3), min(len(rows), i + 4)
        neigh = np.concatenate([c[lo:i], c[i + 1:hi]])
        med = float(np.median(neigh)) if len(neigh) else r[4]
        if med > 0 and r[4] > spike * med:
            continue
        # recorta mechas absurdas hacia arriba aunque el cierre sea razonable
        if med > 0 and r[2] > spike * med:
            r = [r[0], r[1], max(r[1], r[4]), r[3], r[4], r[5]]
        out.append(r)
    return out


def to_series(rows: list[list[float]], max_len: int = 20_000, clean: bool = True) -> Series | None:
    """[ts,o,h,l,c,vol_usd] con huecos -> serie continua de 1 min."""
    if clean:
        rows = clean_rows(rows)
    if len(rows) < 2:
        return None
    rows = sorted(rows, key=lambda r: r[0])
    t0, t1 = int(rows[0][0]), int(rows[-1][0])
    n = (t1 - t0) // 60 + 1
    if n > max_len:
        t0 = t1 - (max_len - 1) * 60
        rows = [r for r in rows if r[0] >= t0]
        n = max_len
    ts = t0 + 60 * np.arange(n)
    o = np.full(n, np.nan)
    h, l, c = o.copy(), o.copy(), o.copy()  # noqa: E741
    v = np.zeros(n)
    for r in rows:
        i = (int(r[0]) - t0) // 60
        o[i], h[i], l[i], c[i], v[i] = r[1], r[2], r[3], r[4], r[5]
    for i in range(1, n):
        if np.isnan(c[i]):
            o[i] = h[i] = l[i] = c[i] = c[i - 1]
    if np.isnan(c).any() or (c <= 0).any():
        return None
    return Series(ts, o, h, l, c, v)


def _rolling_sum(x: np.ndarray, w: int) -> np.ndarray:
    cs = np.concatenate([[0.0], np.cumsum(x)])
    idx = np.arange(len(x))
    return cs[idx + 1] - cs[np.maximum(0, idx + 1 - w)]


def _rolling_max(x: np.ndarray, w: int) -> np.ndarray:
    pad = np.concatenate([np.full(w - 1, -np.inf), x])
    return np.lib.stride_tricks.sliding_window_view(pad, w).max(axis=1)


def _ret(c: np.ndarray, k: int) -> np.ndarray:
    idx = np.maximum(0, np.arange(len(c)) - k)
    return (c / c[idx] - 1) * 100


def features(s: Series, created_at: int, supply: float) -> dict[str, np.ndarray]:
    c, v = s.c, s.v
    n = len(c)
    v60, v5 = _rolling_sum(v, 60), _rolling_sum(v, 5)
    max6h = _rolling_max(c, 361)
    # máximo de cierre entre [i-180, i-60): resistencia previa, sin la última hora
    m120 = _rolling_max(c, 120)
    prev3h = np.full(n, np.inf)
    prev3h[60:] = m120[:-60]
    return {
        "valid": np.arange(n) >= 60,
        "age_min": (s.ts - created_at) / 60,
        "price": c,
        "vol_5m": v5,
        "vol_60m": v60,
        "ret_5m": _ret(c, 5),
        "ret_60m": _ret(c, 60),
        "ret_6h": _ret(c, 360),
        "drawdown_6h": (c / max6h - 1) * 100,
        "high_prev_3h": prev3h,
        "active_60m": _rolling_sum((v > 0).astype(float), 60),
        "accel": np.divide(v5 * 12, v60, out=np.zeros(n), where=v60 > 0),
        "fdv": c * supply,
        # ¿está comprando en pleno estirón? (evitar entrar justo en el pico)
        "ext_vwap15": np.divide(c * _rolling_sum(v, 15), _rolling_sum(c * v, 15), out=np.ones(n),
                                where=_rolling_sum(c * v, 15) > 0) * 100 - 100,
        "wick_1m": np.divide(s.h - np.maximum(s.o, c), c, out=np.zeros(n), where=c > 0) * 100,
        "range_15m": (_rolling_max(s.h, 15) / np.maximum(-_rolling_max(-s.l, 15), 1e-18) - 1) * 100,
    }


def _rng(x, lo, hi):
    return (x >= lo) & (x <= hi)


# --------------------------------------------------------------------------- estrategias
# f(features, params) -> array booleano de "señal de entrada en el cierre de la vela i".

def momentum(f, p):
    """Segunda ola: token ya asentado, volumen acelerando, subida sana no parabólica."""
    return (f["valid"] & _rng(f["age_min"], p.get("min_age", 30), p.get("max_age", 4320))
            & (f["vol_60m"] >= p.get("min_vol_60m", 50_000))
            & _rng(f["ret_60m"], p.get("min_ret_60m", 5), p.get("max_ret_60m", 150))
            & (f["ret_5m"] >= p.get("min_ret_5m", -3))
            & (f["accel"] >= p.get("min_accel", 1.0))
            & (f["active_60m"] >= p.get("min_active", 40))
            & _rng(f["fdv"], p.get("min_fdv", 100_000), p.get("max_fdv", 50_000_000)))


def early(f, p):
    """Proxy de 'sniping tardío': entrar en los primeros minutos si arranca fuerte."""
    return (_rng(f["age_min"], p.get("min_age", 3), p.get("max_age", 30))
            & (f["vol_5m"] >= p.get("min_vol_5m", 15_000))
            & (f["ret_5m"] >= p.get("min_ret_5m", 10))
            & (f["fdv"] >= p.get("min_fdv", 30_000)))


def dip(f, p):
    """Reversión: caída fuerte desde máximos, volumen vivo y primer rebote."""
    return (f["valid"] & _rng(f["age_min"], p.get("min_age", 120), p.get("max_age", 4320))
            & (f["drawdown_6h"] <= -p.get("min_drawdown", 45))
            & (f["vol_60m"] >= p.get("min_vol_60m", 30_000))
            & (f["ret_5m"] >= p.get("min_bounce", 3))
            & (f["active_60m"] >= p.get("min_active", 40))
            & (f["fdv"] >= p.get("min_fdv", 200_000)))


def breakout(f, p):
    """Ruptura del máximo de las 3 h previas con volumen."""
    return (f["valid"] & (f["age_min"] >= p.get("min_age", 120))
            & (f["price"] > f["high_prev_3h"] * (1 + p.get("min_break", 0) / 100))
            & (f["vol_60m"] >= p.get("min_vol_60m", 50_000))
            & (f["accel"] >= p.get("min_accel", 1.5))
            & (f["ret_60m"] <= p.get("max_ret_60m", 80))
            & (f["active_60m"] >= p.get("min_active", 40))
            & (f["fdv"] >= p.get("min_fdv", 150_000)))


def baseline(f, p):
    """Referencia: cualquier momento con actividad mínima. Si una estrategia no bate
    esto, su 'ventaja' es ruido."""
    return (f["valid"] & (f["age_min"] >= 30) & (f["vol_60m"] >= p.get("min_vol_60m", 50_000))
            & (f["active_60m"] >= 40))


STRATEGIES = {"momentum": momentum, "early": early, "dip": dip, "breakout": breakout, "baseline": baseline}


def latest_signal(strategy: str, rows: list[list[float]], created_at: int, supply: float, params: dict) -> tuple[bool, dict]:
    """Para el bot en vivo: ¿hay señal en la última vela cerrada?"""
    s = to_series(rows)
    if s is None:
        return False, {}
    f = features(s, created_at, supply)
    sig = STRATEGIES[strategy](f, params)
    last = {k: float(v[-1]) for k, v in f.items()}
    return bool(sig[-1]), last


# --------------------------------------------------------------------------- explicación (para el panel)
def _fmt_usd(v: float) -> str:
    return f"{v / 1e6:.1f} M$" if v >= 1e6 else f"{v / 1e3:.0f} k$"


def explain(strategy: str, f: dict, p: dict) -> list[dict]:
    """Condiciones de la estrategia en la última vela: [{label, ok, value, rule}] para mostrar en el panel.

    `f` son las features de la última vela (valores escalares, como devuelve latest_signal).
    Refleja exactamente las condiciones de las funciones de estrategia de arriba.
    """
    g = lambda k, d=0.0: float(f.get(k, d))  # noqa: E731
    rows: list[tuple[str, bool, str, str]] = []

    def rng(label, k, lo, hi, fmt=lambda v: f"{v:.0f}"):
        v = g(k)
        rows.append((label, lo <= v <= hi, fmt(v), f"entre {fmt(lo)} y {fmt(hi)}"))

    def ge(label, k, lo, fmt=lambda v: f"{v:.0f}"):
        v = g(k)
        rows.append((label, v >= lo, fmt(v), f"≥ {fmt(lo)}"))

    pct = lambda v: f"{v:+.1f}%"  # noqa: E731
    if strategy == "momentum":
        rng("Edad del token (min)", "age_min", p.get("min_age", 30), p.get("max_age", 4320))
        ge("Volumen última hora", "vol_60m", p.get("min_vol_60m", 50_000), _fmt_usd)
        rng("Subida última hora", "ret_60m", p.get("min_ret_60m", 5), p.get("max_ret_60m", 150), pct)
        ge("Variación últimos 5 min", "ret_5m", p.get("min_ret_5m", -3), pct)
        ge("Aceleración del volumen", "accel", p.get("min_accel", 1.0), lambda v: f"{v:.2f}x")
        ge("Minutos activos en 1 h", "active_60m", p.get("min_active", 40))
        rng("Capitalización (FDV)", "fdv", p.get("min_fdv", 100_000), p.get("max_fdv", 50_000_000), _fmt_usd)
    elif strategy == "early":
        rng("Edad del token (min)", "age_min", p.get("min_age", 3), p.get("max_age", 30))
        ge("Volumen últimos 5 min", "vol_5m", p.get("min_vol_5m", 15_000), _fmt_usd)
        ge("Subida últimos 5 min", "ret_5m", p.get("min_ret_5m", 10), pct)
        ge("Capitalización (FDV)", "fdv", p.get("min_fdv", 30_000), _fmt_usd)
    elif strategy == "dip":
        rng("Edad del token (min)", "age_min", p.get("min_age", 120), p.get("max_age", 4320))
        v = g("drawdown_6h")
        lim = -p.get("min_drawdown", 45)
        rows.append(("Caída desde el máximo de 6 h", v <= lim, pct(v), f"≤ {pct(lim)}"))
        ge("Volumen última hora", "vol_60m", p.get("min_vol_60m", 30_000), _fmt_usd)
        ge("Rebote últimos 5 min", "ret_5m", p.get("min_bounce", 3), pct)
        ge("Minutos activos en 1 h", "active_60m", p.get("min_active", 40))
        ge("Capitalización (FDV)", "fdv", p.get("min_fdv", 200_000), _fmt_usd)
    elif strategy == "breakout":
        ge("Edad del token (min)", "age_min", p.get("min_age", 120))
        hp = g("high_prev_3h", float("inf"))
        brk = (g("price") / hp - 1) * 100 if math.isfinite(hp) and hp > 0 else -100.0
        need = p.get("min_break", 0)
        rows.append(("Rompe el máximo de 3 h", brk > need, pct(brk), f"> {pct(need)}"))
        ge("Volumen última hora", "vol_60m", p.get("min_vol_60m", 50_000), _fmt_usd)
        ge("Aceleración del volumen", "accel", p.get("min_accel", 1.5), lambda v: f"{v:.2f}x")
        v = g("ret_60m")
        rows.append(("Subida última hora (no parabólica)", v <= p.get("max_ret_60m", 80), pct(v),
                     f"≤ {pct(p.get('max_ret_60m', 80))}"))
        ge("Minutos activos en 1 h", "active_60m", p.get("min_active", 40))
        ge("Capitalización (FDV)", "fdv", p.get("min_fdv", 150_000), _fmt_usd)
    else:  # baseline
        ge("Edad del token (min)", "age_min", 30)
        ge("Volumen última hora", "vol_60m", p.get("min_vol_60m", 50_000), _fmt_usd)
        ge("Minutos activos en 1 h", "active_60m", 40)
    return [{"label": a, "ok": bool(b), "value": c, "rule": d} for a, b, c, d in rows]
