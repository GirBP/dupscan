"""T7a: property-тест незнищенності. Випадкові послідовності ЛЕГАЛЬНИХ дій
(видалення однієї копії з групи за правилом вцілілого, видалення теки без
останніх копій, recompute). Інваріант після КОЖНОГО кроку: контент, що мав
≥2 копій, зберігає ≥1 живу копію; стан відповідає диску."""

import os
import random
import shutil
import sys
import tempfile

import blake3
import pytest

os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.domain.core as core  # noqa: E402


def digest_of(path: str) -> str:
    return blake3.blake3(open(path, "rb").read()).hexdigest()


def build_tree(base: str, seed: int) -> dict[str, set[str]]:
    """Повертає початкову мапу digest -> множина шляхів."""
    rng = random.Random(seed)
    families = [
        rng.randbytes(rng.randint(1024, core.PARTIAL * 2)) for _ in range(rng.randint(5, 9))
    ]
    for d in range(rng.randint(8, 14)):
        dp = os.path.join(base, f"d{d % 3}", f"dir{d}")
        os.makedirs(dp, exist_ok=True)
        for f in range(rng.randint(2, 6)):
            data = (
                rng.choice(families)
                if rng.random() < 0.6
                else rng.randbytes(rng.randint(512, 8000))
            )
            with open(os.path.join(dp, f"f{f}.bin"), "wb") as fh:
                fh.write(data)
    by_digest: dict[str, set[str]] = {}
    for root, _dirs, files in os.walk(base):
        for f in files:
            p = os.path.join(root, f)
            by_digest.setdefault(digest_of(p), set()).add(p)
    return by_digest


def surviving_content_ok(initial: dict[str, set[str]]) -> bool:
    """Кожен контент, що мав ≥2 копій, досі має ≥1 живу копію на диску."""
    for _digest, paths in initial.items():
        if len(paths) < 2:
            continue
        if not any(os.path.exists(p) for p in paths):
            return False
    return True


@pytest.mark.parametrize("seed", [5, 21, 77])
def test_random_legal_actions_never_destroy_content(tmp_path, seed):
    rng = random.Random(seed * 1000 + 1)
    initial = build_tree(str(tmp_path), seed)
    r = core.scan([str(tmp_path)])

    for step in range(15):
        action = rng.choice(["delete_copy", "delete_copy", "recompute_noop", "delete_empty_dir"])
        if action == "delete_copy":
            # легальна дія: з групи ≥2 живих копій прибрати ОДНУ (не останню)
            candidates = [(cid, paths) for cid, paths in r.class_paths.items() if len(paths) >= 2]
            if not candidates:
                continue
            _cid, paths = rng.choice(candidates)
            victim = rng.choice(paths)
            os.remove(victim)
            assert core.recompute(r, {victim})
        elif action == "delete_empty_dir":
            empties = [
                d
                for d, files in r.dir_files.items()
                if not files
                and not r.dir_children.get(d)
                and os.path.isdir(d)
                and d != str(tmp_path)
            ]
            if not empties:
                continue
            d = rng.choice(empties)
            shutil.rmtree(d, ignore_errors=True)
            assert core.recompute(r, {d})
        else:
            assert core.recompute(r, set())

        assert surviving_content_ok(initial), f"seed={seed} крок={step}: контент зник з усіх копій"
        # стан відповідає диску: кожен файл стану існує
        for p in r.file_meta:
            assert os.path.exists(p), f"стан тримає мертвий шлях {p}"
        # і жодна група не містить неіснуючих шляхів
        for g in r.file_groups:
            for p in g.paths:
                assert os.path.exists(p)
