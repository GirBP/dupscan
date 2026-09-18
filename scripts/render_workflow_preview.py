#!/usr/bin/env python3
"""Render deterministic DupScan task states without scanning real files."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp(prefix="dupscan-v2-preview-"))

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QColor, QPalette  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import dupscan.ui.app as dupscan_app  # noqa: E402
import dupscan.domain.core as core  # noqa: E402
import dupscan.ui.workflow_ui as workflow_ui  # noqa: E402


def preview_palette(dark: bool) -> QPalette:
    value = QPalette()
    colors = (
        {
            QPalette.ColorRole.Window: "#161719",
            QPalette.ColorRole.WindowText: "#F1F3F5",
            QPalette.ColorRole.Base: "#202226",
            QPalette.ColorRole.AlternateBase: "#292C31",
            QPalette.ColorRole.Text: "#F1F3F5",
            QPalette.ColorRole.PlaceholderText: "#AEB3BD",
            QPalette.ColorRole.Button: "#292C31",
            QPalette.ColorRole.ButtonText: "#F1F3F5",
            QPalette.ColorRole.Mid: "#6B7280",
            QPalette.ColorRole.Midlight: "#34383F",
            QPalette.ColorRole.Highlight: "#3478F6",
            QPalette.ColorRole.HighlightedText: "#FFFFFF",
        }
        if dark else
        {
            QPalette.ColorRole.Window: "#F5F5F7",
            QPalette.ColorRole.WindowText: "#1D1D1F",
            QPalette.ColorRole.Base: "#FFFFFF",
            QPalette.ColorRole.AlternateBase: "#ECECF0",
            QPalette.ColorRole.Text: "#1D1D1F",
            QPalette.ColorRole.PlaceholderText: "#626268",
            QPalette.ColorRole.Button: "#FFFFFF",
            QPalette.ColorRole.ButtonText: "#1D1D1F",
            QPalette.ColorRole.Mid: "#A8A8AD",
            QPalette.ColorRole.Midlight: "#E3E3E8",
            QPalette.ColorRole.Highlight: "#0A64D8",
            QPalette.ColorRole.HighlightedText: "#FFFFFF",
        }
    )
    for role, color in colors.items():
        value.setColor(role, QColor(color))
    return value


def synthetic_result(*, live: bool = True) -> core.ScanResult:
    featured = [
        core.FileGroup(
            4_810_375_168,
            "photos",
            [
                "/Users/example/Pictures/Family Archive/originals.dat",
                "/Volumes/Backup SSD/Photos/Family Archive/originals.dat",
                "/Volumes/Studio NAS/Archive/Family/originals.dat",
            ],
        ),
        core.FileGroup(
            1_247_805_440,
            "video",
            [
                "/Users/example/Movies/Launch/final-master.mov",
                "/Users/example/Downloads/final-master (1).mov",
            ],
        ),
        core.FileGroup(
            328_204_288,
            "assets",
            [
                "/Users/example/Documents/Brand/Assets-2025.zip",
                "/Users/example/Desktop/Transfer/Assets-2025.zip",
            ],
        ),
    ]
    generated = [
        core.FileGroup(
            8_000_000 + index * 4096,
            f"generated-{index}",
            [
                f"/Users/example/Documents/Archive/{index:04d}/document.pdf",
                f"/Volumes/Backup SSD/Documents/{index:04d}/document.pdf",
            ],
        )
        for index in range(120)
    ]
    directories = [
        core.DirGroup(
            40_000_000 + index * 1024,
            12,
            [
                f"/Users/example/Projects/Project-{index:02d}",
                f"/Volumes/Studio NAS/Projects/Project-{index:02d}",
            ],
        )
        for index in range(18)
    ]
    pairs = [
        core.SimPair(
            f"/Users/example/Pictures/Collection-{index:02d}",
            f"/Volumes/Backup SSD/Pictures/Collection-{index:02d}",
            96.0 - index,
            420_000_000 - index * 1_000_000,
        )
        for index in range(7)
    ]
    return core.ScanResult(
        file_groups=featured + generated,
        dir_groups=directories,
        sim_pairs=pairs,
        files_seen=18_643,
        bytes_seen=1_934_281_744_384,
        live=live,
    )


def configure(window: dupscan_app.Main, state: str) -> None:
    for path in (
            "/Users/example/Pictures",
            "/Volumes/Backup SSD",
            "/Volumes/Studio NAS"):
        window.folders.add_dir(path)
    if state == "scan":
        window._show_task(workflow_ui.TASK_SCAN)
        return
    if state == "scanning":
        window.b_scan.setEnabled(False)
        window.b_pause.setEnabled(True)
        window.b_cancel.setEnabled(True)
        window.bar.setRange(0, 100)
        window.bar.setValue(43)
        window._set_operation_controls_visible(True)
        window.status.setText("Повна перевірка BLAKE3: 8 024 / 18 643")
        window._show_task(workflow_ui.TASK_SCAN)
        return
    if state == "compare":
        window._show_task(workflow_ui.TASK_COMPARE)
        return

    result = synthetic_result(live=state != "loaded")
    window.result = result
    window._refresh_models()
    window._sweeping = True
    window._show_task(workflow_ui.TASK_RESULTS)
    first_group = window.m_files.index(0, 0)
    window.v_files.setExpanded(first_group, True)
    first_copy = window.m_files.index(1, 0, first_group)
    window.v_files.setCurrentIndex(first_copy)
    window._inspector_buttons[window.m_files].setChecked(True)
    if state == "results":
        window.m_files.setData(first_copy, Qt.Checked, Qt.CheckStateRole)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output")
    parser.add_argument(
        "--state",
        choices=("scan", "scanning", "results", "loaded", "compare", "picker"),
        default="results")
    parser.add_argument("--dark", action="store_true")
    args = parser.parse_args()
    application = QApplication.instance() or QApplication([])
    application.setPalette(preview_palette(args.dark))
    if args.state == "picker":
        window = dupscan_app.SourcePickerDialog(
            title="Додати папки й диски",
            accept_label="Додати джерела",
        )
        window.resize(900, 720)
    else:
        window = dupscan_app.Main()
        window.resize(1440, 900)
        configure(window, args.state)
    window.show()
    if args.state == "picker":
        QTest.qWait(350)
    for _ in range(8):
        application.processEvents()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if not window.grab().save(str(output)):
        raise SystemExit(f"could not save {output}")
    window.deleteLater()


if __name__ == "__main__":
    main()
