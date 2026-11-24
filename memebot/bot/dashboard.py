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

from .history import positions, summarize

STATIC = Path(__file__).parent / "static" / "dashboard.html"
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
    return {
        "now": int(now), "online": online, "status": status, "equity": equity_series(state),
        "closed": [{k: v for k, v in c.items() if k != "reasons"} | {"reason": (c["reasons"] or ["?"])[-1]}
                   for c in closed[::-1][:100]],
        "stats": stats, "fills": fills, "decisions": decisions,
        "control": {"paused": bool(control.get("paused", status.get("paused", False))),
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
            self._send(code, json.dumps(obj).encode(), "application/json")

        def do_GET(self):  # noqa: N802
            if not self._authorized():
                return self._send(HTTPStatus.UNAUTHORIZED, b"token requerido", "text/plain")
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                return self._send(200, STATIC.read_bytes(), "text/html; charset=utf-8")
            if path == "/api/state":
                return self._json(build_state(state))
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
