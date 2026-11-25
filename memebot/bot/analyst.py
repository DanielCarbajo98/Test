"""Analista Claude: revisa el mercado, las noticias, el bot y las acciones, y aconseja al bot.

  python -m bot.analyst --state state --loop 60      (servicio `analyst` de docker-compose)

Qué hace:
  * REVISIÓN periódica (por defecto cada 60 min): le envía a Claude el contexto completo
    (miedo/codicia, SOL, titulares, saldo, posiciones, últimas operaciones, lo que ha aprendido
    el cerebro, los candidatos analizados y las acciones con señal). Opcionalmente Claude busca
    noticias recientes en la web. Responde con un análisis estructurado.
  * VETO de entradas: cuando el bot va a comprar, consulta a Claude en el momento.

Reglas (en el código, no en el prompt): Claude SOLO puede REDUCIR el riesgo.
  · puede vetar una compra, reducir el tamaño de las posiciones (multiplicador 0–1) y pedir
    cerrar posiciones abiertas;
  · NO puede abrir operaciones, aumentar tamaños ni tocar los límites de pérdida.
  · Cada veto se sigue después para medir si Claude acierta (panel → Analista Claude).
  · Tope diario de llamadas para controlar el coste de la API.
Necesita ANTHROPIC_API_KEY en .env (console.anthropic.com → API Keys; es de pago por uso).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("analyst")

# precios por millón de tokens (entrada, salida) para estimar el coste; búsqueda web por unidad
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}
WEB_SEARCH_PRICE = 0.01
SERVER_FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM = """Eres el analista de riesgos de un bot de trading automatizado que opera tokens de Solana (sobre todo
memecoins) con un capital pequeño, y que además genera alertas de acciones para su dueño, un particular en España.
Tu trabajo es proteger el capital y mejorar las decisiones: detectar entornos peligrosos (caídas de mercado,
hackeos, noticias negativas, tokens con señales de manipulación) y señalar dónde hay oportunidad real.

Cómo decides:
- Usa solo los datos que te doy y, si tienes búsqueda web, noticias recientes y verificables. No inventes cifras.
- Las memecoins suelen vivir horas y la mayoría acaban a cero: sé escéptico con subidas parabólicas, liquidez baja,
  holders concentrados o puntuación orgánica baja (volumen hecho por bots).
- Tu poder es solo de reducir riesgo: puedes vetar, reducir tamaño (risk_multiplier entre 0 y 1) o pedir cerrar
  posiciones. No puedes abrir operaciones. Si no hay motivo claro para intervenir, no intervengas: un veto injustificado
  también cuesta dinero.
