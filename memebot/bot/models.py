"""Estructuras de datos compartidas por todos los módulos."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class PairSnapshot:
    """Foto de un par en DexScreener en un instante dado."""
    token_address: str
    symbol: str
    pair_address: str
    dex_id: str
    price_usd: float
    liquidity_usd: float
    fdv: float
    market_cap: float
    age_minutes: float
    volume_m5: float
    volume_h1: float
    volume_h24: float
    buys_m5: int
    sells_m5: int
    buys_h1: int
    sells_h1: int
    change_m5: float
    change_h1: float
    change_h6: float
    change_h24: float
    boosted: bool = False

    @classmethod
    def from_dexscreener(cls, p: dict[str, Any], now_ms: int, boosted: bool = False) -> "PairSnapshot":
        def f(x: Any) -> float:
            try:
                return float(x)
            except (TypeError, ValueError):
                return 0.0

        txns = p.get("txns") or {}
        vol = p.get("volume") or {}
        chg = p.get("priceChange") or {}
        created = p.get("pairCreatedAt") or now_ms
        return cls(
            token_address=p["baseToken"]["address"],
            symbol=p["baseToken"].get("symbol", "?"),
            pair_address=p.get("pairAddress", ""),
            dex_id=p.get("dexId", ""),
            price_usd=f(p.get("priceUsd")),
            liquidity_usd=f((p.get("liquidity") or {}).get("usd")),
            fdv=f(p.get("fdv")),
            market_cap=f(p.get("marketCap")),
            age_minutes=max(0.0, (now_ms - created) / 60_000),
            volume_m5=f(vol.get("m5")),
            volume_h1=f(vol.get("h1")),
            volume_h24=f(vol.get("h24")),
            buys_m5=int((txns.get("m5") or {}).get("buys", 0)),
            sells_m5=int((txns.get("m5") or {}).get("sells", 0)),
            buys_h1=int((txns.get("h1") or {}).get("buys", 0)),
            sells_h1=int((txns.get("h1") or {}).get("sells", 0)),
            change_m5=f(chg.get("m5")),
            change_h1=f(chg.get("h1")),
            change_h6=f(chg.get("h6")),
            change_h24=f(chg.get("h24")),
            boosted=boosted,
        )


@dataclass
class SafetyReport:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    decimals: int | None = None
    top10_pct: float | None = None
    rugcheck_score: float | None = None


@dataclass
class Position:
    token_address: str
    symbol: str
    decimals: int
    entry_price: float
    qty: float                  # unidades "humanas" (UI) que quedan
    initial_qty: float
    cost_usd: float             # USD invertidos en total
    realized_usd: float         # USD recuperados por ventas parciales
    entry_liquidity: float
    opened_at: float            # epoch segundos
    peak_price: float
    tp_levels_hit: list[int] = field(default_factory=list)
    pool: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Position":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
