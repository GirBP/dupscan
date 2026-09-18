"""Small isolated APFS user journey; never touches personal files or real Trash."""

import os
import sys

import send2trash

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.ui.app as app_mod  # noqa: E402
import dupscan.domain.core as core  # noqa: E402
import dupscan.infra.diagnostics as diagnostics  # noqa: E402
import dupscan.domain.product as product  # noqa: E402
import dupscan.infra.removal_history as removal_history  # noqa: E402
import dupscan.infra.reports as reports  # noqa: E402
import dupscan.infra.session as session  # noqa: E402


def make(path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_isolated_release_user_journey(tmp_path, monkeypatch):
    volume = tmp_path / "isolated-volume"
    side_a = volume / "A"
    side_b = volume / "B"
    same = b"exact-proof" * 1024
    shared = b"shared-large-folder-content" * 1024
    make(side_a / "exact" / "same.bin", same)
    make(side_b / "exact" / "same.bin", same)
    make(side_a / "similar" / "shared-a.bin", shared)
    make(side_b / "similar" / "shared-b.bin", shared)
    make(side_a / "similar" / "only-a.bin", b"A-only" * 400)
    make(side_b / "similar" / "only-b.bin", b"B-only" * 300)

    result = core.scan([str(volume)], walk_threads=1)
    assert not result.partial and result.errors == []
    assert any(
        set(group.paths)
        == {
            str(side_a / "exact"),
            str(side_b / "exact"),
        }
        for group in result.dir_groups
    )
    pair = next(
        candidate
        for candidate in result.sim_pairs
        if {
            candidate.dir_a,
            candidate.dir_b,
        }
        == {
            str(side_a / "similar"),
            str(side_b / "similar"),
        }
    )
    assert pair.shared_bytes == len(shared)

    comparison = product.compare_folders(
        result,
        str(side_a / "similar"),
        str(side_b / "similar"),
    )
    assert comparison.counts["shared_elsewhere"] == 2
    assert comparison.counts["only_a"] == 1
    assert comparison.counts["only_b"] == 1

    data_dir = tmp_path / "app-data"
    saved = session.save_session(
        result,
        [str(volume)],
        base_dir=str(data_dir),
    )
    historical = session.load_session(saved)
    assert not historical.live
    assert len(historical.file_groups) == len(result.file_groups)
    assert len(historical.dir_groups) == len(result.dir_groups)
    assert len(historical.sim_pairs) == len(result.sim_pairs)

    plan, total = core.merge_plan(
        result,
        str(side_a / "similar"),
        str(side_b / "similar"),
    )
    assert total == len(b"A-only" * 400)
    expected_digests = {}
    for _size, source, _relative in plan:
        expected_digests[source] = core.verify_current_file(
            source,
            result.file_meta[source],
        )[0]
    root_identities = {
        str(side_a / "similar"): app_mod._directory_identity(
            str(side_a / "similar")),
        str(side_b / "similar"): app_mod._directory_identity(
            str(side_b / "similar")),
    }
    copied, copy_errors = app_mod._copy_files(
        result,
        plan,
        str(side_b / "similar"),
        src_dir=str(side_a / "similar"),
        expected_digests=expected_digests,
        root_identities=root_identities,
    )
    assert copy_errors == [] and len(copied) == 1
    assert (side_a / "similar" / "only-a.bin").exists()
    assert (side_b / "similar" / "only-a.bin").read_bytes() == b"A-only" * 400

    csv_path = tmp_path / "report.csv"
    html_path = tmp_path / "report.html"
    reports.export_csv(str(csv_path), result)
    reports.export_html(str(html_path), result)
    assert csv_path.stat().st_size > 0
    assert html_path.stat().st_size > 0
    diagnostic_path = tmp_path / "diagnostics.zip"
    diagnostic = diagnostics.export_diagnostics_bundle(
        diagnostic_path,
        errors=result.errors,
        scanned_paths=result.file_meta,
    )
    assert diagnostic.bytes_written == diagnostic_path.stat().st_size
    assert not diagnostic.included_scanned_paths

    # Exercise the real proof/history/restore code with a generated fake
    # Trash backend. No item enters the user's actual macOS Trash.
    fake_trash = tmp_path / ".Trash"
    fake_trash.mkdir()
    victim = side_a / "exact" / "same.bin"
    real_expanduser = app_mod.os.path.expanduser

    def fake_expanduser(value):
        if value == "~/.Trash":
            return str(fake_trash)
        if value == "~":
            return str(tmp_path)
        return real_expanduser(value)

    def fake_send2trash(path):
        os.rename(path, fake_trash / os.path.basename(path))

    monkeypatch.setenv("DUPSCAN_DATA_DIR", str(data_dir))
    monkeypatch.setattr(app_mod.os.path, "expanduser", fake_expanduser)
    monkeypatch.setattr(send2trash, "send2trash", fake_send2trash)
    assert app_mod._verified_to_trash_files(result, [str(victim)]) == []
    assert not victim.exists()

    operation = removal_history.list_operations(
        base_dir=str(data_dir),
    )[0]
    restored = removal_history.restore_item(
        operation["id"],
        str(victim),
        allowed_roots=[str(volume)],
        base_dir=str(data_dir),
    )
    assert restored["restored_path"] == str(victim)
    assert victim.read_bytes() == same
    assert not (fake_trash / victim.name).exists()
