"""Bounded and fail-closed automatic nested-root discovery."""

from __future__ import annotations

import os
import sys
import tempfile
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import QApplication  # noqa: E402

import dupscan.ui.app as app_mod  # noqa: E402

_qapp = QApplication.instance() or QApplication([])


def _sample(root, relative: str) -> str:
    return str(root / relative)


def test_unique_direct_child_with_multiple_matches_is_selected(tmp_path):
    root = tmp_path / "L"
    current = root / "BARRACUDA"
    other = root / "System Volume Information"
    (current / "Camera").mkdir(parents=True)
    (current / "Photos").mkdir()
    (current / "Camera" / "one.jpg").write_bytes(b"1")
    (current / "Photos" / "two.jpg").write_bytes(b"2")
    other.mkdir()
    samples = (
        _sample(root, "Camera/one.jpg"),
        _sample(root, "Photos/two.jpg"),
        _sample(root, "Missing/three.jpg"),
    )

    assert app_mod.detect_nested_session_root(str(root), samples) == (
        str(current), 2, 3)


def test_equal_score_candidates_are_ambiguous(tmp_path):
    root = tmp_path / "L"
    samples = (
        _sample(root, "Camera/one.jpg"),
        _sample(root, "Photos/two.jpg"),
    )
    for name in ("FIRST", "SECOND"):
        candidate = root / name
        (candidate / "Camera").mkdir(parents=True)
        (candidate / "Photos").mkdir()
        (candidate / "Camera" / "one.jpg").write_bytes(b"1")
        (candidate / "Photos" / "two.jpg").write_bytes(b"2")

    assert app_mod.detect_nested_session_root(str(root), samples) is None


def test_symlink_candidate_is_rejected(tmp_path):
    root = tmp_path / "L"
    real = tmp_path / "real"
    (real / "Camera").mkdir(parents=True)
    (real / "Camera" / "one.jpg").write_bytes(b"1")
    root.mkdir()
    alias = root / "BARRACUDA"
    try:
        alias.symlink_to(real, target_is_directory=True)
    except OSError as error:  # pragma: no cover - unusual filesystem policy
        pytest.skip(f"symlink unavailable: {error}")

    assert app_mod.detect_nested_session_root(
        str(root), (_sample(root, "Camera/one.jpg"),)) is None


def test_child_limit_fails_closed_without_partial_decision(tmp_path):
    root = tmp_path / "L"
    root.mkdir()
    for index in range(129):
        (root / f"child-{index:03d}").mkdir()
    valid = root / "child-000" / "Camera"
    valid.mkdir()
    (valid / "one.jpg").write_bytes(b"1")

    assert app_mod.detect_nested_session_root(
        str(root), (_sample(root, "Camera/one.jpg"),),
        max_children=128) is None


def test_multiple_samples_require_more_than_one_match(tmp_path):
    root = tmp_path / "L"
    candidate = root / "BARRACUDA"
    (candidate / "Camera").mkdir(parents=True)
    (candidate / "Camera" / "one.jpg").write_bytes(b"1")
    samples = (
        _sample(root, "Camera/one.jpg"),
        _sample(root, "Photos/two.jpg"),
    )

    assert app_mod.detect_nested_session_root(str(root), samples) is None


def test_manual_fallback_has_explicit_ukrainian_actions(tmp_path):
    main = app_mod.Main()
    box, choose = main._refresh_root_choice_box(
        "/Volumes/L", "/Volumes/L")
    labels = {button.text() for button in box.buttons()}

    assert choose.text() == "Обрати поточну теку…"
    assert "Скасувати оновлення" in labels
    assert "Yes" not in labels and "No" not in labels
    assert "файли не змінювались" in box.text()
    assert box.text().count("/Volumes/L") == 1
    box.close()
    main.close()


def test_refresh_confirmation_has_explicit_ukrainian_actions():
    main = app_mod.Main()
    box, refresh = main._session_refresh_confirmation_box(("/Volumes/L",))
    labels = {button.text() for button in box.buttons()}

    assert refresh.text() == "Оновити дані"
    assert "Залишити історичний перегляд" in labels
    assert "Yes" not in labels and "No" not in labels
    assert "/Volumes/L" in box.text()
    box.close()
    main.close()


def test_history_load_choice_names_the_complete_workflow():
    main = app_mod.Main()
    box, refresh, view = main._session_load_choice_box({
        "roots": ["/Volumes/L"],
    })
    labels = {button.text() for button in box.buttons()}

    assert refresh.text() == "Інтелектуальний рескан і працювати"
    assert view.text() == "Лише переглянути знімок"
    assert "Скасувати" in labels
    assert "Yes" not in labels and "No" not in labels
    assert box.defaultButton() is refresh
    assert "Старий файл сесії не зміниться" in box.text()
    box.close()
    main.close()


def test_wrong_type_and_wrong_size_are_not_root_evidence(tmp_path):
    root = tmp_path / "L"
    sample = _sample(root, "Camera/one.jpg")
    specs = {sample: ("file", 2)}

    wrong_size = root / "WRONG-SIZE" / "Camera"
    wrong_size.mkdir(parents=True)
    (wrong_size / "one.jpg").write_bytes(b"1")
    wrong_type = root / "WRONG-TYPE" / "Camera" / "one.jpg"
    wrong_type.mkdir(parents=True)

    assert app_mod.detect_nested_session_root(
        str(root), (sample,), probe_specs=specs) is None


def test_near_tie_two_matches_against_one_fails_closed(tmp_path):
    root = tmp_path / "L"
    samples = tuple(
        _sample(root, f"Camera/{name}.jpg")
        for name in ("one", "two", "three")
    )
    specs = {path: ("file", 1) for path in samples}
    best = root / "BARRACUDA" / "Camera"
    runner = root / "OTHER" / "Camera"
    best.mkdir(parents=True)
    runner.mkdir(parents=True)
    for name in ("one", "two"):
        (best / f"{name}.jpg").write_bytes(b"x")
    (runner / "one.jpg").write_bytes(b"x")

    assert app_mod.detect_nested_session_root(
        str(root), samples, probe_specs=specs) is None


def test_three_typed_matches_against_zero_selects_root(tmp_path):
    root = tmp_path / "L"
    samples = tuple(
        _sample(root, f"Camera/{index}.jpg") for index in range(8)
    )
    specs = {path: ("file", 1) for path in samples}
    current = root / "BARRACUDA" / "Camera"
    empty = root / "System Volume Information"
    current.mkdir(parents=True)
    empty.mkdir(parents=True)
    for index in range(3):
        (current / f"{index}.jpg").write_bytes(b"x")

    assert app_mod.detect_nested_session_root(
        str(root), samples, probe_specs=specs) == (
            str(root / "BARRACUDA"), 3, 8)


def test_cancelled_detector_does_not_probe_disk(monkeypatch, tmp_path):
    cancel = threading.Event()
    cancel.set()
    probes = []
    monkeypatch.setattr(
        app_mod.os, "scandir",
        lambda *_args: probes.append(True))

    assert app_mod.detect_nested_session_root(
        str(tmp_path), (str(tmp_path / "one"),),
        cancel=cancel) is None
    assert probes == []
