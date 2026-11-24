import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from bot.dashboard import make_handler
from research.demo_state import main as demo_main


def _serve(state, token=""):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state, token))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _req(url, data=None, headers=None):
    r = urllib.request.Request(url, data=json.dumps(data).encode() if data else None, headers=headers or {},
                               method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_state_api_and_controls(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.argv", ["x", "--out", str(tmp_path)])
    demo_main()
    httpd, base = _serve(tmp_path)
    try:
        code, html = _req(base + "/")
        assert code == 200 and b"MemeBot" in html
        code, body = _req(base + "/api/state")
        st = json.loads(body)
        assert st["online"] and st["stats"]["n"] == 22 and len(st["status"]["positions"]) == 3 and st["equity"]
        code, _ = _req(base + "/api/control", {"action": "pause"})
        assert code == 200 and json.loads((tmp_path / "control.json").read_text())["paused"] is True
        code, _ = _req(base + "/api/control", {"action": "close_all"})
        assert json.loads((tmp_path / "control.json").read_text())["close_all"] is True
        assert _req(base + "/api/control", {"action": "rm -rf"})[0] == 400
    finally:
        httpd.shutdown()


def test_token_required(tmp_path):
    httpd, base = _serve(tmp_path, token="secreto")
    try:
        assert _req(base + "/api/state")[0] == 401
        assert _req(base + "/api/state?token=secreto")[0] == 200
        assert _req(base + "/api/control", {"action": "pause"})[0] == 401
    finally:
        httpd.shutdown()
