"""Повний oracle трьох результатів на випадкових деревах.

1. Групи ТЕК: точна рівність із наївним рекурсивним підписом (файли за
   повним хешем, symlink-и за target, підтеки за іменами) + те саме правило
   пригнічення вкладених груп. Не лише soundness — ПОВНОТА.
2. Подібність: для КОЖНОЇ виданої пари shared_bytes і відсоток
   перераховуються незалежно з диска і мусять збігтися до байта; жодна
   видана пара не може бути фантомом.
3. Конструйований кейс подібності з наперед відомими числами.
"""

import os
import random
import sys
import tempfile
from collections import Counter

import blake3
import pytest

os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.domain.core as core  # noqa: E402


# ---- незалежні (наївні) обчислення з диска ---------------------------------


def full_digest(path: str) -> str:
    return blake3.blake3(open(path, "rb").read()).hexdigest()


def walk_state(base: str):
    """Файли/лінки/підтеки з диска, без жодного коду сканера."""
    files: dict[str, list[tuple[str, str, int]]] = {}
    links: dict[str, list[tuple[str, str]]] = {}
    subdirs: dict[str, list[str]] = {}
    for root, dirs, names in os.walk(base):
        files.setdefault(root, [])
        links.setdefault(root, [])
        subdirs[root] = [
            os.path.join(root, d) for d in dirs if not os.path.islink(os.path.join(root, d))
        ]
        for n in names:
            p = os.path.join(root, n)
            if os.path.islink(p):
                links[root].append((n, os.readlink(p)))
            elif os.path.isfile(p) and os.path.getsize(p) > 0:
                files[root].append((n, full_digest(p), os.path.getsize(p)))
    return files, links, subdirs


def oracle_dir_groups(base: str) -> set[frozenset]:
    files, links, subdirs = walk_state(base)
    sig_memo: dict[str, tuple] = {}
    count_memo: dict[str, int] = {}

    def sig(d: str):
        if d in sig_memo:
            return sig_memo[d]
        entry = (
            tuple(sorted((n, dg, sz) for n, dg, sz in files.get(d, []))),
            tuple(sorted(links.get(d, []))),
            tuple(sorted((os.path.basename(s), sig(s)) for s in subdirs.get(d, []))),
        )
        sig_memo[d] = entry
        count_memo[d] = len(files.get(d, [])) + sum(count_memo[s] for s in subdirs.get(d, []))
        return entry

    all_dirs = list(files)
    for d in all_dirs:
        sig(d)
    by_sig: dict[tuple, list[str]] = {}
    for d in all_dirs:
        if count_memo[d] >= 1:
            by_sig.setdefault(sig(d), []).append(d)
    grouped = {d for ds in by_sig.values() if len(ds) >= 2 for d in ds}

    def covered(d: str) -> bool:
        p = os.path.dirname(d)
        while len(p) > len(base) - 1 and p != d:
            if p in grouped:
                return True
            np = os.path.dirname(p)
            if np == p:
                break
            p = np
        return False

    out = set()
    for ds in by_sig.values():
        kept = [d for d in ds if not covered(d)]
        if len(kept) >= 2:
            out.add(frozenset(kept))
    return out


def oracle_similarity(base: str):
    """Незалежний перерахунок Dice-подібності для будь-якої пари тек."""
    files, _links, subdirs = walk_state(base)
    by_digest: Counter = Counter()
    for rows in files.values():
        for _n, dg, _sz in rows:
            by_digest[dg] += 1
    dup_digests = {dg for dg, c in by_digest.items() if c >= 2}

    classes_memo: dict[str, Counter] = {}
    bytes_memo: dict[str, int] = {}
    size_of: dict[str, int] = {}

    def fill(d: str):
        if d in classes_memo:
            return
        cnt: Counter = Counter()
        total = 0
        for _n, dg, sz in files.get(d, []):
            total += sz
            if dg in dup_digests:
                cnt[dg] += 1
                size_of[dg] = sz
        for s in subdirs.get(d, []):
            fill(s)
            cnt += classes_memo[s]
            total += bytes_memo[s]
        classes_memo[d] = cnt
        bytes_memo[d] = total

    for d in files:
        fill(d)

    def pair(a: str, b: str) -> tuple[int, float]:
        ca, cb = classes_memo[a], classes_memo[b]
        shared = sum(size_of[k] * min(ca[k], cb[k]) for k in ca.keys() & cb.keys())
        ta, tb = bytes_memo[a], bytes_memo[b]
        percent = round(200.0 * shared / (ta + tb), 1) if ta + tb else 0.0
        return shared, percent

    return pair


