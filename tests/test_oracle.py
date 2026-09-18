"""C: oracle-коректність. Воронка scan() звіряється з наївним повним
хешуванням КОЖНОГО файла на випадкових деревах (фіксовані сіди), включно з
пастками «спільна голова — різний хвіст». Групи тек: soundness — кожна
заявлена група рекурсивно ідентична насправді."""

import os
import random
import sys
import tempfile

import blake3
import pytest

os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.domain.core as core  # noqa: E402


def build_random_tree(base: str, seed: int) -> int:
    rng = random.Random(seed)
    payload_pool: list[bytes] = []
    for _ in range(rng.randint(8, 16)):  # сім'ї дублікатів
        size = rng.choice(
            [
                rng.randint(1, 4096),  # дрібні
                rng.randint(4096, core.PARTIAL),  # до межі
                core.PARTIAL,  # рівно межа
                rng.randint(core.PARTIAL + 1, core.PARTIAL * 3),  # за межею
            ]
        )
        payload_pool.append(rng.randbytes(size))
    # пастки: спільна голова PARTIAL, різні хвости
    head = rng.randbytes(core.PARTIAL)
    traps = [head + rng.randbytes(rng.randint(1, 2048)) for _ in range(3)]

    n = 0
    for d in range(rng.randint(15, 30)):
        dp = os.path.join(base, f"lvl{d % 4}", f"dir{d}")
        os.makedirs(dp, exist_ok=True)
        for f in range(rng.randint(2, 10)):
            r = rng.random()
            if r < 0.45:
                data = rng.choice(payload_pool)  # дублікат сім'ї
            elif r < 0.55:
                data = rng.choice(traps)  # пастка
            else:
                data = rng.randbytes(rng.randint(1, core.PARTIAL * 2))
            fp = os.path.join(dp, f"f{f}.bin")
            with open(fp, "wb") as fh:
                fh.write(data)
            n += 1
        if rng.random() < 0.3 and n:
            try:
                os.symlink(fp, os.path.join(dp, "link"))
            except OSError:
                pass
    return n


def oracle_file_groups(base: str) -> set[frozenset]:
    by_digest: dict[tuple, list[str]] = {}
    for root, dirs, files in os.walk(base):
        for f in files:
            p = os.path.join(root, f)
            st = os.lstat(p)
            if not os.path.isfile(p) or os.path.islink(p) or st.st_size == 0:
                continue
            d = blake3.blake3(open(p, "rb").read()).hexdigest()
            by_digest.setdefault((st.st_size, d), []).append(p)
    return {frozenset(v) for v in by_digest.values() if len(v) >= 2}


def dirs_identical_naive(a: str, b: str) -> bool:
    """Рекурсивне наївне порівняння двох тек (файли за вмістом, лінки за
    target, підтеки за іменами)."""
    try:
        ea = sorted(os.listdir(a))
        eb = sorted(os.listdir(b))
    except OSError:
        return False
    if ea != eb:
        return False
    for name in ea:
        pa, pb = os.path.join(a, name), os.path.join(b, name)
        la, lb = os.path.islink(pa), os.path.islink(pb)
        if la or lb:
            if not (la and lb and os.readlink(pa) == os.readlink(pb)):
                return False
            continue
        if os.path.isdir(pa) != os.path.isdir(pb):
            return False
        if os.path.isdir(pa):
            if not dirs_identical_naive(pa, pb):
                return False
        else:
            if open(pa, "rb").read() != open(pb, "rb").read():
                return False
    return True


@pytest.mark.parametrize("seed", [11, 23, 47, 91])
def test_scan_matches_bruteforce_oracle(tmp_path, seed):
    n = build_random_tree(str(tmp_path), seed)
    r = core.scan([str(tmp_path)])
    got = {frozenset(g.paths) for g in r.file_groups}
    want = oracle_file_groups(str(tmp_path))
    assert got == want, (
        f"seed={seed}, файлів={n}: scan() і повний перебір мусять збігатися; "
        f"зайве={got - want}, пропущене={want - got}"
    )


@pytest.mark.parametrize("seed", [13, 57])
def test_dir_groups_are_sound(tmp_path, seed):
    build_random_tree(str(tmp_path), seed)
    # гарантована пара ідентичних тек, щоб перевірка не була порожньою
    src = tmp_path / "twin_a"
    os.makedirs(src / "sub", exist_ok=True)
    (src / "f.bin").write_bytes(b"T" * 5000)
    (src / "sub/g.bin").write_bytes(b"G" * 3000)
    import shutil

    shutil.copytree(src, tmp_path / "twin_b")
    r = core.scan([str(tmp_path)])
    assert r.dir_groups, "контрольна пара тек мусить знайтися"
    for g in r.dir_groups:
        first = g.paths[0]
        for other in g.paths[1:]:
            assert dirs_identical_naive(first, other), f"хибний дублікат тек: {first} vs {other}"
