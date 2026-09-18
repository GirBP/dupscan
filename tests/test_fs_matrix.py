"""T6: матриця файлових систем на hdiutil-образах. Вмикається env
DUPSCAN_FS_MATRIX=1 (hdiutil недоступний у частині пісочниць). Повний
самодостатній прогін — scripts/fs_matrix.sh."""

import os
import sys
import tempfile

import pytest

os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.domain.core as core  # noqa: E402
from qa.fs_images import hdiutil_available, mount_image, unmount  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.environ.get("DUPSCAN_FS_MATRIX") != "1" or not hdiutil_available(),
    reason="потрібен hdiutil: увімкнути DUPSCAN_FS_MATRIX=1",
)


# ExFAT — файлова система зовнішнього диска власника (BARRACUDA) і єдина тут
# без жорстких посилань; саме на ній ламалося злиття (хотфікс 4).
@pytest.mark.parametrize("fs", ["APFS", "Case-sensitive APFS", "HFS+", "ExFAT"])
def test_duplicates_found_on_fs(fs):
    _img, mnt = mount_image(fs)
    try:
        data = os.urandom(64 * 1024)
        for top in ("A", "B"):
            d = os.path.join(mnt, top)
            os.makedirs(d)
            with open(os.path.join(d, "f.bin"), "wb") as fh:
                fh.write(data)
        r = core.scan([mnt])
        assert len(r.file_groups) == 1
        assert len(r.file_groups[0].paths) == 2
    finally:
        unmount(mnt)


def test_case_sensitive_names_are_distinct_files():
    _img, mnt = mount_image("Case-sensitive APFS")
    try:
        d = os.path.join(mnt, "Docs")
        os.makedirs(d)
        payload = os.urandom(16 * 1024)
        with open(os.path.join(d, "File.bin"), "wb") as fh:
            fh.write(payload)
        with open(os.path.join(d, "file.bin"), "wb") as fh:
            fh.write(payload)
        r = core.scan([mnt])
        assert r.files_seen == 2, "на case-sensitive ФС це РІЗНІ файли"
        assert len(r.file_groups) == 1, "і вони чесні дублікати за вмістом"
    finally:
        unmount(mnt)


def test_enospc_during_copy_is_clean_error():
    _img, mnt = mount_image("APFS", size_mb=16)
    try:
        src = os.path.join(mnt, "src.bin")
        with open(src, "wb") as fh:
            fh.write(os.urandom(6 * 1024 * 1024))
        # заповнити том детерміновано: лишити МЕНШЕ, ніж треба для копії
        vfs = os.statvfs(mnt)
        free = vfs.f_bavail * vfs.f_frsize
        filler_size = max(0, free - 2 * 1024 * 1024)  # лишаємо ~2 МіБ
        filler = os.path.join(mnt, "filler.bin")
        with open(filler, "wb") as fh:
            written = 0
            while written < filler_size:
                chunk = min(1 << 20, filler_size - written)
                fh.write(os.urandom(chunk))
                written += chunk
            fh.flush()
            os.fsync(fh.fileno())
        dst = os.path.join(mnt, "dst.bin")
        with pytest.raises(OSError):
            with open(src, "rb") as s, open(dst, "wb") as t:
                while True:
                    chunk = s.read(1 << 20)
                    if not chunk:
                        break
                    t.write(chunk)
                    t.flush()
                    os.fsync(t.fileno())
        # джерело неушкоджене
        assert os.path.getsize(src) == 6 * 1024 * 1024
    finally:
        unmount(mnt)
