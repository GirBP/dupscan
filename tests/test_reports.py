import csv
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.infra.reports as reports


def result_fixture():
    return SimpleNamespace(
        file_groups=[
            SimpleNamespace(
                size=100,
                digest="a" * 64,
                paths=["/scan/photo.JPG", "/scan/copy/photo.JPG"],
            ),
            SimpleNamespace(
                size=25,
                digest="=HYPERLINK(\"bad\")",
                paths=["=2+3", "</td><script>alert(1)</script>.pdf"],
            ),
        ],
        dir_groups=[SimpleNamespace(size=300, paths=["/scan/A", "/scan/B"])],
        sim_pairs=[
            SimpleNamespace(dir_a="/scan/A", dir_b="/scan/C", percent=82.5, shared_bytes=240)
        ],
        errors=["Denied <img src=x onerror=alert(2)>"],
    )


def test_categories_cover_common_media_and_unknown_types():
    assert reports.categorize_path("x.HeIc") == "image"
    assert reports.categorize_path("x.MOV") == "video"
    assert reports.categorize_path("x.flac") == "audio"
    assert reports.categorize_path("x.pdf") == "document"
    assert reports.categorize_path("x.tar") == "archive"
    assert reports.categorize_path("x.app") == "application"
    assert reports.categorize_path("x.unknown") == "other"
    assert reports.categorize_path("anything", directory=True) == "folder"


def test_summary_caps_reclaimable_selection_to_leave_a_survivor():
    result = result_fixture()
    selected = {"/scan/photo.JPG", "/scan/copy/photo.JPG", "/scan/A"}
    summary = reports.summarize_result(result, selected)
    assert summary["exact_file_groups"] == 2
    assert summary["exact_file_items"] == 4
    assert summary["directory_groups"] == 1
    assert summary["directory_items"] == 2
    assert summary["similar_pairs"] == 1
    assert summary["file_reclaimable_bytes"] == 125
    assert summary["directory_reclaimable_bytes"] == 300
    assert summary["selected_items"] == 3
    assert summary["selected_bytes"] == 500
    assert summary["selected_reclaimable_bytes"] == 400
    assert summary["categories"]["image"]["items"] == 2
    assert summary["categories"]["folder"]["selected_items"] == 1


def test_csv_is_streamed_atomically_and_neutralizes_formulas(tmp_path):
    destination = tmp_path / "report.csv"
    summary = reports.export_csv(
        str(destination), result_fixture(), selected_paths=["=2+3"]
    )
    assert summary["selected_reclaimable_bytes"] == 25
    with destination.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 7
    hostile = next(row for row in rows if row["group"] == "F2" and row["selected"] == "True")
    assert hostile["path"] == "'=2+3"
    assert hostile["digest"].startswith("'=")
    assert not list(tmp_path.glob(".dupscan-report-*.tmp"))
    assert oct(os.stat(destination).st_mode & 0o777) == "0o600"


def test_html_escapes_all_hostile_content_and_has_no_executable_code(tmp_path):
    destination = tmp_path / "report.html"
    title = "<img src=x onerror=alert(9)>"
    reports.export_html(
        str(destination),
        result_fixture(),
        selected_paths=["</td><script>alert(1)</script>.pdf"],
        title=title,
        roots=["/scan/<unsafe>"],
        generated_ns=1_700_000_000_000_000_000,
    )
    document = destination.read_text(encoding="utf-8")
    assert "<!doctype html>" in document
    assert "&lt;img src=x onerror=alert(9)&gt;" in document
    assert "&lt;/td&gt;&lt;script&gt;alert(1)&lt;/script&gt;.pdf" in document
    assert "Denied &lt;img src=x onerror=alert(2)&gt;" in document
    assert "<script" not in document.casefold()
    assert "<img" not in document.casefold()
    assert "http://" not in document and "https://" not in document


def test_failed_midstream_export_preserves_existing_destination(tmp_path, monkeypatch):
    destination = tmp_path / "report.csv"
    destination.write_text("original", encoding="utf-8")
    original_iterator = reports.iter_report_rows
    calls = 0

    def fail_second_pass(result, selected_paths=()):
        nonlocal calls
        calls += 1
        iterator = original_iterator(result, selected_paths)
        if calls == 1:
            yield from iterator
            return
        yield next(iterator)
        raise OSError("simulated export failure")

    monkeypatch.setattr(reports, "iter_report_rows", fail_second_pass)
    with pytest.raises(OSError, match="simulated"):
        reports.export_csv(str(destination), result_fixture())
    assert destination.read_text(encoding="utf-8") == "original"
    assert not list(tmp_path.glob(".dupscan-report-*.tmp"))


def test_export_rejects_unsafe_destination_and_invalid_result(tmp_path):
    target = tmp_path / "target"
    target.write_text("keep")
    symlink = tmp_path / "report.csv"
    symlink.symlink_to(target)
    with pytest.raises(ValueError, match="safe regular"):
        reports.export_csv(str(symlink), result_fixture())
    assert target.read_text() == "keep"

    bad = SimpleNamespace(
        file_groups=[SimpleNamespace(size=-1, digest="a", paths=["/a", "/b"])],
        dir_groups=[],
        sim_pairs=[],
    )
    with pytest.raises(ValueError, match="number"):
        reports.export_csv(str(tmp_path / "bad.csv"), bad)
    assert not (tmp_path / "bad.csv").exists()


def test_csv_quotes_newlines_and_commas_without_row_injection(tmp_path):
    result = SimpleNamespace(
        file_groups=[
            SimpleNamespace(size=1, digest="b" * 64, paths=["/a,line\nnext.txt", "/b.txt"])
        ],
        dir_groups=[],
        sim_pairs=[],
        errors=[],
    )
    destination = tmp_path / "quoted.csv"
    reports.export_csv(str(destination), result)
    with destination.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 2
    assert rows[0]["path"] == "/a,line\nnext.txt"
