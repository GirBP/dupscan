"""Регресія: публікація злиття на ФС без жорстких посилань (exFAT/FAT/SMB).

Реальний кейс власника (диск BARRACUDA, exFAT): перенос більшості файлів
проходив, але на колізії імені os.link давав «[Errno 45] Operation not
supported: 'x.pyc' -> 'x (2).pyc'» і файл не переносився.

Механізм exFAT відтворено точно: VFS повертає EEXIST з namei, ЯКЩО ціль
існує (звідси суфіксна гілка), і ENOTSUP з vnop_link, коли цілі ще нема.
Тобто саме СУФІКСНА спроба падає ENOTSUP — як у діалозі власника.
"""

import errno
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication  # noqa: E402

import dupscan.ui.app as app_mod  # noqa: E402
import dupscan.domain.core as core  # noqa: E402
import dupscan.infra.fsops as fsops  # noqa: E402

_qapp = QApplication.instance() or QApplication([])


def make(p, data: bytes):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def _install_exfat_link(monkeypatch):
    """os.link, як на exFAT: FileExistsError коли ціль є, інакше ENOTSUP."""
    def exfat_link(src, dst, *, src_dir_fd=None, dst_dir_fd=None,
                   follow_symlinks=True):
        exists = True
        try:
            os.stat(dst, dir_fd=dst_dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            exists = False
        if exists:
            raise FileExistsError(errno.EEXIST, "File exists")
        raise OSError(errno.ENOTSUP, "Operation not supported")
    monkeypatch.setattr(fsops.os, "link", exfat_link)


def _prep(r, src, dst):
    """GUI-шлях: MergePreparationWorker дає план + свіжі digest-и."""
    worker = app_mod.MergePreparationWorker(r, src, dst)
    done, failed = [], []
    worker.done.connect(done.append)
    worker.failed.connect(failed.append)
    worker.run()
    assert not failed, failed
    plan, _total, expected_digests, _same, root_identities = done[0]
    return plan, expected_digests, root_identities


def _owner_like(tmp_path):
    """Дві теки-дублікати; у B — унікальні файли й один колізійний."""
    shared = os.urandom(64 * 1024)
    make(tmp_path / "A/shared.bin", shared)
    make(tmp_path / "B/shared.bin", shared)               # дублікат — лишиться
    make(tmp_path / "B/uniq.bin", os.urandom(20_000))     # унікальний
    make(tmp_path / "B/d/все/graf/uniq2.bin", os.urandom(9_000))  # вкладений
    # колізія: A вже має файл із таким rel-шляхом, але ІНШИМ вмістом
    make(tmp_path / "A/collide.bin", b"ORIGINAL-IN-TARGET")
    make(tmp_path / "B/collide.bin", os.urandom(5_000))
    r = core.scan([str(tmp_path)])
    return r, str(tmp_path / "B"), str(tmp_path / "A")


def test_move_publishes_without_hardlink_support(tmp_path, monkeypatch):
    r, src, dst = _owner_like(tmp_path)
    plan, digests, roots = _prep(r, src, dst)
    _install_exfat_link(monkeypatch)
    moved, errors = app_mod._move_files(r, plan, dst, src, digests, roots)
    assert errors == [], f"ENOTSUP не має валити перенос: {errors}"
    assert os.path.exists(os.path.join(dst, "uniq.bin"))
    assert os.path.exists(os.path.join(dst, "d/все/graf/uniq2.bin"))
    # джерело переміщених зникло (семантика move)
    assert not os.path.exists(os.path.join(src, "uniq.bin"))


def test_move_collision_never_overwrites_on_exfat(tmp_path, monkeypatch):
    r, src, dst = _owner_like(tmp_path)
    plan, digests, roots = _prep(r, src, dst)
    b_collide = (tmp_path / "B/collide.bin").read_bytes()
    _install_exfat_link(monkeypatch)
    moved, errors = app_mod._move_files(r, plan, dst, src, digests, roots)
    assert errors == []
    # наявний файл у цілі НЕ перезаписано
    assert (tmp_path / "A/collide.bin").read_bytes() == b"ORIGINAL-IN-TARGET"
    # нова копія отримала суфікс « (2)» і має вміст джерела
    assert (tmp_path / "A/collide (2).bin").read_bytes() == b_collide


def test_copy_publishes_without_hardlink_support(tmp_path, monkeypatch):
    r, src, dst = _owner_like(tmp_path)
    plan, digests, roots = _prep(r, src, dst)
    _install_exfat_link(monkeypatch)
    copied, errors = app_mod._copy_files(r, plan, dst, src, digests, roots)
    assert errors == [], f"ENOTSUP не має валити копіювання: {errors}"
    assert os.path.exists(os.path.join(dst, "uniq.bin"))
    # copy лишає джерело недоторканним
    assert os.path.exists(os.path.join(src, "uniq.bin"))
    assert (tmp_path / "A/collide.bin").read_bytes() == b"ORIGINAL-IN-TARGET"
    assert os.path.exists(os.path.join(dst, "collide (2).bin"))


def test_non_capability_link_error_still_surfaces(tmp_path, monkeypatch):
    """EIO — не «немає підтримки», а справжній збій: НЕ маскувати rename-ом."""
    r, src, dst = _owner_like(tmp_path)
    plan, digests, roots = _prep(r, src, dst)

    def broken_link(src_, dst_, *, src_dir_fd=None, dst_dir_fd=None,
                    follow_symlinks=True):
        raise OSError(errno.EIO, "Input/output error")
    monkeypatch.setattr(fsops.os, "link", broken_link)
    moved, errors = app_mod._move_files(r, plan, dst, src, digests, roots)
    assert any("I/O" in e or "Input/output" in e or "5" in e for e in errors), (
        f"справжній збій os.link мусить лишатись помилкою: {errors}")


def test_move_placeholder_cleaned_up_if_rename_fails(tmp_path, monkeypatch):
    """Якщо rename у фолбеку впаде — 0-байтова заглушка не лишається."""
    r, src, dst = _owner_like(tmp_path)
    plan, digests, roots = _prep(r, src, dst)
    _install_exfat_link(monkeypatch)

    real_rename = os.rename

    def broken_rename(*a, **k):
        raise OSError(errno.EIO, "Input/output error")
    monkeypatch.setattr(fsops.os, "rename", broken_rename)
    app_mod._move_files(r, plan, dst, src, digests, roots)
    monkeypatch.setattr(fsops.os, "rename", real_rename)
    # у цілі не лишилось порожніх заглушок від невдалої публікації
    for name in ("uniq.bin",):
        p = os.path.join(dst, name)
        if os.path.exists(p):
            assert os.path.getsize(p) > 0, "порожня заглушка не має лишатись"


def test_owner_chain_completes_on_exfat(tmp_path, monkeypatch):
    """Повний ланцюг перевірка→план→перенос→Кошик на «exFAT» до кінця."""
    r, src, dst = _owner_like(tmp_path)
    plan, digests, roots = _prep(r, src, dst)
    _install_exfat_link(monkeypatch)
    moved, errors = app_mod._move_files(r, plan, dst, src, digests, roots)
    assert errors == []
    trashed = []
    monkeypatch.setattr(
        app_mod, "to_trash", lambda ps, **k: (trashed.extend(ps), [])[1])
    kind, _payload = app_mod._verify_then_trash_dir(r, src)
    assert kind == "ok", "після переносу джерело мусить піти в Кошик"
    assert trashed == [src]
