import base64
import json
from datetime import datetime, timezone

import httpx

from bot import reports


def gh_mock(store, status=200):
    def handler(req: httpx.Request):
        if status != 200:
            return httpx.Response(status)
        path = req.url.path
        if path.endswith("/git/ref/heads/bot-reports"):
            return httpx.Response(200 if store.get("branch") else 404, json={"object": {"sha": "b"}})
        if "/git/ref/heads/" in path:
            return httpx.Response(200, json={"object": {"sha": "base"}})
        if path.endswith("/git/refs") and req.method == "POST":
            store["branch"] = True
            return httpx.Response(201, json={})
        if "/contents/" in path:
            if req.method == "GET":
                return httpx.Response(404)
            store[path.split("/contents/")[1]] = json.loads(base64.b64decode(json.loads(req.content)["content"]))
            return httpx.Response(201, json={})
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_report_has_results_but_no_secrets(tmp_path):
    (tmp_path / "status.json").write_text(json.dumps({"ts": 1, "equity": 1000, "mode": "paper",
                                                      "wallet": "SECRETO", "positions": []}))
    rep = reports.build(tmp_path)
    assert rep["crypto"]["status"]["equity"] == 1000 and "SECRETO" not in json.dumps(rep)


def test_publishes_once_a_day_after_21(tmp_path):
    store, sent = {}, []
    (tmp_path / "status.json").write_text(json.dumps({"ts": 1, "equity": 1000}))
    at = datetime(2026, 1, 15, 20, 30, tzinfo=timezone.utc).timestamp()        # 21:30 en Madrid
    http = gh_mock(store)
    assert reports.maybe_publish(tmp_path, "o/r", "main", "tok", sent.append, at, http)
    assert store["branch"] and store["reports/latest.json"]["crypto"]["status"]["equity"] == 1000
    assert "reports/2026-01-15.json" in store
    assert not reports.maybe_publish(tmp_path, "o/r", "main", "tok", sent.append, at + 60, http)


def test_warns_once_if_token_cannot_write(tmp_path):
    sent = []
    at = datetime(2026, 1, 15, 20, 30, tzinfo=timezone.utc).timestamp()
    assert not reports.maybe_publish(tmp_path, "o/r", "main", "tok", sent.append, at, gh_mock({}, 403))
    assert len(sent) == 1 and "Read and write" in sent[0]


def test_first_report_goes_out_immediately(tmp_path):
    store, sent = {}, []
    at = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc).timestamp()          # 10:00 en Madrid
    http = gh_mock(store)
    assert reports.maybe_publish(tmp_path, "o/r", "main", "tok", sent.append, at, http)
    assert "reports/latest.json" in store and "Primer informe" in sent[0]
    assert not reports.maybe_publish(tmp_path, "o/r", "main", "tok", sent.append, at + 60, http)   # el resto, a las 21:00
