"""Panel web en tiempo real (solo librería estándar).

  python -m bot.dashboard --state state            # http://127.0.0.1:8050

Lee lo que escribe el bot en la carpeta de estado (status.json, equity.csv,
trades.csv, decisions.jsonl) y permite pausar entradas o cerrar todas las
posiciones escribiendo state/control.json, que el bot lee en cada ciclo.

Seguridad:
  * Por defecto solo escucha en 127.0.0.1. En un VPS, ábrelo con un túnel SSH:
      ssh -L 8050:127.0.0.1:8050 usuario@tu-vps     y abre http://127.0.0.1:8050
  * Para escuchar en otra interfaz (--host 0.0.0.0) es obligatorio DASHBOARD_TOKEN en .env;
    entonces se abre como http://IP:8050/?token=TU_TOKEN
"""
from __future__ import annotations

import argparse
import csv
import hmac
import json
import os
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

from . import version
from .history import positions, summarize

STATIC = Path(__file__).parent / "static" / "dashboard.html"
VENDOR = Path(__file__).parent / "static" / "vendor"
MAX_POINTS = 1500


def _read_json(f: Path, default):
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return default


def _tail_lines(f: Path, n: int) -> list[str]:
    if not f.exists():
        return []
    with f.open("rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - 200_000))
        return fh.read().decode(errors="ignore").splitlines()[-n:]


def equity_series(state: Path) -> list[list[float]]:
    f = state / "equity.csv"
    if not f.exists():
        return []
    pts = []
    with f.open() as fh:
        for r in csv.DictReader(fh):
            try:
                pts.append([int(r["ts"]), float(r["equity"])])
            except (KeyError, ValueError):
                continue
    if len(pts) > MAX_POINTS:
        step = len(pts) / MAX_POINTS
        pts = [pts[int(i * step)] for i in range(MAX_POINTS)] + [pts[-1]]
    return pts


def chart_list(state: Path) -> list[dict]:
    """Tokens con gráfico disponible: primero posiciones abiertas, luego señales, luego lo más reciente."""
    idx = _read_json(state / "charts" / "index.json", [])
    return sorted(idx, key=lambda c: (not c.get("position"), not c.get("signal"), -c.get("ts", 0)))


def chart_detail(state: Path, pool: str) -> dict | None:
    if not pool or not all(ch.isalnum() for ch in pool):
        return None
    data = _read_json(state / "charts" / f"{pool}.json", None)
    if data is None:
        return None
    mint = data.get("mint", "")
    trades = []
    f = state / "trades.csv"
    if f.exists() and mint:
        with f.open() as fh:
            for r in csv.DictReader(fh):
                if r.get("mint") == mint:
                    trades.append({"ts": r["ts"], "side": r["side"], "price": float(r["price"]),
                                   "usd": float(r["usd"]), "reason": r["reason"]})
    status = _read_json(state / "status.json", {})
    pos = next((p for p in status.get("positions", []) if p.get("mint") == mint), None)
    exits = (_read_json(state / "brain.json", {}).get("champion") or {}).get("exits_bt")
    data.update({"trades": trades, "position": pos, "exits": exits})
    return data


def _stocks_view(state: Path) -> dict:
    st = _read_json(state / "stocks.json", {})
    if not st:
        return {}
    sb = _read_json(state / "stocks_brain.json", {})
    brain = {k: sb.get(k) for k in ("ts", "lessons", "meta")}
    brain["setups"] = {k: {kk: v.get(kk) for kk in ("params", "in_sample", "out_of_sample", "live", "active",
                                                   "suspended", "suspended_why", "edge")}
                       for k, v in (sb.get("setups") or {}).items()}
    opened = [dict(p, symbol=s) for s, p in (st.get("positions") or {}).items()]
    return {"paper": st.get("paper"), "open_alerts": opened, "brain": brain if sb else None, "market_up": st.get("market_up"), "ts": st.get("ts"), "universe": st.get("universe"), "watchlist": st.get("watchlist", []), "positions": st.get("positions", {}),
            "alerts": st.get("alerts", [])[-40:][::-1], "closed": st.get("closed", [])[-40:][::-1],
            "track": st.get("track", {})}


def _finite(o):
    """JSON válido para el navegador: infinito / NaN (p. ej. profit factor sin pérdidas) -> null."""
    import math
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _finite(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_finite(v) for v in o]
    return o


_TRADE_CACHE: dict[str, tuple[float, dict]] = {}


def trade_detail(state: Path, mint: str, client=None) -> dict | None:
    """Gráfico y valor actual de una operación cerrada: velas desde antes de la compra hasta ahora."""
    import httpx

    if not mint or not all(ch.isalnum() for ch in mint):
        return None
    f = state / "trades.csv"
    done = [d for d in (positions(f) if f.exists() else []) if d.get("mint") == mint]
    if not done:
        return None
    trade = done[-1]
    hit = _TRADE_CACHE.get(mint)
    if hit and time.time() - hit[0] < 60:
        return {**hit[1], "trade": trade}
    disk_f = state / "trade_charts" / f"{mint}.json"
    disk = _read_json(disk_f, {})
    http = client or httpx.Client(timeout=15, headers={"User-Agent": "Mozilla/5.0 (compatible; MemeBot/1.0)"})
    out = {"price_now": None, "liquidity": None, "pool": None, "candles": [], "tf": None, "alive": False}
    try:
        pairs = http.get(f"https://api.dexscreener.com/tokens/v1/solana/{mint}").json() or []
        pairs = [p for p in pairs if (p.get("baseToken") or {}).get("address") == mint]
        if pairs:
            best = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
            out.update(price_now=float(best.get("priceUsd") or 0), pool=best.get("pairAddress"),
                       liquidity=float((best.get("liquidity") or {}).get("usd") or 0), alive=True)
    except (httpx.HTTPError, ValueError, TypeError):
        pass
    if out["pool"]:
        from datetime import datetime
        opened = datetime.fromisoformat(trade["opened"]).timestamp()
        span_min = (time.time() - opened) / 60 + 60
        if span_min <= 900:
            tf, agg, lim = "minute", 1, int(span_min) + 10
        elif span_min <= 5 * 1000:
            tf, agg, lim = "minute", 5, int(span_min / 5) + 10
        elif span_min <= 15 * 1000:
            tf, agg, lim = "minute", 15, int(span_min / 15) + 10
        else:
            tf, agg, lim = "hour", 1, min(1000, int(span_min / 60) + 10)
        try:
            for attempt in range(3):          # GeckoTerminal limita peticiones: esperar y reintentar
                r = http.get(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{out['pool']}/ohlcv/{tf}",
                             params={"aggregate": agg, "limit": min(lim, 1000), "currency": "usd", "token": "base"})
                if r.status_code != 429:
                    break
                time.sleep(2 * (attempt + 1))
            rows = ((r.json().get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
            out["candles"] = sorted(([float(x) for x in row] for row in rows), key=lambda x: x[0])
            out["tf"] = f"{agg} {'h' if tf == 'hour' else 'min'}"
        except (httpx.HTTPError, ValueError, TypeError):
            pass
    if out["candles"]:
        out["source"] = "live"
        try:                                     # guardar en disco: la próxima vez no depende de GeckoTerminal
            disk_f.parent.mkdir(exist_ok=True)
            disk_f.write_text(json.dumps({"ts": int(time.time()), "pool": out["pool"], "source": "live",
                                          "candles": out["candles"], "tf": out["tf"]}))
        except OSError:
            pass
    elif disk.get("candles"):                    # GeckoTerminal saturado: velas guardadas (del panel o del bot)
        out.update(candles=disk["candles"], tf=disk.get("tf") or "1 min", source="bot" if disk.get("source") == "bot" else "cache",
                   saved_min=int((time.time() - disk.get("ts", time.time())) / 60))
        out["pool"] = out["pool"] or disk.get("pool")
    else:
        for cf in (state / "charts").glob("*.json") if (state / "charts").exists() else []:
            c = _read_json(cf, {})
            if c.get("mint") == mint and c.get("candles"):
                out.update(candles=c["candles"], tf="1 min", source="bot",
                           saved_min=int((time.time() - c.get("ts", time.time())) / 60))
                break
    if out["candles"] or not out["alive"]:        # sin velas por un fallo momentáneo: no guardar, reintentar luego
        _TRADE_CACHE[mint] = (time.time(), out)
    return {**out, "trade": trade}


_STOCK_LIVE: dict[str, tuple[float, dict]] = {}


def stock_live(state: Path, sym: str, client=None) -> dict | None:
    """Velas intradía (con pre-market y after-hours) de una acción de la cartera simulada, con su entrada y salida."""
    import httpx
    st = _read_json(state / "stocks.json", {})
    pos = (st.get("positions") or {}).get(sym)
    closed = [c for c in st.get("closed") or [] if c.get("symbol") == sym]
    if not pos and not closed:
        return None
    hit = _STOCK_LIVE.get(sym)
    if hit and time.time() - hit[0] < 30:
        return hit[1]
    rec = pos or closed[-1]
    since = float(rec.get("entry_ts") or time.time() - 86400)
    age_d = (time.time() - since) / 86400
    if age_d < 4:
        rng, iv = "5d", "5m"
    elif age_d < 25:
        rng, iv = "1mo", "30m"
    else:
        rng, iv = "6mo", "1d"
    http = client or httpx.Client(timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    out = {"symbol": sym, "interval": iv, "candles": [], "position": pos, "closed": closed[-5:],
           "alerts": [a for a in st.get("alerts") or [] if a.get("symbol") == sym][-10:]}
    row = next((r for r in st.get("watchlist") or [] if r.get("symbol") == sym), {})
    out.update(name=row.get("name") or rec.get("name") or sym, currency=row.get("currency") or rec.get("currency", ""),
               eur_rate=row.get("eur_rate") or 1.0)
    try:
        r = http.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                     params={"range": rng, "interval": iv, "includePrePost": "true" if iv != "1d" else "false"})
        res = r.json()["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        rows = [[int(t), o, h, lo, c, v or 0] for t, o, h, lo, c, v in
                zip(res.get("timestamp") or [], q["open"], q["high"], q["low"], q["close"], q["volume"])
                if None not in (o, h, lo, c)]
        out["candles"] = rows
        out["price_now"] = rows[-1][4] if rows else None
        per = res["meta"].get("currentTradingPeriod") or {}
        now = time.time()
        out["session"] = next((k for k in ("pre", "regular", "post") if (per.get(k) or {}).get("start", 0) <= now
                               <= (per.get(k) or {}).get("end", 0)), "closed")
    except Exception:  # noqa: BLE001
        pass
    if out["candles"]:
        _STOCK_LIVE[sym] = (time.time(), out)
    return out


def _hunter_view(state: Path) -> dict:
    h = _read_json(state / "hunter.json", {})
    sl = h.get("sleeves") or {}
    return {"ts": h.get("ts"), "summary": h.get("summary") or {}, "watch_n": h.get("watch_n"),
            "open": {k: list((v.get("positions") or {}).values())[:25] for k, v in sl.items()},
            "closed": {k: list(reversed((v.get("closed") or [])[-20:])) for k, v in sl.items()},
            "study": {k: v for k, v in _read_json(state / "moonshot.json", {}).items()},
            "copy": {k: v for k, v in _read_json(state / "copy.json", {}).items()},
            "traders": _traders_view(state)}


def _traders_view(state: Path) -> dict:
    t = _read_json(state / "traders.json", {})
    rank = {"habilidad probable": 0, "posible suerte": 1, "pierde": 2, "bot": 3, "pocos datos": 4}
    prof = sorted(((w, v) for w, v in (t.get("profiles") or {}).items()),
                  key=lambda kv: (rank.get(kv[1].get("verdict"), 9), -(kv[1].get("pnl_sol") or 0)))
    return {"tally": t.get("tally") or {}, "ts": t.get("ts"),
            "top": [{"wallet": w, **v} for w, v in prof[:25]]}


def build_state(state: Path) -> dict:
    status = _read_json(state / "status.json", {})
    now = time.time()
    interval = status.get("interval", 20)
    online = bool(status) and now - status.get("ts", 0) < max(90, 4 * interval)
    trades_csv = state / "trades.csv"
    closed = positions(trades_csv) if trades_csv.exists() else []
    stats = summarize(closed) if closed else {"n": 0}
    fills = []
    if trades_csv.exists():
        lines = _tail_lines(trades_csv, 41)
        header = "ts,side,symbol,mint,qty,usd,price,reason,tx"
        rows = list(csv.DictReader([header] + [ln for ln in lines if not ln.startswith("ts,")]))
        fills = rows[::-1][:40]
    decisions = []
    for ln in _tail_lines(state / "decisions.jsonl", 30)[::-1]:
        try:
            d = json.loads(ln)
            decisions.append({k: d.get(k) for k in ("ts", "symbol", "mint", "signal", "liq", "ret_60m", "accel", "fdv")})
        except ValueError:
            continue
    control = _read_json(state / "control.json", {})
    brain = _read_json(state / "brain.json", {})
    brain_view = {k: brain.get(k) for k in ("version", "status", "status_text", "updated_at", "samples", "pools",
                                            "cost_penalty", "lessons", "reality", "ml", "learn_seconds", "learn_mode",
                                            "last_full_learn", "running_since", "next_learn", "needs")}
    champ = brain.get("champion") or {}
    brain_view["champion"] = {k: champ.get(k) for k in ("kind", "strategy", "version", "test", "promoted_at",
                                                        "exits_bt", "threshold", "params")} if champ else None
    brain_view["candidates"] = [{k: c.get(k) for k in ("kind", "strategy", "test", "score")}
                                for c in brain.get("candidates", [])]
    brain_view["history"] = brain.get("history", [])[-120:]
    return {
        "now": int(now), "online": online, "status": status, "equity": equity_series(state),
        "closed": [{k: v for k, v in c.items() if k != "reasons"} | {"reason": (c["reasons"] or ["?"])[-1]}
                   for c in closed[::-1][:100]],
        "stats": stats, "fills": fills, "decisions": decisions, "brain": brain_view if brain else None,
        "charts": chart_list(state)[:40],
        "version": version.read()[:7], "update": _read_json(state / "update.json", {}),
        "context": _read_json(state / "context.json", {}),
        "stocks": _stocks_view(state),
        "hunter": _hunter_view(state),
        "analyst": {k: v for k, v in _read_json(state / "analyst.json", {}).items() if k in ("review", "usage", "track", "history")}
                   | {"decisions": _read_json(state / "analyst.json", {}).get("decisions", [])[-15:][::-1]},
        "control": {"stocks_muted": control.get("stocks_alerts") is False,
                    "paused": bool(control.get("paused", status.get("paused", False))),
                    "close_all_pending": bool(control.get("close_all"))},
    }


def make_handler(state: Path, token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MemeBotPanel"

        def log_message(self, fmt, *args):  # silencioso
            pass

        def _authorized(self) -> bool:
            if not token:
                return True
            q = parse_qs(urlparse(self.path).query)
            given = self.headers.get("X-Token") or (q.get("token") or [""])[0]
            return hmac.compare_digest(given, token)

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200):
            self._send(code, json.dumps(_finite(obj), allow_nan=False).encode(), "application/json")

        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            if path.startswith("/vendor/"):          # librería pública de gráficos: sin token
                name = path.rsplit("/", 1)[-1]
                f = VENDOR / name
                if name and f.is_file() and f.parent == VENDOR:
                    ctype = "application/javascript" if name.endswith(".js") else "text/plain; charset=utf-8"
                    return self._send(200, f.read_bytes(), ctype)
                return self._send(404, b"no encontrado", "text/plain")
            if not self._authorized():
                return self._send(HTTPStatus.UNAUTHORIZED, b"token requerido", "text/plain")
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                return self._send(200, STATIC.read_bytes(), "text/html; charset=utf-8")
            if path == "/api/state":
                return self._json(build_state(state))
            if path == "/api/stock_chart":
                sym = (parse_qs(urlparse(self.path).query).get("symbol") or [""])[0].upper()
                if not sym or not all(ch.isalnum() or ch in ".-^=" for ch in sym):
                    return self._json({"error": "símbolo no válido"}, 400)
                d = _read_json(state / "stock_charts" / f"{sym.replace('.', '_')}.json", None)
                return self._json(d) if d else self._json({"error": "sin gráfico"}, 404)
            if path == "/api/stock_live":
                sym = (parse_qs(urlparse(self.path).query).get("symbol") or [""])[0].upper()
                if not sym or not all(ch.isalnum() or ch in ".-^=" for ch in sym):
                    return self._json({"error": "símbolo no válido"}, 400)
                d = stock_live(state, sym)
                return self._json(d) if d else self._json({"error": "no está en tu cartera"}, 404)
            if path == "/api/trade":
                mint = (parse_qs(urlparse(self.path).query).get("mint") or [""])[0]
                d = trade_detail(state, mint)
                return self._json(d) if d else self._json({"error": "operación no encontrada"}, 404)
            if path == "/api/chart":
                pool = (parse_qs(urlparse(self.path).query).get("pool") or [""])[0]
                d = chart_detail(state, pool)
                return self._json(d) if d else self._json({"error": "sin gráfico"}, 404)
            self._send(404, b"no encontrado", "text/plain")

        def do_POST(self):  # noqa: N802
            if not self._authorized():
                return self._json({"error": "token requerido"}, 401)
            if urlparse(self.path).path != "/api/control":
                return self._json({"error": "no encontrado"}, 404)
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(n, 10_000)) or b"{}")
            except ValueError:
                return self._json({"error": "json inválido"}, 400)
            action = body.get("action")
            f = state / "control.json"
            ctl = _read_json(f, {})
            if action == "pause":
                ctl["paused"] = True
            elif action == "resume":
                ctl["paused"] = False
            elif action == "close_all":
                ctl["close_all"] = True
            elif action in ("stocks_mute", "stocks_unmute"):
                ctl["stocks_alerts"] = action == "stocks_unmute"
            else:
                return self._json({"error": "acción desconocida"}, 400)
            ctl["updated_at"] = int(time.time())
            f.write_text(json.dumps(ctl))
            self._json({"ok": True, "control": ctl})

    return Handler


def serve(state: str, host: str, port: int, token: str):
    st = Path(state)
    st.mkdir(parents=True, exist_ok=True)
    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        raise SystemExit("Para escuchar fuera de localhost define DASHBOARD_TOKEN en .env")
    httpd = ThreadingHTTPServer((host, port), make_handler(st, token))
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/" + (f"?token={token}" if token else "")
    print(f"Panel en {url}  (estado: {st.resolve()})")
    import threading

    def watch():                        # reinicio automático tras una actualización
        while True:
            time.sleep(20)
            if version.changed():
                os._exit(0)
    threading.Thread(target=watch, daemon=True).start()
    httpd.serve_forever()


def main():
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="state")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8050)
    a = ap.parse_args()
    serve(a.state, a.host, a.port, os.getenv("DASHBOARD_TOKEN", ""))


if __name__ == "__main__":
    main()
