"""Actualizador: aplica código nuevo sin tocar claves/datos, rechaza versiones rotas y pide INICIAR si cambian librerías."""
import io
import json
import zipfile

import httpx

from bot import updater


def make_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for path, content in files.items():
            z.writestr(f"danielcarbajo98-test-abc1234/memebot/{path}", content)
    return buf.getvalue()


def client(sha: str, zipdata: bytes) -> httpx.Client:
    def handler(req: httpx.Request):
        if req.url.path.endswith("/commits") and req.url.params.get("sha") == "claude/bold-johnson-evz8g1":
            return httpx.Response(200, json=[{"sha": sha}])
        if f"/zipball/{sha}" in req.url.path:
            return httpx.Response(200, content=zipdata)
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


def setup_work(tmp_path):
    w = tmp_path / "work"
    (w / "bot").mkdir(parents=True)
    (w / "bot" / "main.py").write_text("OLD")
    (w / "bot" / "obsoleto.py").write_text("borrar")
    (w / "research").mkdir()
    (w / "state").mkdir()
    (w / "state" / "portfolio.json").write_text('{"cash": 50}')
    (w / "data").mkdir()
    (w / "data" / "pools.json").write_text("{}")
    (w / ".env").write_text("WALLET_PRIVATE_KEY=secreto\n")
    (w / "config.yaml").write_text("mode: live\n")
    (w / "requirements.txt").write_text("httpx\n")
    (w / "Dockerfile").write_text("FROM x\n")
    (w / "docker-compose.yml").write_text("services: {}\n")
    return w


BASE = {"bot/main.py": "NEW", "research/collect.py": "R", "requirements.txt": "httpx\n",
        "Dockerfile": "FROM x\n", "docker-compose.yml": "services: {}\n", "README.md": "nuevo",
        ".env": "WALLET_PRIVATE_KEY=del_atacante\n", "config.yaml": "mode: paper\n"}


def test_applies_update_and_never_touches_secrets_or_data(tmp_path):
    w = setup_work(tmp_path)
    st = updater.check_once(w, "danielcarbajo98/test", "claude/bold-johnson-evz8g1", "t", test=False,
                            http=client("sha1", make_zip(BASE)))
    assert st["state"] == "actualizado"
    assert (w / "bot" / "main.py").read_text() == "NEW"
    assert not (w / "bot" / "obsoleto.py").exists()             # lo que ya no existe se borra
    assert (w / "bot" / "VERSION").read_text().strip() == "sha1"
    assert (w / ".env").read_text() == "WALLET_PRIVATE_KEY=secreto\n"   # jamás se sobrescriben
    assert (w / "config.yaml").read_text() == "mode: live\n"
    assert json.loads((w / "state" / "portfolio.json").read_text())["cash"] == 50
    assert (w / "data" / "pools.json").exists()
    assert not (w / ".update").exists()
    # segunda comprobación: al día
    st2 = updater.check_once(w, "danielcarbajo98/test", "claude/bold-johnson-evz8g1", "t", test=False,
                             http=client("sha1", make_zip(BASE)))
    assert st2["state"] == "al día"


def test_requirements_change_is_not_applied_half_way(tmp_path):
    w = setup_work(tmp_path)
    files = dict(BASE, **{"requirements.txt": "httpx\nnueva-libreria\n"})
    st = updater.check_once(w, "danielcarbajo98/test", "claude/bold-johnson-evz8g1", "t", test=False,
                            http=client("sha2", make_zip(files)))
    assert st["state"] == "requiere INICIAR" and "requirements.txt" in st["files"]
    assert (w / "bot" / "main.py").read_text() == "OLD"


def test_broken_version_is_rejected(tmp_path):
    w = setup_work(tmp_path)
    files = dict(BASE, **{"tests/test_roto.py": "def test_x():\n    assert False\n", "tests/__init__.py": ""})
    st = updater.check_once(w, "danielcarbajo98/test", "claude/bold-johnson-evz8g1", "t", test=True,
                            http=client("sha3", make_zip(files)))
    assert st["state"].startswith("versión rechazada")
    assert (w / "bot" / "main.py").read_text() == "OLD"


def test_zip_with_path_traversal_is_refused(tmp_path):
    w = setup_work(tmp_path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x/memebot/bot/main.py", "NEW")
        z.writestr("x/../../evil.txt", "malo")
    try:
        updater.check_once(w, "danielcarbajo98/test", "claude/bold-johnson-evz8g1", "t", test=False,
                           http=client("sha4", buf.getvalue()))
        raised = False
    except RuntimeError:
        raised = True
    assert raised and not (tmp_path / "evil.txt").exists()
