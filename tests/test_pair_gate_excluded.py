"""Гейт пари (directory_tree_mergeable) не має відмовляти на навмисних
виключеннях (EXCLUDE_NAMES-теки типу __pycache__/.git), лише на
справжніх збоях читання (dir_read_failed).

Симптом власника: злиття двох тек на BARRACUDA відмовляло діалогом «Не всі
елементи у вибраних теках вдалося прочитати…», бо службова тека __pycache__
у B ставила dir_ok[батька]=False без жодного справжнього збою, а гейт пари
(directory_tree_complete) вимагав dir_ok is True для всього піддерева.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.domain.core as core  # noqa: E402
import dupscan.infra.session as session  # noqa: E402
import dupscan.ui.app as app_mod  # noqa: E402


def _make(path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_mergeable_true_but_complete_false_for_excluded_service_dirs(tmp_path):
    dir_a = tmp_path / "A"
    dir_b = tmp_path / "B"
    _make(dir_a / "dup.bin", b"shared" * 900)
    _make(dir_b / "dup.bin", b"shared" * 900)
    # службові теки — навмисне виключення (EXCLUDE_NAMES), не збій:
    _make(dir_b / "__pycache__" / "x.pyc", b"cache-bytes")
    _make(dir_b / ".git" / "config", b"[core]\n")

    result = core.scan([str(dir_a), str(dir_b)])

    assert core.directory_tree_mergeable(result, str(dir_a))
    assert core.directory_tree_mergeable(result, str(dir_b))
    # directory_tree_complete лишається суворим — саме ним користуються
    # Merkle-докази точних копій тек, і його поведінку МИ НЕ послаблюємо.
    assert not core.directory_tree_complete(result, str(dir_b))


def test_pair_verification_worker_reports_mergeable_for_excluded_dirs(tmp_path):
    dir_a = tmp_path / "A"
    dir_b = tmp_path / "B"
    _make(dir_a / "dup.bin", b"shared" * 900)
    _make(dir_b / "dup.bin", b"shared" * 900)
    _make(dir_b / "__pycache__" / "x.pyc", b"cache-bytes")
    _make(dir_b / ".git" / "config", b"[core]\n")

    worker = app_mod.PairVerificationWorker(str(dir_a), str(dir_b))
    results = []
    worker.done.connect(results.append)
    errors = []
    worker.failed.connect(errors.append)

    worker.run()

    assert not errors
    assert len(results) == 1
    fresh = results[0]
    assert not fresh.partial
    assert core.directory_tree_mergeable(fresh, str(dir_a))
    assert core.directory_tree_mergeable(fresh, str(dir_b))
    # Доказ регресії: старий гейт (directory_tree_complete) саме тут і
    # відмовляв власнику — dir_b містить службові EXCLUDE_NAMES-теки.
    assert not core.directory_tree_complete(fresh, str(dir_b))


def test_mergeable_false_for_a_genuine_read_failure(tmp_path):
    dir_a = tmp_path / "A"
    dir_b = tmp_path / "B"
    _make(dir_a / "dup.bin", b"shared" * 900)
    _make(dir_b / "dup.bin", b"shared" * 900)
    secret = dir_b / "secret"
    _make(secret / "inner.bin", b"S" * 500)
    os.chmod(secret, 0o000)
    try:
        result = core.scan([str(dir_a), str(dir_b)])
        assert not core.directory_tree_mergeable(result, str(dir_b))
        assert not core.directory_tree_complete(result, str(dir_b))
        assert result.errors  # справжній збій зафіксовано, а не проковтнутий
    finally:
        os.chmod(secret, 0o755)


def test_dir_read_failed_round_trips_through_session(tmp_path):
    dir_a = tmp_path / "A"
    dir_b = tmp_path / "B"
    _make(dir_a / "dup.bin", b"shared" * 900)
    _make(dir_b / "dup.bin", b"shared" * 900)
    _make(dir_b / "__pycache__" / "x.pyc", b"cache-bytes")
    dir_b.mkdir(parents=True, exist_ok=True)
    # Непідтримуваний тип запису (FIFO) — справжня деградація читання, не
    # виключення: dir_b мусить потрапити в dir_read_failed (core.py: не-REG
    # запис при sequential-обході).
    os.mkfifo(dir_b / "pipe")

    result = core.scan([str(dir_a), str(dir_b)])
    assert result.dir_read_failed  # маємо хоч один справжній збій
    assert str(dir_b) in result.dir_read_failed
    assert not core.directory_tree_mergeable(result, str(dir_b))

    saved = session.save_session(
        result, [str(dir_a), str(dir_b)], base_dir=str(tmp_path / "data"))
    assert saved
    loaded = session.load_session(saved)

    assert loaded.dir_read_failed == result.dir_read_failed
    assert not core.directory_tree_mergeable(loaded, str(dir_b))


def test_old_v3_session_without_dir_read_failed_section_loads(tmp_path):
    dir_a = tmp_path / "A"
    dir_b = tmp_path / "B"
    _make(dir_a / "dup.bin", b"shared" * 900)
    _make(dir_b / "dup.bin", b"shared" * 900)
    result = core.scan([str(dir_a), str(dir_b)])
    saved = session.save_session(
        result, [str(dir_a), str(dir_b)], base_dir=str(tmp_path / "data"))
    assert saved

    # Емулюємо стару сесію v3 без розділу dir_read_failed: прибираємо
    # його з payload вручну (лишається валідний v3 JSON).
    import gzip
    import json

    with gzip.open(saved, "rt", encoding="utf-8") as fh:
        payload = json.load(fh)
    assert "dir_read_failed" in payload["state"]
    del payload["state"]["dir_read_failed"]
    with gzip.open(saved, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh)

    loaded = session.load_session(saved)
    assert loaded.dir_read_failed == set()
