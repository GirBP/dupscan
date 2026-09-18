"""Замір пам'яті/розміру сесії для S2/S3 «Solid Core».

Синтетичний ScanResult на N файлів (за замовчуванням 300 000) з
реалістичною структурою: теки по 10 файлів, половина файлів — дублікати
класами по 2, решта унікальні.

Режими (кожен запускати СВІЖИМ процесом, ru_maxrss — пік процесу):
  build          — побудувати результат у пам'яті, друк RSS
  save DIR       — побудувати і зберегти сесію в DIR, друк розміру файлу
  load PATH      — завантажити сесію з PATH (з агрегацією), друк RSS і часу

Приклад:
  .venv/bin/python qa/mem_benchmark.py build
  .venv/bin/python qa/mem_benchmark.py save /tmp/bench_store
  .venv/bin/python qa/mem_benchmark.py load /tmp/bench_store/sessions/<...>.json.gz
"""

import hashlib
import os
import resource
import sys
import time

# BENCH_IMPORT_DIR дозволяє заміряти інше дерево (напр. 2.16) БЕЗ його
# редагування; за замовчуванням — власне дерево цього скрипта.
_BENCH_DIR = os.environ.get(
    "BENCH_IMPORT_DIR",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _BENCH_DIR)
sys.path.insert(0, os.path.join(_BENCH_DIR, "src"))

N_FILES = int(os.environ.get("BENCH_FILES", "300000"))
# Частка файлів у класах дублікатів. 1.0 — стрес (кожен файл у парі,
# storage/alloc на всіх); 0.1 — реалістичний скан (~10% дублікатів).
DUP_RATIO = float(os.environ.get("BENCH_DUP_RATIO", "1.0"))
FILES_PER_DIR = 10
ROOT = "/private/tmp/dupscan-bench-root"


def rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024)  # macOS: ru_maxrss у байтах


def build_result():
    import dupscan.domain.core as core

    res = core.ScanResult()
    res.scanned_roots = [ROOT]
    n_dirs = N_FILES // FILES_PER_DIR
    res.dir_children[ROOT] = []
    res.dir_ok[ROOT] = True
    res.dir_files[ROOT] = []
    class_counter = 0
    uniq_counter = 0
    dup_every = max(2, round(2 / DUP_RATIO)) if DUP_RATIO > 0 else 0
    for d in range(n_dirs):
        dirpath = f"{ROOT}/dir{d:05d}"
        res.dir_children[ROOT].append(dirpath)
        res.dir_ok[dirpath] = True
        res.dir_files[dirpath] = []
        for j in range(FILES_PER_DIR):
            i = d * FILES_PER_DIR + j
            path = f"{dirpath}/file{j:04d}.bin"
            size = 4096 + (i % 100) * 512
            info = core.FileInfo(
                path, size, 1_700_000_000_000_000_000 + i,
                1_600_000_000_000_000_000 + i,
                1_700_000_000_000_000_000 + i, 16777231, 10_000_000 + i)
            res.file_meta[path] = info
            res.dir_files[dirpath].append(path)
            # кожні dup_every файлів — пара дублікатів (i, i+1); решта
            # унікальні u:N без storage/alloc (як у реальному скані)
            in_pair = dup_every and (i % dup_every) in (0, 1)
            if in_pair and i % dup_every == 0:
                class_counter += 1
                # детермінований, але ентропійний hex — як справжній BLAKE3;
                # "%064x" % лічильник давав стисливі нулі й занижував розмір
                digest = hashlib.sha256(str(class_counter).encode()).hexdigest()
                cls = f"{size}:{digest}"
                res.file_class[path] = cls
                res.class_size[cls] = size
                res.class_paths[cls] = [path]
                res.file_storage[path] = (16777231, i * 4096)
                res.file_alloc[path] = size
                pending = cls
            elif in_pair:
                # другий файл пари мусить мати ТОЙ САМИЙ розмір, інакше
                # клас невалідний; беремо розмір парного попередника
                prev = res.class_paths[pending][0]
                info.size = res.file_meta[prev].size
                res.file_class[path] = pending
                res.class_paths[pending].append(path)
                res.file_storage[path] = (16777231, i * 4096)
                res.file_alloc[path] = info.size
            else:
                uniq_counter += 1
                res.file_class[path] = f"u:{uniq_counter}"
    res.files_seen = N_FILES
    res.bytes_seen = sum(v.size for v in res.file_meta.values())
    return res


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "build"
    if mode == "build":
        import dupscan.domain.core as core  # noqa: F401 — прогріти імпорти до базового заміру
        base = rss_mb()
        t0 = time.monotonic()
        res = build_result()
        dt = time.monotonic() - t0
        mb = rss_mb()
        delta = mb - base
        print(f"build: files={len(res.file_meta):,} rss={mb:.1f} MB "
              f"(Δ={delta:.1f} MB, {delta * 1024 * 1024 / N_FILES:.0f} "
              f"B/файл) t={dt:.2f}s")
    elif mode == "save":
        import dupscan.infra.session as session
        base = sys.argv[2]
        res = build_result()
        t0 = time.monotonic()
        path = session.save_session(res, [ROOT], base_dir=base)
        dt = time.monotonic() - t0
        assert path, res.errors[-3:]
        size = os.path.getsize(path)
        print(f"save: {path}")
        print(f"save: bytes={size:,} ({size / N_FILES:.1f} B/файл) t={dt:.2f}s")
    elif mode == "load":
        import dupscan.infra.session as session
        path = sys.argv[2]
        t0 = time.monotonic()
        res = session.load_session(path)
        dt = time.monotonic() - t0
        mb = rss_mb()
        n = len(res.file_meta)
        print(f"load: files={n:,} groups={len(res.file_groups):,} "
              f"rss={mb:.1f} MB ({mb * 1024 * 1024 / max(1, n):.0f} B/файл) "
              f"t={dt:.2f}s")
    else:
        raise SystemExit(f"невідомий режим: {mode}")


if __name__ == "__main__":
    main()
