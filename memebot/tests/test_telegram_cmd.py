import json

import httpx

from bot import telegram_cmd as tc


def test_commands_only_from_own_chat(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    sent, ups = [], []

    def handler(req: httpx.Request):
        if req.url.path.endswith("/getUpdates"):
            return httpx.Response(200, json={"result": ups})
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"ok": True})
    http = httpx.Client(transport=httpx.MockTransport(handler))
    memo = {}
    ups[:] = [{"update_id": 1, "message": {"chat": {"id": 42}, "text": "/pausa"}}]
    assert tc.poll(tmp_path, memo, http) == 0            # mensajes antiguos al arrancar: se ignoran
    ups[:] = [{"update_id": 2, "message": {"chat": {"id": 42}, "text": "/pausa"}},
              {"update_id": 3, "message": {"chat": {"id": 666}, "text": "/cerrar_todo SI"}}]
    assert tc.poll(tmp_path, memo, http) == 1
    ctl = json.loads((tmp_path / "control.json").read_text())
    assert ctl["paused"] is True and "close_all" not in ctl   # el desconocido no puede cerrar nada
    assert memo["tg_offset"] == 4 and "pausa" in sent[0]["text"].lower()


def test_close_all_needs_confirmation(tmp_path):
    assert "SI" in tc.answer(tmp_path, "/cerrar_todo")
    assert not (tmp_path / "control.json").exists()
    tc.answer(tmp_path, "/cerrar_todo si")
    assert json.loads((tmp_path / "control.json").read_text())["close_all"] is True
    assert tc.answer(tmp_path, "hola") is None and "Comandos" in tc.answer(tmp_path, "/ayuda")
    assert "ESTADO" in tc.answer(tmp_path, "/estado")


def test_positions_show_return_and_profit(tmp_path):
    import time
    (tmp_path / "status.json").write_text(json.dumps({"positions": [
        {"symbol": "4CHAN", "pnl_usd": -12.5, "pnl_pct": -6.25, "cost": 200, "value": 187.5, "opened_at": time.time() - 3600}]}))
    (tmp_path / "stocks.json").write_text(json.dumps({"positions": {
        "SAP.DE": {"entry": 100, "price": 105, "amount": 500, "fees_eur": 0.0}}}))
    t = tc.answer(tmp_path, "/posiciones")
    assert "4CHAN: -6.2% · −$12.50" in t and "SAP.DE: +5.0% · +25.00 €" in t and "1 h 0 min" in t
