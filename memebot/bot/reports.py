"""Informe diario de rendimiento que el bot sube a GitHub (rama `bot-reports`) para que la sesión semanal de
mejora (Claude) lo analice y proponga cambios con pruebas.

Solo contiene resultados y estadísticas: NUNCA claves, .env, config.yaml ni la dirección de la cartera.
Necesita que el GITHUB_TOKEN tenga permiso «Contents: Read and write» en el repositorio.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path

import httpx

log = logging.getLogger("reports")
API = "https://api.github.com"
BRANCH = "bot-reports"
REPORT_HOUR = 21                    # hora de España; se sube una vez al día a partir de esta hora


def _read(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def _pick(d: dict, keys) -> dict:
    return {k: d.get(k) for k in keys if k in d}


def build(state: Path, days: int = 30) -> dict:
    now = time.time()
    st = _read(state / "status.json")
    brain = _read(state / "brain.json")
    sb = _read(state / "stocks_brain.json")
    stocks = _read(state / "stocks.json")
    closed = []
    f = state / "trades.csv"
    if f.exists():
        from datetime import datetime
        from .history import positions
        for d in positions(f):
            try:
                if now - datetime.fromisoformat(d["closed"]).timestamp() > days * 86400:
                    continue
            except (KeyError, ValueError):
                continue
            closed.append({k: d.get(k) for k in ("symbol", "opened", "closed", "ret", "cost", "got", "reasons")})
    rets = [c["ret"] for c in closed if c.get("ret") is not None]
    crypto_stats = {"n": len(rets), "win_rate": round(100 * sum(r > 0 for r in rets) / len(rets), 1) if rets else None,
                    "avg_ret_pct": round(100 * sum(rets) / len(rets), 2) if rets else None}
    setups = {}
    for k, v in (sb.get("setups") or {}).items():
        setups[k] = _pick(v, ("params", "active", "edge", "regions", "in_sample", "out_of_sample", "out_of_sample_all",
                              "by_region", "by_broker", "live", "suspended", "suspended_why", "exit_reasons_oos"))
    return {
        "generated_at": int(now), "days": days,
        "version": (_read(state / "update.json").get("current") or "")[:12],
        "crypto": {
            "status": _pick(st, ("mode", "strategy", "paused", "equity", "start_equity", "daily_pnl", "risk_off",
                                 "last_scan", "interval")),
            "open_positions": len(st.get("positions") or []),
            "closed_last_days": closed[-300:], "closed_stats": crypto_stats,
        },
        "crypto_brain": {**_pick(brain, ("status", "status_text", "samples", "pools", "version", "champion",
                                         "reality", "cost_penalty", "needs", "updated_at")),
                         "history": (brain.get("history") or [])[-30:]},
        "stocks": {"paper": stocks.get("paper"), "universe": stocks.get("universe"),
                   "market_up": stocks.get("market_up"), "closed": (stocks.get("closed") or [])[-100:],
                   "brain_meta": sb.get("meta"), "setups": setups, "lessons": sb.get("lessons"),
                   "history": (sb.get("history") or [])[-30:]},
        "hunter": {"summary": _read(state / "hunter.json").get("summary"),
                   "closed": {k: v.get("closed", [])[-100:] for k, v in
                              (_read(state / "hunter.json").get("sleeves") or {}).items()},
                   "study": _read(state / "moonshot.json"),
                   "copy": {k: v for k, v in _read(state / "copy.json").items() if k != "watch"},
                   "traders": {"tally": _read(state / "traders.json").get("tally"),
                               "skilled": {w[:6]: {k: x for k, x in v.items() if k != "why"} for w, v in
                                           (_read(state / "traders.json").get("profiles") or {}).items()
                                           if v.get("verdict") == "habilidad probable"}}},
        "updates": _pick(_read(state / "update.json"), ("state", "current", "applied_at", "error")),
    }


def _gh(token: str) -> httpx.Client:
    return httpx.Client(timeout=60, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})


def ensure_branch(http: httpx.Client, repo: str, base: str):
    r = http.get(f"{API}/repos/{repo}/git/ref/heads/{BRANCH}")
    if r.status_code == 200:
        return
    b = http.get(f"{API}/repos/{repo}/git/ref/heads/{base}")
    b.raise_for_status()
    c = http.post(f"{API}/repos/{repo}/git/refs", json={"ref": f"refs/heads/{BRANCH}", "sha": b.json()["object"]["sha"]})
    c.raise_for_status()


def put_file(http: httpx.Client, repo: str, path: str, text: str, message: str):
    url = f"{API}/repos/{repo}/contents/{path}"
    old = http.get(url, params={"ref": BRANCH})
    body = {"message": message, "branch": BRANCH, "content": base64.b64encode(text.encode()).decode()}
    if old.status_code == 200:
        body["sha"] = old.json()["sha"]
    r = http.put(url, json=body)
    r.raise_for_status()


def publish(state: Path, repo: str, base: str, token: str, http: httpx.Client | None = None, day: str = "") -> bool:
    http = http or _gh(token)
    rep = build(state)
    text = json.dumps(rep, ensure_ascii=False, indent=1, default=str)
    ensure_branch(http, repo, base)
    put_file(http, repo, "reports/latest.json", text, f"Informe del bot {day}")
    if day:
        put_file(http, repo, f"reports/{day}.json", text, f"Informe del bot {day}")
    return True


def maybe_publish(state: Path, repo: str, base: str, token: str, notify, now: float | None = None,
                  http: httpx.Client | None = None) -> bool:
    """Una vez al día (a partir de las 21:00 de España). Si el token no tiene permiso, avisa una sola vez."""
    from .watchdog import madrid_now
    if not token:
        return False
    m = madrid_now(now)
    day = m.strftime("%Y-%m-%d")
    mark = state / "report_sent.json"
    info = _read(mark)
    first = not info                                # el primero se sube al arrancar: así se comprueba el token
    if (m.hour < REPORT_HOUR and not first) or info.get("day") == day or time.time() - info.get("failed_at", 0) < 3 * 3600:
        return False
    try:
        publish(state, repo, base, token, http, day)
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        info["failed_at"] = int(time.time())
        if code in (401, 403, 404) and not info.get("warned"):
            info["warned"] = True
            notify("⚠️ No he podido subir el informe diario a GitHub: tu GITHUB_TOKEN no tiene permiso de escritura. "
                   "En GitHub → Settings → Developer settings → Fine-grained tokens, edita el token y pon "
                   "«Contents: Read and write». Sin esto la mejora automática semanal no tiene datos.")
        mark.write_text(json.dumps(info))
        log.warning("No se pudo subir el informe: %s", e)
        return False
    except httpx.HTTPError as e:
        info["failed_at"] = int(time.time())
        mark.write_text(json.dumps(info))
        log.warning("No se pudo subir el informe: %s", e)
        return False
    mark.write_text(json.dumps({"day": day if m.hour >= REPORT_HOUR else "", "ts": int(time.time())}))
    log.info("Informe diario subido a la rama %s", BRANCH)
    if first:
        notify("✅ Primer informe subido a GitHub (rama bot-reports): el token tiene permiso de escritura y la "
               "mejora automática de los domingos ya tiene datos.")
    return True
