"""Saldo conjunto (solo en modo simulado): cripto y acciones comparten los mismos 10.000 $ de la demo.

Cada parte sigue llevando sus cuentas en su archivo (cripto: portfolio/status.json; acciones: stocks.json), y
cada una LEE la de la otra (nunca la escribe), así no hay choques entre procesos:
  liquidez conjunta = liquidez de cripto − dinero metido en acciones abiertas + ganado/perdido en acciones cerradas
  saldo conjunto    = saldo de cripto + resultado de acciones (cerradas y abiertas, con comisiones)
Las acciones van en euros y se convierten a dólares con el cambio que guarda el servicio de acciones.
Con dinero REAL (mode: live) no se aplica: la cripto usa siempre tu cartera real.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_USD_PER_EUR = 1.08


def enabled() -> bool:
    return os.getenv("JOINT_BALANCE", "1") != "0"


def _read(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def stocks_side(st: dict) -> dict:
    """Lo que las acciones aportan/consumen, en euros: comprometido (abiertas), realizado y flotante."""
    from .stocks import paper_pnl, PAPER_EUR
    pos = (st.get("positions") or {}).values()
    committed = sum(p.get("amount", PAPER_EUR) for p in pos)
    realized = sum(c.get("pnl_eur", 0.0) or 0.0 for c in st.get("closed") or [] if c.get("ret") is not None)
    floating = 0.0
    for p in pos:
        r = 0.0 if p.get("pending_open") else (p.get("price", p["entry"]) / p["entry"] - 1) * 100
        floating += paper_pnl(r, p.get("amount", PAPER_EUR), fees_total=p.get("fees_eur"))
    return {"committed_eur": committed, "realized_eur": realized, "open_pnl_eur": floating,
            "usd_per_eur": float(st.get("usd_per_eur") or DEFAULT_USD_PER_EUR)}


def crypto_view(state: Path, crypto_cash: float, crypto_equity: float) -> dict:
    """Para el bot de cripto: liquidez y saldo conjuntos (en $) usando lo que publica el servicio de acciones."""
    s = stocks_side(_read(state / "stocks.json"))
    fx = s["usd_per_eur"]
    cash = crypto_cash - s["committed_eur"] * fx + s["realized_eur"] * fx
    equity = crypto_equity + (s["realized_eur"] + s["open_pnl_eur"]) * fx
    return {"cash": cash, "equity": equity, "stocks_invested_usd": s["committed_eur"] * fx,
            "stocks_pnl_usd": (s["realized_eur"] + s["open_pnl_eur"]) * fx, "crypto_equity": crypto_equity,
            "crypto_cash": crypto_cash, "usd_per_eur": fx}


def stocks_cash_eur(state: Path, st: dict) -> float | None:
    """Para el servicio de acciones: liquidez conjunta en euros (None si cripto aún no ha publicado su estado)."""
    status = _read(state / "status.json")
    if "cash" not in status or status.get("mode") == "live":
        return None
    s = stocks_side(st)
    return status["cash"] / s["usd_per_eur"] - s["committed_eur"] + s["realized_eur"]


def stocks_equity_eur(state: Path, st: dict) -> float | None:
    """Saldo conjunto en euros, visto desde el servicio de acciones (None si cripto aún no ha publicado su estado)."""
    status = _read(state / "status.json")
    if "equity" not in status or status.get("mode") == "live":
        return None
    s = stocks_side(st)
    return status["equity"] / s["usd_per_eur"] + s["realized_eur"] + s["open_pnl_eur"]
