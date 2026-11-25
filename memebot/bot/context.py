"""Contexto de mercado: sentimiento, tendencia de SOL y noticias.

Fuentes gratuitas y sin clave:
  * Índice de miedo y codicia (alternative.me): 0 = miedo extremo, 100 = codicia extrema.
  * SOL: variación 1 h y 24 h (DexScreener). Si SOL se hunde, las memecoins de Solana caen detrás.
  * Titulares de CoinDesk, Cointelegraph y Decrypt, puntuados por palabras clave, con
    ALERTA CRÍTICA si mencionan un hackeo, exploit o caída de Solana o de su ecosistema.

Uso:
  * El bot frena las entradas nuevas si SOL se desploma o hay una alerta crítica reciente.
  * El cerebro recibe estos valores como factores y aprende si predicen mejores o peores operaciones.
"""
from __future__ import annotations

import email.utils
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

log = logging.getLogger("context")
SOL = "So11111111111111111111111111111111111111112"
FEEDS = {"CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
         "Cointelegraph": "https://cointelegraph.com/rss",
         "Decrypt": "https://decrypt.co/feed"}
UA = {"User-Agent": "Mozilla/5.0 (compatible; MemeBot/1.0)"}

NEG = ["hack", "exploit", "drain", "stolen", "rug pull", "rugpull", "scam", "crash", "plunge", "tumble", "dump",
       "lawsuit", "sues", "charged", "ban", "outage", "halt", "liquidat", "bankrupt", "fraud", "arrest", "selloff",
       "sell-off", "bear", "fear", "delist", "vulnerab", "attack", "collapse", "warning", "slump", "freeze"]
POS = ["surge", "soar", "rally", "record", "all-time high", "ath", "approv", "etf", "partnership", "adoption",
       "bull", "inflow", "jumps", "gains", "rebound", "breakout", "upgrade", "launches", "integrat", "listing"]
SOLANA = ["solana", " sol ", "sol price", "memecoin", "meme coin", "pump.fun", "pumpfun", "jupiter", "raydium",
          "phantom", "bonk", "dogwifhat", "wif ", "helius", "jito"]
CRITICAL = ["hack", "exploit", "drain", "outage", "halt", "stolen", "attack", "vulnerab", "down for"]
# piezas clave: si caen o las hackean, afecta a TODO el mercado de memecoins de Solana
CORE = ["jupiter", "raydium", "pump.fun", "pumpfun", "phantom", "jito", "helius", "meteora", "orca "]
# palabras que indican que el problema es de la RED Solana (no de un proyecto pequeño que usa Solana)
NETWORK = ["solana network", "solana outage", "solana down", "solana mainnet", "solana validators", "solana halts",
           "solana halted", "solana blockchain halt", "network outage", "network halt", "mainnet", "consensus"]


def _has(text: str, words: list[str]) -> bool:
    return any(w in text for w in words)


def score_headline(title: str) -> dict:
    t = f" {title.lower()} "
    neg = sum(1 for w in NEG if w in t)
    pos = sum(1 for w in POS if w in t)
    sol = _has(t, SOLANA)
    s = (pos - neg) * (2 if sol else 1)
    critical = _has(t, CRITICAL) and (_has(t, CORE) or _has(t, NETWORK))
    return {"score": max(-3, min(3, s)), "solana": sol, "critical": critical}


def fetch_headlines(http: httpx.Client, max_age_h: float = 24) -> list[dict]:
    out = []
    now = time.time()
    for src, url in FEEDS.items():
        try:
            r = http.get(url, headers=UA, follow_redirects=True)
            if r.status_code != 200:
                continue
            root = ET.fromstring(r.content)
        except (httpx.HTTPError, ET.ParseError) as e:
            log.info("Noticias %s no disponibles: %s", src, e)
            continue
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            if not title:
                continue
            try:
                ts = email.utils.parsedate_to_datetime(it.findtext("pubDate") or "").timestamp()
            except (TypeError, ValueError):
                ts = now
            if now - ts > max_age_h * 3600:
                continue
            out.append({"source": src, "title": re.sub(r"\s+", " ", title)[:200], "link": (it.findtext("link") or "").strip(),
                        "ts": int(ts), **score_headline(title)})
    out.sort(key=lambda x: -x["ts"])
    return out[:60]


