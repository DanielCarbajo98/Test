"""Comprobaciones antes de arrancar (sobre todo antes de pasar a dinero real).

  python -m bot.preflight --config config.yaml

Verifica: configuración, RPC, APIs de datos, Jupiter (clave y cotización), wallet
(saldo de USDC y de SOL para comisiones) y los resultados del paper trading.
No envía ninguna transacción.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

import httpx

from .config import load_config
from .executor import Quoter
from .safety import SolanaRPC

OK, WARN, FAIL = "✅", "⚠️ ", "❌"
SOL = "So11111111111111111111111111111111111111112"


def paper_stats(trades_csv: Path) -> dict | None:
    if not trades_csv.exists():
        return None
    rows = list(csv.DictReader(trades_csv.open()))
    by_mint: dict[str, dict] = {}
    for r in rows:
        d = by_mint.setdefault(r["mint"], {"cost": 0.0, "got": 0.0, "closed": False})
        if r["side"] == "BUY":
            d["cost"] += float(r["usd"])
        else:
            d["got"] += float(r["usd"])
    trades = [d["got"] / d["cost"] - 1 for d in by_mint.values() if d["cost"] > 0 and d["got"] > 0]
    if not trades:
        return {"n": 0}
    wins = [t for t in trades if t > 0]
    return {"n": len(trades), "win_rate": 100 * len(wins) / len(trades),
            "expectancy": 100 * sum(trades) / len(trades), "worst": 100 * min(trades)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--paper-state", default=None, help="carpeta state del paper trading a evaluar")
    a = ap.parse_args()
    fails = 0

    def check(cond, good, bad, warn=False):
        nonlocal fails
        if cond:
            print(OK, good)
        else:
            print(WARN if warn else FAIL, bad)
            fails += 0 if warn else 1

    try:
        cfg = load_config(a.config)
        print(OK, f"configuración válida (modo {cfg.mode}, estrategia {cfg.strategy.name})")
    except Exception as e:  # noqa: BLE001
        print(FAIL, f"configuración: {e}")
        raise SystemExit(1)

    rpc = SolanaRPC(cfg.rpc_url)
    try:
        slot = rpc.call("getSlot", [])
        check(True, f"RPC responde (slot {slot})", "")
        check("mainnet-beta.solana.com" not in cfg.rpc_url, "RPC privado configurado",
              "usas el RPC público: ponlo en SOLANA_RPC_URL (Helius/QuickNode) para evitar límites", warn=True)
    except Exception as e:  # noqa: BLE001
        check(False, "", f"RPC no responde: {e}")

    for name, url in [("DexScreener", "https://api.dexscreener.com/token-profiles/latest/v1"),
                      ("GeckoTerminal", "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools"),
                      ("RugCheck", "https://api.rugcheck.xyz/v1/tokens/So11111111111111111111111111111111111111112/report/summary")]:
        try:
            r = httpx.get(url, timeout=15)
            check(r.status_code == 200, f"{name} OK", f"{name} HTTP {r.status_code}", warn=name == "RugCheck")
        except httpx.HTTPError as e:
            check(False, "", f"{name} inaccesible: {e}")

    q = Quoter(cfg.jupiter_api_key, cfg.risk.slippage_bps).quote(cfg.quote_mint, SOL, 10 * 10 ** cfg.quote_decimals)
    check(q is not None, f"Jupiter cotiza ({'API v2 con clave' if cfg.jupiter_api_key else 'lite-api sin clave'})",
          "Jupiter no cotiza: revisa JUPITER_API_KEY")

    if cfg.mode == "live" or cfg.wallet_private_key:
        try:
            from solders.keypair import Keypair
            kp = Keypair.from_base58_string(cfg.wallet_private_key)
            owner = str(kp.pubkey())
            print(OK, f"wallet {owner[:4]}…{owner[-4:]}")
            sol = rpc.call("getBalance", [owner])["value"] / 1e9
            check(sol >= 0.05, f"SOL para comisiones: {sol:.3f}", f"poco SOL para comisiones ({sol:.3f}); deja ≥ 0,05 SOL")
            res = rpc.call("getTokenAccountsByOwner", [owner, {"mint": cfg.quote_mint}, {"encoding": "jsonParsed"}])
            usdc = sum(float(x["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0) for x in res["value"])
            check(usdc >= cfg.risk.starting_equity_usd * 0.95, f"USDC en wallet: {usdc:.2f}",
                  f"USDC en wallet ({usdc:.2f}) < presupuesto del bot ({cfg.risk.starting_equity_usd})")
        except Exception as e:  # noqa: BLE001
            check(False, "", f"wallet: {e}")
        check(bool(cfg.jupiter_api_key), "JUPITER_API_KEY presente", "falta JUPITER_API_KEY (obligatoria en live)")

    st = paper_stats(Path(a.paper_state or cfg.state_dir) / "trades.csv")
    if st is None or st["n"] == 0:
        check(cfg.mode == "paper", "sin operaciones de paper todavía (normal al empezar)",
              "no hay historial de paper trading: haz paper antes de pasar a live", warn=cfg.mode == "paper")
    else:
        print(f"   paper: {st['n']} operaciones, acierto {st['win_rate']:.0f}%, "
              f"esperanza {st['expectancy']:+.1f}%/op, peor {st['worst']:.0f}%")
        check(st["n"] >= 50, "muestra de paper suficiente (≥ 50 operaciones)",
              f"solo {st['n']} operaciones de paper: poco para decidir", warn=True)
        check(st["expectancy"] > 0, "esperanza positiva en paper",
              "esperanza NEGATIVA en paper: no pases a real", warn=cfg.mode == "paper")
    print("\nResultado:", "TODO OK" if fails == 0 else f"{fails} problema(s) que resolver")
    raise SystemExit(1 if fails else 0)


if __name__ == "__main__":
    main()
