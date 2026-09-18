"""Регресія 2.17.0: у PyInstaller-збірці app.py виконується як __main__.

Модуля "dupscan.ui.app" у sys.modules тоді НЕ існує, і воркери, які читають точки
перехоплення пізнім lookup-ом (_app_hooks), мусять знайти той самий
модуль під "__main__" — інакше злиття і Кошик теки падають з KeyError
'app' («Помилка плану злиття: 'app'») лише у зібраному застосунку.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dupscan.ui.app as app_mod  # noqa: E402
import dupscan.domain.core as core  # noqa: E402
import dupscan.infra.fsops as fsops  # noqa: E402
import dupscan.ui.workers as workers  # noqa: E402


def _make(path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_app_hooks_resolve_when_app_runs_as_main(monkeypatch):
    """Збірковий сценарій: немає sys.modules["dupscan.ui.app"], є лише __main__."""
    monkeypatch.delitem(sys.modules, "dupscan.ui.app")
    monkeypatch.setitem(sys.modules, "__main__", app_mod)
    hooks = workers._app_hooks()
    assert hooks is app_mod
    assert callable(hooks._same_device)
    assert callable(hooks._verify_then_trash_dir)


def test_app_hooks_fail_safe_falls_back_to_fsops(monkeypatch):
    """Якщо ні app, ні __main__ не мають хуків — безпечні fsops-дефолти
    (та сама логіка, лише без можливості підміни), а не KeyError."""
    monkeypatch.delitem(sys.modules, "dupscan.ui.app")

    class _Alien:
        pass

    monkeypatch.setitem(sys.modules, "__main__", _Alien())
    hooks = workers._app_hooks()
    assert hooks is fsops
    assert callable(hooks._same_device)


def test_app_hooks_prefer_app_module_under_pytest():
    """Звичайний тестовий сценарій: import app існує — хуки з app."""
    assert workers._app_hooks() is app_mod


# ---- Task C: конкретні app-обгортки на воркерних шляхах, збірковий сценарій


def test_directory_trash_worker_run_under_frozen_entry(tmp_path, monkeypatch):
    """DirectoryTrashWorker кличе _verify_then_trash_dir через _app_hooks()
    пізнім lookup-ом — цей шлях мусить пережити відсутність
    dupscan.ui.app у sys.modules (frozen-точка входу, коли app.py
    виконується як __main__; див. _app_hooks). Тест прогоняє реальний
    клас воркера, не лише ізольований _app_hooks()."""
    monkeypatch.delitem(sys.modules, "dupscan.ui.app")
    monkeypatch.setitem(sys.modules, "__main__", app_mod)

    source = tmp_path / "source"
    _make(source / "same.bin", b"same")
    snapshot = ("same", 4, 1)
    monkeypatch.setattr(
        app_mod.core, "snapshot_directory_state",
        lambda _path, **_kwargs: snapshot)
    monkeypatch.setattr(
        app_mod, "_verified_survivor",
        lambda *_args, **_kwargs: os.lstat(source / "same.bin"))
    captured = {}

    def capture(paths, **kwargs):
        captured["paths"] = paths
        captured.update(kwargs)
        return []

    monkeypatch.setattr(app_mod, "to_trash", capture)

    worker = workers.DirectoryTrashWorker(core.ScanResult(), str(source))
    results = []
    errors = []
    worker.done.connect(results.append)
    worker.failed.connect(errors.append)

    worker.run()

    assert not errors, f"воркер не мусив впасти: {errors}"
    assert results == [("ok", [])]
    assert captured["paths"] == [str(source)]


def test_to_trash_known_direct_call_under_frozen_entry(tmp_path, monkeypatch):
    """_to_trash_known — виклик через шлях app._sim_merge/_run_sweep (не
    через _app_hooks(), пряме module-level ім'я to_trash), теж не сміє
    впасти KeyError у зібраному сценарії."""
    monkeypatch.delitem(sys.modules, "dupscan.ui.app")
    monkeypatch.setitem(sys.modules, "__main__", app_mod)

    data = os.urandom(4096)
    _make(tmp_path / "t/A/f.bin", data)
    _make(tmp_path / "t/B/f.bin", data)
    r = core.scan([str(tmp_path / "t")])
    victim = r.file_groups[0].paths[0]
    trashed = []
    monkeypatch.setattr(
        app_mod, "to_trash",
        lambda paths, **_kwargs: (trashed.extend(paths), [])[1])

    errors = app_mod._to_trash_known(r, [victim])

    assert errors == []
    assert trashed == [victim]


def test_verified_to_trash_files_direct_call_under_frozen_entry(tmp_path, monkeypatch):
    """_verified_to_trash_files — виклик через app._bg_run(lambda: …) з
    Main-методів (Bg QThread, не _app_hooks()); той самий фрозен-сценарій."""
    monkeypatch.delitem(sys.modules, "dupscan.ui.app")
    monkeypatch.setitem(sys.modules, "__main__", app_mod)

    data = os.urandom(4096)
    _make(tmp_path / "t/A/f.bin", data)
    _make(tmp_path / "t/B/f.bin", data)
    r = core.scan([str(tmp_path / "t")])
    victim = r.file_groups[0].paths[0]
    trashed = []
    monkeypatch.setattr(
        app_mod, "to_trash",
        lambda paths, **_kwargs: (trashed.extend(paths), [])[1])

    errors = app_mod._verified_to_trash_files(r, [victim])

    assert errors == []
    assert trashed == [victim]
