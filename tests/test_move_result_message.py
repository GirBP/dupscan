"""Чесність підсумку часткового переносу.

Регресія: попап злиття завжди дописував «Решту перенесено», навіть коли
не перенеслось нічого, і не показував реальну кількість перенесених.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication  # noqa: E402

import dupscan.ui.app as app_mod  # noqa: E402

_qapp = QApplication.instance() or QApplication([])


def test_all_failed_does_not_claim_rest_transferred():
    msg = app_mod._move_result_message(0, ["x.pyc: [Errno 45]", "y", "z"])
    assert "Решту перенесено" not in msg
    assert "Жодного файла не перенесено" in msg
    assert "3 з помилкою" in msg
    assert "x.pyc: [Errno 45]" in msg


def test_partial_states_both_real_counts():
    msg = app_mod._move_result_message(7, ["a: err", "b: err"])
    assert "Перенесено 7 файл(ів), не перенесено 2." in msg
    assert "Решту перенесено" not in msg
    assert "a: err" in msg


def test_single_moved_single_failed():
    msg = app_mod._move_result_message(1, ["only: err"])
    assert "Перенесено 1 файл(ів), не перенесено 1." in msg
    assert "only: err" in msg