# ---- генератор випадкових дерев --------------------------------------------


def build_tree(base: str, seed: int) -> None:
    rng = random.Random(seed)
    pool = [rng.randbytes(rng.randint(2048, core.PARTIAL * 2)) for _ in range(rng.randint(6, 10))]
    for d in range(rng.randint(10, 18)):
        dp = os.path.join(base, f"top{d % 3}", f"dir{d}")
        os.makedirs(dp, exist_ok=True)
        for f in range(rng.randint(1, 6)):
            data = (
                rng.choice(pool) if rng.random() < 0.55 else rng.randbytes(rng.randint(1024, 60000))
            )
            with open(os.path.join(dp, f"f{f}.bin"), "wb") as fh:
                fh.write(data)
        if rng.random() < 0.25:
            os.symlink("/tmp/target", os.path.join(dp, "ln"))
    # гарантовані близнюки на одній глибині + вкладена пара для пригнічення
    twin = os.path.join(base, "twinA", "inner")
    os.makedirs(twin, exist_ok=True)
    open(os.path.join(twin, "x.bin"), "wb").write(b"T" * 7000)
    import shutil

    shutil.copytree(os.path.join(base, "twinA"), os.path.join(base, "twinB"))


# ---- тести ------------------------------------------------------------------


@pytest.mark.parametrize("seed", [3, 19, 42])
def test_dir_groups_match_oracle_exactly(tmp_path, seed):
    build_tree(str(tmp_path), seed)
    r = core.scan([str(tmp_path)])
    got = {frozenset(g.paths) for g in r.dir_groups}
    want = oracle_dir_groups(str(tmp_path))
    assert got == want, (
        f"seed={seed}: групи тек != oracle;\nзайве={got - want}\nпропущене={want - got}"
    )


@pytest.mark.parametrize("seed", [7, 29, 63])
def test_similarity_values_exact_and_no_phantoms(tmp_path, seed):
    build_tree(str(tmp_path), seed)
    r = core.scan([str(tmp_path)])
    pair_value = oracle_similarity(str(tmp_path))
    assert r.sim_pairs, "перевірка не має права бути порожньою"
    for p in r.sim_pairs:
        shared, percent = pair_value(p.dir_a, p.dir_b)
        assert shared > 0, f"фантомна пара {p.dir_a} ~ {p.dir_b}"
        assert p.shared_bytes == shared, (
            f"{p.dir_a} ~ {p.dir_b}: shared {p.shared_bytes} != oracle {shared}"
        )
        assert p.percent == percent, (
            f"{p.dir_a} ~ {p.dir_b}: percent {p.percent} != oracle {percent}"
        )


def test_similarity_constructed_case_exact_numbers(tmp_path):
    # C і D: спільні 4000 (пряме) + 2500 (у підтеках під РІЗНИМИ іменами
    # файлів), власні 3000 і 5000. Очікування пораховано вручну:
    # shared = 6500; ta = 9500; tb = 11500; percent = 61.9
    def w(rel, data):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    w("C/shared.bin", b"S" * 4000)
    w("C/sub/deep.bin", b"Q" * 2500)
    w("C/own1.bin", b"1" * 3000)
    w("D/shared.bin", b"S" * 4000)
    w("D/other/renamed.bin", b"Q" * 2500)
    w("D/own2.bin", b"2" * 5000)
    r = core.scan([str(tmp_path)])
    cd = [
        p
        for p in r.sim_pairs
        if os.path.basename(p.dir_a) == "C" and os.path.basename(p.dir_b) == "D"
    ]
    assert cd, "пара C ~ D мусить бути знайдена"
    p = cd[0]
    assert p.shared_bytes == 6500
    assert p.percent == round(200.0 * 6500 / (9500 + 11500), 1) == 61.9
    pair_value = oracle_similarity(str(tmp_path))
    assert (p.shared_bytes, p.percent) == pair_value(p.dir_a, p.dir_b)