- risk_multiplier: 1 = condiciones normales; 0.5 = prudencia; 0 = no abrir nada nuevo. Justifícalo.
- Escribe en español claro y breve, para alguien sin formación financiera. Nada de promesas de ganancias."""

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "regime": {"type": "string", "enum": ["favorable", "neutral", "desfavorable"]},
        "risk_multiplier": {"type": "number"},
        "summary": {"type": "string"},
        "crypto_view": {"type": "string"},
        "stocks_view": {"type": "string"},
        "avoid_tokens": {"type": "array", "items": {"type": "string"}},
        "watch_tokens": {"type": "array", "items": {"type": "string"}},
        "position_actions": {"type": "array", "items": {
            "type": "object",
            "properties": {"mint": {"type": "string"}, "action": {"type": "string", "enum": ["mantener", "cerrar"]},
                           "reason": {"type": "string"}},
            "required": ["mint", "action", "reason"], "additionalProperties": False}},
        "stock_ideas": {"type": "array", "items": {
            "type": "object",
            "properties": {"symbol": {"type": "string"},
                           "action": {"type": "string", "enum": ["comprar", "vender", "mantener", "evitar"]},
                           "reason": {"type": "string"}},
            "required": ["symbol", "action", "reason"], "additionalProperties": False}},
        "news_highlights": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["regime", "risk_multiplier", "summary", "crypto_view", "stocks_view", "avoid_tokens",
                 "watch_tokens", "position_actions", "stock_ideas", "news_highlights"],
    "additionalProperties": False,
}

CHECK_SCHEMA = {
    "type": "object",
    "properties": {"approve": {"type": "boolean"}, "confidence": {"type": "number"}, "reason": {"type": "string"}},
    "required": ["approve", "confidence", "reason"],
    "additionalProperties": False,
}


class Analyst:
    def __init__(self, state_dir: str, model: str = "claude-opus-5-5", effort: str = "medium",
                 web_search: bool = True, web_search_max_uses: int = 3, max_calls_per_day: int = 60,
                 client=None):
        self.dir = Path(state_dir)
        self.file = self.dir / "analyst.json"
        self.model, self.effort = model, effort
        self.web_search, self.web_max = web_search, web_search_max_uses
        self.max_calls = max_calls_per_day
        self._client = client

    # ------------------------------------------------------------------ utilidades
    @property
    def enabled(self) -> bool:
        return bool(self._client or os.getenv("ANTHROPIC_API_KEY"))

    @property
    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(max_retries=2, timeout=180.0)
        return self._client

    def load(self) -> dict:
        try:
            return json.loads(self.file.read_text())
        except (OSError, ValueError):
            return {}

    def save(self, data: dict):
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        tmp.replace(self.file)

    def _budget_ok(self, data: dict) -> bool:
        day = time.strftime("%Y-%m-%d")
        u = data.setdefault("usage", {})
        if u.get("day") != day:
            u.update({"day": day, "calls_today": 0, "cost_today": 0.0})
        return u["calls_today"] < self.max_calls

    def _account(self, data: dict, resp, kind: str):
        u = data.setdefault("usage", {})
        pin, pout = PRICES.get(self.model, (4.0, 20.0))
        us = resp.usage
        inp = (us.input_tokens or 0) + (getattr(us, "cache_creation_input_tokens", 0) or 0) * 1.25 \
            + (getattr(us, "cache_read_input_tokens", 0) or 0) * 0.1
        searches = getattr(getattr(us, "server_tool_use", None), "web_search_requests", 0) or 0
        cost = inp / 1e6 * pin + (us.output_tokens or 0) / 1e6 * pout + searches * WEB_SEARCH_PRICE
        u["calls_today"] = u.get("calls_today", 0) + 1
        u["cost_today"] = round(u.get("cost_today", 0.0) + cost, 4)
        month = time.strftime("%Y-%m")
        if u.get("month") != month:
            u.update({"month": month, "cost_month": 0.0})
        u["cost_month"] = round(u.get("cost_month", 0.0) + cost, 4)
        u["last_call"] = {"kind": kind, "ts": int(time.time()), "cost": round(cost, 4),
                          "input_tokens": us.input_tokens, "output_tokens": us.output_tokens, "web_searches": searches}

    def _call(self, prompt: str, schema: dict, effort: str, web: bool) -> tuple[dict | None, object]:
        tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": self.web_max}] if web else []
        messages = [{"role": "user", "content": prompt}]
        kwargs = dict(model=self.model, max_tokens=16000, system=SYSTEM, messages=messages,
                      output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
                      betas=[SERVER_FALLBACK_BETA], fallbacks="default")
        if tools:
            kwargs["tools"] = tools
        resp = self.client.beta.messages.create(**kwargs)
        for _ in range(3):                         # la búsqueda web puede pausar el turno: continuar
            if resp.stop_reason != "pause_turn":
                break
            messages = messages + [{"role": "assistant", "content": resp.content}]
            resp = self.client.beta.messages.create(**{**kwargs, "messages": messages})
        if resp.stop_reason == "refusal":
            log.warning("Claude no respondió (refusal): %s", getattr(resp, "stop_details", None))
            return None, resp
        texts = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
        for t in reversed(texts):
            try:
                return json.loads(t), resp
            except ValueError:
                continue
        return None, resp

    # ------------------------------------------------------------------ revisión periódica
    def build_brief(self) -> str:
        rd = lambda n, d=None: _read(self.dir / n, d)  # noqa: E731
        status, ctx, brain = rd("status.json", {}), rd("context.json", {}), rd("brain.json", {})
        stocks, charts = rd("stocks.json", {}), rd("charts/index.json", [])
        trades = []
        f = self.dir / "trades.csv"
        if f.exists():
            trades = f.read_text().splitlines()[-21:]
        cands = []
        for c in sorted(charts, key=lambda x: -x.get("ts", 0))[:15]:
            d = rd(f"charts/{c['pool']}.json", {})
            feat = d.get("features", {})
            cands.append({"symbol": c.get("symbol"), "mint": c.get("mint"), "signal": c.get("signal"),
                          "in_portfolio": c.get("position"), "price": c.get("price"),
                          "liquidity": d.get("liquidity"), "fdv": d.get("fdv"), "organic_score": d.get("organic"),
                          "holders": d.get("holders"),
                          "ret_1h_pct": _r(feat.get("ret_60m")), "ret_5m_pct": _r(feat.get("ret_5m")),
                          "vol_1h_usd": _r(feat.get("vol_60m"), 0), "volume_accel": _r(feat.get("accel"), 2),
                          "age_min": _r(feat.get("age_min"), 0),
                          "failed_conditions": [x["label"] for x in d.get("checks", []) if not x["ok"]]})
        wl = stocks.get("watchlist", [])
        stock_rows = [r for r in wl if r.get("signal") or r.get("status") != "vigilando"]
        stock_rows += sorted((r for r in wl if r not in stock_rows), key=lambda r: -abs(r.get("chg_1d", 0)))[:15]
        brief = {
            "fecha_utc": time.strftime("%Y-%m-%d %H:%M"),
            "contexto": {k: ctx.get(k) for k in ("fng", "fng_text", "sol_price", "sol_h1", "sol_h24", "news_score")},
            "titulares": [{"t": h.get("title"), "fuente": h.get("source"), "puntuacion": h.get("score"),
                           "solana": h.get("solana")} for h in (ctx.get("headlines") or [])[:20]],
            "freno_activo": status.get("risk_off"),
            "bot": {"modo": status.get("mode"), "saldo": _r(status.get("equity"), 2), "caja": _r(status.get("cash"), 2),
                    "capital_inicial": status.get("start_equity"), "pnl_hoy": _r(status.get("daily_pnl"), 2),
                    "estrategia": status.get("strategy"),
                    "posiciones": [{k: p.get(k) for k in ("symbol", "mint", "entry_price", "price", "pnl_pct", "opened_at",
                                                          "tp_hit", "liquidity")} for p in status.get("positions", [])]},
            "ultimas_operaciones_csv": trades,
            "cerebro": {"estado": brain.get("status_text"), "lecciones": brain.get("lessons", [])[:5],
                        "resultado_real": brain.get("reality")},
            "candidatos_cripto_recientes": cands,
            "acciones": [{k: r.get(k) for k in ("symbol", "name", "price", "currency", "chg_1d", "chg_1m", "rsi", "trend",
                                                 "signal", "status")} for r in stock_rows[:30]],
            "alertas_acciones_abiertas": list((stocks.get("positions") or {}).values())[:10],
            "seguimiento_de_tus_vetos": self.load().get("track", {}),
        }
        return ("Revisión periódica. Analiza el contexto y devuelve tu evaluación. Si tienes búsqueda web, busca "
                "brevemente noticias de las últimas horas sobre Solana, el mercado cripto y los mercados de acciones.\n\n"
                + json.dumps(brief, ensure_ascii=False, default=str))

    def review(self) -> dict | None:
        data = self.load()
        if not self._budget_ok(data):
            log.info("Tope diario de llamadas a Claude alcanzado; se omite la revisión")
            return None
        out, resp = self._call(self.build_brief(), REVIEW_SCHEMA, self.effort, self.web_search)
        self._account(data, resp, "revisión")
        if out:
            out["risk_multiplier"] = max(0.0, min(1.0, float(out.get("risk_multiplier", 1.0))))
            out["ts"] = int(time.time())
            out["model"] = getattr(resp, "model", self.model)
            out["id"] = f"r{out['ts']}"
            data["review"] = out
            data.setdefault("history", []).append({k: out[k] for k in ("ts", "regime", "risk_multiplier", "summary")})
            data["history"] = data["history"][-100:]
        self.save(data)
        return out

    # ------------------------------------------------------------------ veto de una compra
    def check_trade(self, candidate: dict) -> dict:
        """{'approve', 'confidence', 'reason'}; ante cualquier fallo, aprueba (el bot ya filtró la entrada)."""
        data = self.load()
        if not self.enabled or not self._budget_ok(data):
            return {"approve": True, "confidence": 0.0, "reason": "analista no disponible", "skipped": True}
        rv = data.get("review") or {}
        prompt = ("El bot quiere COMPRAR este token ahora mismo. Ya ha pasado sus filtros cuantitativos y anti-rug. "
                  "¿Lo apruebas? Veta solo si ves un motivo concreto (contexto de mercado, señales de manipulación, "
                  "subida insostenible, noticia negativa).\n\n" + json.dumps(
                      {"candidato": candidate, "tu_ultima_revision": {k: rv.get(k) for k in
                                                                     ("regime", "risk_multiplier", "summary",
                                                                      "avoid_tokens", "crypto_view")}},
                      ensure_ascii=False, default=str))
        try:
            out, resp = self._call(prompt, CHECK_SCHEMA, "low", False)
            self._account(data, resp, "veto")
        except Exception as e:  # noqa: BLE001
            log.warning("Consulta a Claude fallida: %s", e)
            return {"approve": True, "confidence": 0.0, "reason": f"error: {e}", "skipped": True}
        out = out or {"approve": True, "confidence": 0.0, "reason": "respuesta no válida"}
        dec = {"ts": int(time.time()), "mint": candidate.get("mint"), "pool": candidate.get("pool"),
               "symbol": candidate.get("symbol"), "price": candidate.get("price"), **out}
        data.setdefault("decisions", []).append(dec)
        data["decisions"] = data["decisions"][-300:]
        self.save(data)
        return out

    def record_outcomes(self, price_of) -> None:
        """Mide si los vetos acertaron: precio 2 h después frente al precio del veto."""
        data = self.load()
        changed = False
        for d in data.get("decisions", []):
            if d.get("approve") or "outcome" in d or time.time() - d["ts"] < 2 * 3600:
                continue
            p = price_of(d.get("mint"))
            if p and d.get("price"):
                d["outcome"] = round((p / d["price"] - 1) * 100, 2)
                changed = True
        if changed:
            vetos = [d for d in data["decisions"] if not d.get("approve") and "outcome" in d]
            good = [d for d in vetos if d["outcome"] < 0]
            data["track"] = {"vetos_medidos": len(vetos), "vetos_acertados": len(good),
                             "acierto_pct": round(100 * len(good) / len(vetos), 1) if vetos else None,
                             "media_tras_veto_pct": round(sum(d["outcome"] for d in vetos) / len(vetos), 2) if vetos else None}
            self.save(data)


def _read(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _r(v, n=1):
    try:
        return round(float(v), n)
    except (TypeError, ValueError):
        return None


def notify(text: str):
    import httpx
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if tok and chat:
        try:
            httpx.post(f"https://api.telegram.org/bot{tok}/sendMessage", json={"chat_id": chat, "text": text}, timeout=10)
        except httpx.HTTPError:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="state")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--loop", type=float, default=0, help="revisar cada N minutos (por defecto, el de config)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s analyst: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    from . import version
    from .config import load_config
    cfg = load_config(a.config).analyst
    an = Analyst(a.state, cfg.model, cfg.effort, cfg.web_search, cfg.web_search_max_uses, cfg.max_calls_per_day)
    if not cfg.enabled or not an.enabled:
        log.info("Analista Claude desactivado (falta ANTHROPIC_API_KEY en .env o analyst.enabled: false).")
        while a.loop or cfg.review_minutes:
            time.sleep(3600)
            if version.changed():
                return
        return
    period = (a.loop or cfg.review_minutes) * 60
    last_digest, last_regime = 0.0, None
    while True:
        try:
            r = an.review()
            if r:
                log.info("Revisión: %s · riesgo x%.2f · %s", r["regime"], r["risk_multiplier"], r["summary"][:120])
                if r["regime"] != last_regime or time.time() - last_digest > cfg.telegram_digest_hours * 3600:
                    notify(f"🤖 Analista Claude · mercado {r['regime']} · tamaño de posiciones x{r['risk_multiplier']:.2f}\n"
                           f"{r['summary']}")
                    last_digest, last_regime = time.time(), r["regime"]
        except Exception as e:  # noqa: BLE001
            log.warning("Revisión fallida: %s", e)
        end = time.time() + period
        while time.time() < end:
            if version.changed():
                return
            time.sleep(15)


if __name__ == "__main__":
    main()
