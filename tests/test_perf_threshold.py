"""T7b: перф-поріг у гейті. Агрегація 100k синтетичних файлів мусить
вкладатися в ліміт (запас ×6 від виміряного) — ловить квадратичні регресії."""

import os
import sys
import tempfile
import time

os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.domain.core as core  # noqa: E402


def synth(n_files: int, per_dir: int = 30, dup_every: int = 3) -> core.ScanResult:
    res = core.ScanResult()
    for d in range(n_files // per_dir):
        dp = f"/synth/top{d // 40}/dir{d}"
        parent = f"/synth/top{d // 40}"
        res.dir_ok[dp] = True
        res.dir_files[dp] = []
        res.dir_ok.setdefault(parent, True)
        res.dir_files.setdefault(parent, [])
        res.dir_children.setdefault(parent, []).append(dp)
        for f in range(per_dir):
            fp = f"{dp}/f{f}.bin"
            size = 2048 + (d * f) % 60000
            res.file_meta[fp] = core.FileInfo(fp, size, 1, 1)
            res.dir_files[dp].append(fp)
            if f % dup_every == 0:
                cls = f"{size}:shared{(d * per_dir + f) % 200}"
                res.class_paths.setdefault(cls, []).append(fp)
                res.class_size[cls] = size
            else:
                cls = f"u:{d}_{f}"
            res.file_class[fp] = cls
    res.files_seen = len(res.file_meta)
    return res


def test_aggregate_100k_under_threshold():
    res = synth(100_000)
    t0 = time.monotonic()
    core._aggregate(res)
    elapsed = time.monotonic() - t0
    assert elapsed < 2.5, (
        f"агрегація 100k = {elapsed:.2f}s — квадратична регресія? базлайн 0.37s, ліміт 2.5s"
    )
    assert res.file_groups, "агрегація мусить дати групи"
