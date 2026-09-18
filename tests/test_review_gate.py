"""Обхід ReviewDialog має діяти лише під pytest.

`_review_selection` пропускав модальний ReviewDialog за самою умовою
QT_QPA_PLATFORM=offscreen — а це змінна середовища, яку можна виставити
і в зібраному .app (не лише в тестах). Людський гейт перед Кошиком
вимикався одним `export`. Обхід тепер вимагає ДВОХ незалежних ознак:
offscreen-платформа І `"pytest" in sys.modules` — друге неможливо
підробити ззовні зібраного застосунку.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

import dupscan.ui.app as app  # noqa: E402
import dupscan.domain.core as core  # noqa: E402

_qapp = QApplication.instance() or QApplication([])


def _make(path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _scan_with_duplicate(tmp_path):
    _make(tmp_path / "A" / "same.bin", b"review-gate-duplicate")
    _make(tmp_path / "B" / "same.bin", b"review-gate-duplicate")
    return core.scan([str(tmp_path)])


# ---- (в) юніт на сам helper -------------------------------------------------


def test_helper_true_only_with_both_offscreen_and_pytest(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    assert app._review_bypass_allowed(sys.modules) is True


def test_helper_false_without_pytest_in_modules(monkeypatch):
    """Підроблена modules-мапа без 'pytest' — саме вектор, який до фіксу
    міг обійти гейт у зібраному .app (там 'pytest' у sys.modules немає)."""
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    fake_modules = {k: v for k, v in sys.modules.items() if k != "pytest"}
    assert "pytest" not in fake_modules
    assert app._review_bypass_allowed(fake_modules) is False


def test_helper_false_without_offscreen(monkeypatch):
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    assert app._review_bypass_allowed(sys.modules) is False


# ---- (а) наявна поведінка тестів жива ---------------------------------------


def test_pytest_offscreen_bypass_still_works(tmp_path):
    result = _scan_with_duplicate(tmp_path)
    main = app.Main()
    main.result = result
    groups = [list(g.paths) for g in result.file_groups]
    selected = {result.file_groups[0].paths[0]}
    out = main._review_selection(set(selected), groups)
    assert out == selected
    main.close()


# ---- (б) обхід вимкнено -> діалог МУСИТЬ бути створений ---------------------


def test_bypass_disabled_forces_real_review_dialog(monkeypatch, tmp_path):
    result = _scan_with_duplicate(tmp_path)
    main = app.Main()
    main.result = result
    monkeypatch.setattr(app, "_review_bypass_allowed", lambda: False)

    created: list[dict] = []

    class FakeReviewDialog:
        def __init__(self, parent, entries, selected, relevant,
                     action_text="Продовжити перевірку"):
            created.append({
                "parent": parent, "entries": entries,
                "selected": selected, "relevant": relevant,
            })
            self.selected_paths = set(selected)

        def exec(self):
            return QDialog.Rejected

    monkeypatch.setattr(app, "ReviewDialog", FakeReviewDialog)

    groups = [list(g.paths) for g in result.file_groups]
    selected = {result.file_groups[0].paths[0]}
    out = main._review_selection(set(selected), groups)

    assert len(created) == 1, "ReviewDialog мусить бути інстанційований рівно раз"
    assert out is None, "Reject у діалозі -> None (нічого не видаляти)"
    main.close()