def fetch_fng(http: httpx.Client) -> tuple[int | None, str]:
    try:
        d = http.get("https://api.alternative.me/fng/?limit=1", headers=UA).json()["data"][0]
        return int(d["value"]), d.get("value_classification", "")
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        return None, ""


def fetch_sol(http: httpx.Client) -> dict:
    try:
        pairs = http.get(f"https://api.dexscreener.com/tokens/v1/solana/{SOL}").json() or []
        best = max((p for p in pairs if p.get("baseToken", {}).get("address") == SOL),
                   key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
        ch = best.get("priceChange") or {}
        return {"price": float(best.get("priceUsd") or 0), "h1": float(ch.get("h1") or 0), "h24": float(ch.get("h24") or 0)}
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return {}


def build(http: httpx.Client | None = None) -> dict:
    http = http or httpx.Client(timeout=20)
    heads = fetch_headlines(http)
    fng, fng_txt = fetch_fng(http)
    sol = fetch_sol(http)
    recent = [h for h in heads if time.time() - h["ts"] < 12 * 3600]
    news_score = round(sum(h["score"] for h in recent) / max(1, len(recent)), 3) if recent else None
    alerts = [h for h in heads if h["critical"] and time.time() - h["ts"] < 6 * 3600]
    return {"ts": int(time.time()), "fng": fng, "fng_text": fng_txt, "sol_price": sol.get("price"),
            "sol_h1": sol.get("h1"), "sol_h24": sol.get("h24"), "news_score": news_score,
            "news_n": len(recent), "alerts": alerts[:5], "headlines": heads[:25]}


def risk_off(ctx: dict, sol_h1_drop: float, sol_h24_drop: float, pause_on_alert: bool) -> str:
    """Motivo para frenar entradas nuevas ("" si no hay)."""
    if not ctx:
        return ""
    if ctx.get("sol_h1") is not None and ctx["sol_h1"] <= -sol_h1_drop:
        return f"SOL cae {ctx['sol_h1']:.1f}% en 1 h"
    if ctx.get("sol_h24") is not None and ctx["sol_h24"] <= -sol_h24_drop:
        return f"SOL cae {ctx['sol_h24']:.1f}% en 24 h"
    if pause_on_alert and ctx.get("alerts"):
        return "alerta crítica en noticias: " + ctx["alerts"][0]["title"][:80]
    return ""


def features_of(ctx: dict | None) -> dict:
    """Factores de contexto para el cerebro (NaN si no hay dato: el modelo lo admite)."""
    nan = float("nan")
    ctx = ctx or {}
    g = lambda k: float(ctx[k]) if ctx.get(k) is not None else nan  # noqa: E731
    return {"fng": g("fng"), "sol_h1": g("sol_h1"), "sol_h24": g("sol_h24"), "news_score": g("news_score")}


def save(path: Path, ctx: dict):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(ctx))
    tmp.replace(path)


def append_history(path: Path, ctx: dict):
    """Historial compacto (para que el cerebro alinee contexto y velas por tiempo)."""
    row = {"ts": ctx["ts"], **{k: ctx.get(k) for k in ("fng", "sol_h1", "sol_h24", "news_score")},
           "alerts": len(ctx.get("alerts") or [])}
    with path.open("a") as fh:
        fh.write(json.dumps(row) + "\n")


def load_history(path: Path) -> tuple:
    """(timestamps ordenados, filas) del historial de contexto."""
    import numpy as np
    if not path.exists():
        return np.array([]), []
    rows = []
    for ln in path.read_text().splitlines():
        try:
            rows.append(json.loads(ln))
        except ValueError:
            continue
    rows.sort(key=lambda r: r["ts"])
    return np.array([r["ts"] for r in rows]), rows


if __name__ == "__main__":
    c = build()
    print(json.dumps({k: v for k, v in c.items() if k != "headlines"}, indent=1, ensure_ascii=False))
    for h in c["headlines"][:10]:
        print(f"{h['score']:+d} {'⚠️ ' if h['critical'] else ''}{'[SOL] ' if h['solana'] else ''}{h['source']}: {h['title']}")
