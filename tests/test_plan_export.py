"""Повний dry-run звіт.

Прев'ю діалогу злиття показує 15 рядків; для 18-гігабайтних злиттів це
іграшка (ринковий аналіз: Beyond Compare/rsync-світ дають повний
переглядний план). Тут: (1) reports.write_merge_plan_csv — ПОВНИЙ план у
CSV; (2) app._export_merge_plan — вибір файла + фоновий запис,
best-effort: відмова запису не ламає злиття; (3) гейт Кошика віддає
СПИСОК недоведених шляхів (unproven_out), а не лише кількість.
"""

import csv
import os
import sys
import tempfile

os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication, QFileDialog  # noqa: E402

import dupscan.ui.app as app_mod  # noqa: E402
import dupscan.domain.core as core  # noqa: E402
import dupscan.infra.fsops as fsops  # noqa: E402
import dupscan.infra.reports as reports  # noqa: E402

_qapp = QApplication.instance() or QApplication([])


def _plan_rows(tmp_path, count: int):
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    rows = []
    for i in range(count):
        p = src / f"f{i:04d}.bin"
        p.write_bytes(b"x" * (i + 1))
        rows.append((i + 1, str(p), f"f{i:04d}.bin"))
    return str(src), rows


def test_csv_contains_every_plan_row_not_a_preview(tmp_path):
    src_dir, plan = _plan_rows(tmp_path, 40)  # > 15 прев'ю-рядків
    out = str(tmp_path / "plan.csv")

    reports.write_merge_plan_csv(out, plan, src_dir, str(tmp_path / "dst"))

    with open(out, newline="") as fh:
        rows = list(csv.reader(fh))
    assert len(rows) == 41  # заголовок + УСІ 40
    assert rows[0][:3] == ["розмір", "джерело", "ціль"]
    assert rows[1][1] == plan[0][1]
    assert rows[-1][2] == os.path.join(str(tmp_path / "dst"), "f0039.bin")


def test_csv_marks_symlink_rows(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    real = src / "real.bin"
    real.write_bytes(b"data")
    link = src / "link"
    link.symlink_to(real)
    plan = [(4, str(real), "real.bin"), (0, str(link), "link")]
    out = str(tmp_path / "plan.csv")

    reports.write_merge_plan_csv(out, plan, str(src), str(tmp_path / "dst"))

    with open(out, newline="") as fh:
        by_source = {row[1]: row[3] for row in list(csv.reader(fh))[1:]}
    assert by_source[str(real)] == ""
    assert by_source[str(link)] == "symlink"


def test_export_runs_in_background_and_reports_path(tmp_path, monkeypatch):
    main = app_mod.Main()
    src_dir, plan = _plan_rows(tmp_path, 20)
    out = str(tmp_path / "звіт.csv")
    ran_in_bg = []
    monkeypatch.setattr(
        main, "_bg_run",
        lambda fn, ok, err=None: (ran_in_bg.append(True), ok(fn()))[-1])
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName",
        staticmethod(lambda *a, **k: (out, "CSV (*.csv)")))

    main._export_merge_plan(plan, src_dir, str(tmp_path / "dst"))

    assert ran_in_bg, "запис мусив піти через фоновий раннер, не GUI-потік"
    assert os.path.exists(out)
    assert out in main.status.text()
    main.close()


def test_export_failure_is_best_effort_not_a_crash(tmp_path, monkeypatch):
    main = app_mod.Main()
    src_dir, plan = _plan_rows(tmp_path, 3)
    missing_dir_target = str(tmp_path / "no-such-dir" / "план.csv")
    monkeypatch.setattr(
        main, "_bg_run",
        lambda fn, ok, err=None: err(str(_raise_of(fn))) if err else None)
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName",
        staticmethod(lambda *a, **k: (missing_dir_target, "CSV (*.csv)")))

    main._export_merge_plan(plan, src_dir, str(tmp_path / "dst"))  # не кидає

    assert "Не вдалося зберегти" in main.status.text()
    main.close()


def _raise_of(fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 — тест збирає текст помилки
        return exc
    raise AssertionError("очікувалась помилка запису")


def test_export_cancelled_dialog_writes_nothing(tmp_path, monkeypatch):
    main = app_mod.Main()
    src_dir, plan = _plan_rows(tmp_path, 3)
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName",
        staticmethod(lambda *a, **k: ("", "")))
    wrote = []
    monkeypatch.setattr(
        main, "_bg_run", lambda fn, ok, err=None: wrote.append(True))

    main._export_merge_plan(plan, src_dir, str(tmp_path / "dst"))

    assert wrote == []
    main.close()


def test_trash_gate_reports_unproven_paths_not_only_a_count(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    lone = source / "унікальний-без-копії.bin"
    lone.write_bytes(b"irreplaceable")
    result = core.scan([str(source), str(target)])
    unproven: list[str] = []

    kind, payload = fsops._verify_then_trash_dir(
        result, str(source),
        trash=lambda paths, **_kw: [],
        unproven_out=unproven,
    )

    assert kind == "abort"
    assert payload == 1  # форма сумісна зі старими викликами
    assert unproven == [str(lone)]


def test_directory_trash_worker_exposes_unproven_paths(tmp_path, monkeypatch):
    """UI-шар бере список недоведених з воркера: голе число «3 файл(ів) без
    живої копії» власнику ні про що — треба ЯКІ саме (біль першого дня:
    «20 шляхів не прочитано»)."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "lone.bin").write_bytes(b"no-copy-anywhere")
    result = core.scan([str(source)])
    monkeypatch.setattr(app_mod, "to_trash", lambda paths, **_kw: [])
    worker = app_mod.DirectoryTrashWorker(result, str(source))

    worker.run()

    assert worker.unproven == [str(source / "lone.bin")]
