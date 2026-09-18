from types import SimpleNamespace

import dupscan.domain.core as core
import dupscan.domain.product as product


def _result(tmp_path):
    a = tmp_path / "A"
    b = tmp_path / "B"
    (a / "nested").mkdir(parents=True)
    b.mkdir()
    paths = {
        "same_a": str(a / "same.jpg"),
        "same_b": str(b / "same.jpg"),
        "moved_a": str(a / "nested" / "moved.pdf"),
        "moved_b": str(b / "elsewhere.pdf"),
        "diff_a": str(a / "changed.txt"),
        "diff_b": str(b / "changed.txt"),
        "only_a": str(a / "only.zip"),
        "only_b": str(b / "only.mp3"),
    }
    result = core.ScanResult()
    result.dir_files = {
        str(a): [paths["same_a"], paths["diff_a"], paths["only_a"]],
        str(a / "nested"): [paths["moved_a"]],
        str(b): [paths["same_b"], paths["moved_b"], paths["diff_b"], paths["only_b"]],
    }
    result.dir_children = {str(a): [str(a / "nested")]}
    result.file_class = {
        paths["same_a"]: "10:same", paths["same_b"]: "10:same",
        paths["moved_a"]: "20:moved", paths["moved_b"]: "20:moved",
        paths["diff_a"]: "u:1", paths["diff_b"]: "u:2",
        paths["only_a"]: "u:3", paths["only_b"]: "u:4",
    }
    result.class_paths = {
        "10:same": [paths["same_a"], paths["same_b"]],
        "20:moved": [paths["moved_a"], paths["moved_b"]],
    }
    result.file_meta = {
        path: SimpleNamespace(size=i + 1, dev=1, ino=i + 10)
        for i, path in enumerate(paths.values())
    }
    return result, a, b, paths


def test_categories_and_summary():
    assert product.category_for_path("photo.HEIC") == "image"
    assert product.category_for_path("/Applications/X.app") == "application"
    assert product.category_for_path("README") == "other"
    summary = product.category_summary(
        {"a.jpg", "b.jpg", "notes.pdf"},
        {"a.jpg": SimpleNamespace(size=2), "b.jpg": SimpleNamespace(size=3),
         "notes.pdf": SimpleNamespace(size=5)},
    )
    assert summary == {"image": (2, 5), "document": (1, 5)}


def test_folder_comparison_covers_all_statuses(tmp_path):
    result, a, b, _paths = _result(tmp_path)
    comparison = product.compare_folders(result, str(a), str(b))
    by_rel = {row.relative_path: row.status for row in comparison.rows}
    assert by_rel["same.jpg"] == "identical"
    assert by_rel["nested/moved.pdf"] == "shared_elsewhere"
    assert by_rel["elsewhere.pdf"] == "shared_elsewhere"
    assert by_rel["changed.txt"] == "different_content"
    assert by_rel["only.zip"] == "only_a"
    assert by_rel["only.mp3"] == "only_b"


def test_explanation_requires_independent_inodes(tmp_path):
    result, _a, _b, paths = _result(tmp_path)
    assert "Повний BLAKE3" in product.duplicate_explanation(result, paths["same_a"])
    result.file_meta[paths["same_b"]].ino = result.file_meta[paths["same_a"]].ino
    assert "заблоковано" in product.duplicate_explanation(result, paths["same_a"])


def test_problem_classification():
    title, advice = product.classify_problem("Permission denied")
    assert title == "Немає доступу"
    assert "Full Disk Access" in advice
