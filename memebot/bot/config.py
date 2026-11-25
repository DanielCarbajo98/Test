"""Carga de configuración (YAML) y secretos (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv


@dataclass
class ScannerCfg:
    interval_seconds: int = 20          # revisión de posiciones abiertas
    entry_interval_seconds: int = 60    # búsqueda de entradas
    max_candidates: int = 200
    discovery_cache_seconds: int = 120  # la lista de tokens se refresca cada 2 min (ahorra cupo de API)
    include_gecko: bool = True
    include_boosted: bool = True
    include_profiles: bool = True


@dataclass
class FilterCfg:
    """Prefiltro en tiempo real (DexScreener) antes de descargar velas."""
    min_liquidity_usd: float = 40_000
    max_liquidity_usd: float = 100_000_000
    min_age_minutes: float = 30
    max_age_minutes: float = 72 * 60
    min_volume_h1_usd: float = 30_000
    min_txns_h1: int = 200
    min_buy_ratio_m5: float = 0.0     # 0 = desactivado (no se puede backtestear)
    max_fdv_usd: float = 1_000_000_000   # memecoins y también tokens más grandes
    allowed_dexes: list[str] = field(default_factory=lambda: ["raydium", "pumpswap", "meteora", "orca"])


@dataclass
class StrategyCfg:
    """Estrategia validada en backtest (bot/signals.py). Ver reports/ y research/apply_best.py."""
    name: str = "momentum"
    params: dict = field(default_factory=dict)
    max_ohlcv_per_cycle: int = 8      # límite de GeckoTerminal: ~30 peticiones/min


@dataclass
class BrainCfg:
    """Cerebro autónomo (bot/brain.py). Decide la estrategia y las salidas; NUNCA el riesgo."""
    enabled: bool = True
    auto_apply_live: bool = True      # aplica sus decisiones también con dinero real, sin aprobación
    defensive_in_paper: bool = False  # en paper sigue operando sin ventaja demostrada (para aprender)


@dataclass
class SafetyCfg:
    require_mint_revoked: bool = True
    require_freeze_revoked: bool = True
    max_top10_pct: float = 35.0       # % de supply en top-10 (excluyendo la pool, aprox.)
    use_rugcheck: bool = True
    max_rugcheck_danger: int = 0      # nº de riesgos "danger" tolerados
    simulate_sell: bool = True        # comprobar que existe ruta de venta (anti-honeypot)


@dataclass
class RiskCfg:
    starting_equity_usd: float = 1_000
    risk_per_trade_pct: float = 1.0   # % del capital que aceptas perder por operación
    max_position_pct: float = 5.0     # tope de tamaño por posición
    max_pool_share_pct: float = 1.0   # no comprar más del 1% de la liquidez de la pool
    max_open_positions: int = 4
    daily_loss_limit_pct: float = 5.0  # kill-switch diario
    max_drawdown_pct: float = 0.0     # freno total: sin entradas si el saldo cae este % desde el inicio (0 = off)
    cooldown_minutes: int = 240       # no reentrar en el mismo token
    slippage_bps: int = 300
    max_price_impact_pct: float = 3.0  # rechaza órdenes con más impacto


@dataclass
class ExitCfg:
    stop_loss_pct: float = 25.0
    take_profits: list[list[float]] = field(default_factory=lambda: [[100.0, 50.0], [300.0, 25.0]])
    trailing_stop_pct: float = 30.0
    trailing_activation_pct: float = 50.0
    time_stop_minutes: int = 90
    time_stop_min_gain_pct: float = 10.0
    liquidity_drop_exit_pct: float = 40.0


@dataclass
class Config:
    mode: str = "paper"               # paper | live
    quote_mint: str = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # USDC
    quote_decimals: int = 6
    state_dir: str = "state"
    scanner: ScannerCfg = field(default_factory=ScannerCfg)
    filters: FilterCfg = field(default_factory=FilterCfg)
    strategy: StrategyCfg = field(default_factory=StrategyCfg)
    brain: BrainCfg = field(default_factory=BrainCfg)
    safety: SafetyCfg = field(default_factory=SafetyCfg)
    risk: RiskCfg = field(default_factory=RiskCfg)
    exits: ExitCfg = field(default_factory=ExitCfg)
    # secretos (desde .env)
    rpc_url: str = "https://api.mainnet-beta.solana.com"
    jupiter_api_key: str = ""
    wallet_private_key: str = ""
    telegram_token: str = ""
    telegram_chat_id: str = ""


def _section(cls, data: dict | None):
    data = data or {}
    known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
    unknown = set(data) - set(known)
    if unknown:
        raise ValueError(f"Claves desconocidas en {cls.__name__}: {sorted(unknown)}")
    return cls(**known)


def load_config(path: str | Path = "config.yaml") -> Config:
    load_dotenv()
    raw = {}
    p = Path(path)
    if p.exists():
        raw = yaml.safe_load(p.read_text()) or {}
    cfg = Config(
        mode=raw.get("mode", "paper"),
        quote_mint=raw.get("quote_mint", Config.quote_mint),
        quote_decimals=raw.get("quote_decimals", 6),
        state_dir=raw.get("state_dir", "state"),
        scanner=_section(ScannerCfg, raw.get("scanner")),
        filters=_section(FilterCfg, raw.get("filters")),
        strategy=_section(StrategyCfg, raw.get("strategy")),
        brain=_section(BrainCfg, raw.get("brain")),
        safety=_section(SafetyCfg, raw.get("safety")),
        risk=_section(RiskCfg, raw.get("risk")),
        exits=_section(ExitCfg, raw.get("exits")),
        rpc_url=os.getenv("SOLANA_RPC_URL", Config.rpc_url),
        jupiter_api_key=os.getenv("JUPITER_API_KEY", ""),
        wallet_private_key=os.getenv("WALLET_PRIVATE_KEY", ""),
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
    )
    if cfg.mode not in ("paper", "live"):
        raise ValueError("mode debe ser 'paper' o 'live'")
    from .signals import STRATEGIES
    if cfg.strategy.name not in STRATEGIES:
        raise ValueError(f"Estrategia desconocida: {cfg.strategy.name}")
    if cfg.mode == "live" and not (cfg.wallet_private_key and cfg.jupiter_api_key):
        raise ValueError("Modo live requiere WALLET_PRIVATE_KEY y JUPITER_API_KEY en .env")
    return cfg
