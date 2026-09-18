"""S4 «Solid Core»: session.compact_store — явне прибирання сховища сесій.

Retention за БАЙТАМИ і за віком незжатих legacy-сесій. НІЧОГО не
видаляється без явного виклику; save_session прибирання не запускає.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.infra.session as session  # noqa: E402

DAY_NS = 24 * 3600 * 10 ** 9


def _make_session(base_dir: str, created_ns: int, size: int,
                  compressed: bool = True) -> str:
    """Штучна пара payload+meta з заданим часом у імені та розміром."""
    sdir = os.path.join(base_dir, "sessions")
    os.makedirs(sdir, exist_ok=True)
    suffix = ".json.gz" if compressed else ".json"
    path = os.path.join(sdir, f"{created_ns}{suffix}")
    with open(path, "wb") as fh:
        fh.write(b"x" * size)
    meta = {
        "version": 3, "created_ns": created_ns, "roots": ["/tmp"],
        "partial": False, "files_seen": 1, "bytes_seen": 1,
        "errors_total": 0, "counts": {"files": 0, "dirs": 0, "pairs": 0},
    }
    with open(session._meta_path(path), "w", encoding="utf-8") as fh:
        json.dump(meta, fh)
    return path


def test_compact_store_byte_budget_keeps_newest(tmp_path):
    base = str(tmp_path)
    now = time.time_ns()
    old1 = _make_session(base, now - 5 * DAY_NS, 400)
    old2 = _make_session(base, now - 4 * DAY_NS, 400)
    mid = _make_session(base, now - 3 * DAY_NS, 400)
    new = _make_session(base, now - DAY_NS, 400)
    # бюджет вміщує рівно дві наймолодші ПАРИ payload+meta
    budget = session._pair_size(mid) + session._pair_size(new) + 50
    report = session.compact_store(base_dir=base, max_bytes=budget)
    # найстаріші видаляються парами (payload+meta), поки не влазимо в бюджет
    assert not os.path.exists(old1) and not os.path.exists(session._meta_path(old1))
    assert not os.path.exists(old2)
    assert os.path.exists(mid) and os.path.exists(new)
    assert report["removed"] == 2
    assert report["freed_bytes"] >= 800
    assert report["store_bytes"] <= budget


def test_compact_store_never_removes_newest_session(tmp_path):
    base = str(tmp_path)
    now = time.time_ns()
    old = _make_session(base, now - 2 * DAY_NS, 600)
    new = _make_session(base, now - DAY_NS, 600)
    report = session.compact_store(base_dir=base, max_bytes=100)
    assert not os.path.exists(old)
    assert os.path.exists(new), "найновіша сесія недоторканна"
    assert report["removed"] == 1


def test_compact_store_legacy_age(tmp_path):
    base = str(tmp_path)
    now = time.time_ns()
    stale_legacy = _make_session(
        base, now - 40 * DAY_NS, 300, compressed=False)
    fresh_legacy = _make_session(
        base, now - 5 * DAY_NS, 300, compressed=False)
    modern_old = _make_session(base, now - 40 * DAY_NS + 1, 300)
    report = session.compact_store(base_dir=base, legacy_age_days=30)
    assert not os.path.exists(stale_legacy), "старий незжатий .json іде геть"
    assert os.path.exists(fresh_legacy), "свіжий legacy лишається"
    assert os.path.exists(modern_old), "вік чистить лише незжаті legacy"
    assert report["removed"] == 1


def test_compact_store_dry_run_removes_nothing(tmp_path):
    base = str(tmp_path)
    now = time.time_ns()
    old = _make_session(base, now - 5 * DAY_NS, 500)
    new = _make_session(base, now - DAY_NS, 500)
    report = session.compact_store(base_dir=base, max_bytes=600, dry_run=True)
    assert os.path.exists(old) and os.path.exists(new)
    assert report["removed"] >= 1  # скільки БУЛО Б прибрано
    assert report["freed_bytes"] > 0


def test_compact_store_preserves_explicit_paths(tmp_path):
    base = str(tmp_path)
    now = time.time_ns()
    protected = _make_session(base, now - 9 * DAY_NS, 700)
    victim = _make_session(base, now - 8 * DAY_NS, 700)
    _new = _make_session(base, now - DAY_NS, 100)
    session.compact_store(
        base_dir=base, max_bytes=900, preserve_paths=(protected,))
    assert os.path.exists(protected), "активна сесія недоторканна"
    assert not os.path.exists(victim)


def test_compact_store_without_limits_is_noop(tmp_path):
    base = str(tmp_path)
    now = time.time_ns()
    a = _make_session(base, now - 50 * DAY_NS, 500, compressed=False)
    b = _make_session(base, now - 2 * DAY_NS, 500)
    report = session.compact_store(base_dir=base)
    assert os.path.exists(a) and os.path.exists(b)
    assert report["removed"] == 0
    assert report["store_bytes"] > 0


def test_save_session_never_calls_compact_store(tmp_path, monkeypatch):
    import dupscan.domain.core as core

    def boom(*_args, **_kwargs):
        raise AssertionError("save_session не сміє прибирати сховище")

    monkeypatch.setattr(session, "compact_store", boom)
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a.bin").write_bytes(b"A" * 100)
    r = core.scan([str(tree)])
    path = session.save_session(
        r, r.scanned_roots, base_dir=str(tmp_path / "data"))
    assert path
