from bot.bootstrap import ensure_deps


def test_installs_only_when_requirements_change_and_only_once(tmp_path):
    live, baked, deps = tmp_path / "live.txt", tmp_path / "baked.txt", tmp_path / "deps"
    baked.write_text("httpx\n")
    live.write_text("httpx\n")
    calls = []
    inst = lambda cmd, env: calls.append(cmd) or True  # noqa: E731
    assert ensure_deps(live, baked, deps, inst) == "al día" and not calls       # igual que la imagen
    live.write_text("httpx\nanthropic\n")
    assert ensure_deps(live, baked, deps, inst) == "instalado" and len(calls) == 1
    assert "--user" in calls[0] and str(live) in calls[0]
    assert ensure_deps(live, baked, deps, inst) == "al día" and len(calls) == 1  # no repite


def test_failed_install_is_retried_next_start(tmp_path):
    live, baked, deps = tmp_path / "live.txt", tmp_path / "baked.txt", tmp_path / "deps"
    baked.write_text("a\n")
    live.write_text("a\nb\n")
    assert ensure_deps(live, baked, deps, lambda c, e: False) == "error"
    assert ensure_deps(live, baked, deps, lambda c, e: True) == "instalado"
