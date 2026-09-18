"""Паралельний обхід (8 потоків): семантичний паритет із послідовним еталоном,
пауза/скасування, захист від повторного відвідування при вкладених коренях."""

import os
import sys
import threading
import time
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.domain.core as core  # noqa: E402


def make(p, data: bytes):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def build_tricky_tree(tmp_path):
    """Дерево з усіма пастками: дублікати файлів/тек, часткове перекриття,
    symlink-и, hardlink-и, бандл, виключення, юнікод NFC/NFD, порожня тека."""
    for top in ("A", "B"):  # дублікати тек (із вкладеністю)
        make(tmp_path / top / "doc.txt", b"D" * 500)
        make(tmp_path / top / "sub/img.bin", b"I" * 2000)
    make(tmp_path / "C/shared.bin", b"S" * 4000)  # часткове перекриття C~D
    make(tmp_path / "C/own1.bin", b"1" * 3000)
    make(tmp_path / "D/shared.bin", b"S" * 4000)
    make(tmp_path / "D/own2.bin", b"2" * 5000)
    make(tmp_path / "E/deep/deeper/u.bin", b"u" * 123)  # унікальний у глибині
    (tmp_path / "E/empty").mkdir(parents=True)  # порожня тека
    os.symlink(tmp_path / "A/doc.txt", tmp_path / "A/link_file")
    os.symlink(tmp_path / "B", tmp_path / "link_dir")
    make(tmp_path / "hl1.bin", b"H" * 1000)  # hardlink-сім'я: 1 фізичне тіло
    os.link(tmp_path / "hl1.bin", tmp_path / "hl2.bin")
    make(tmp_path / "Thing.app/Contents/res.bin", b"R" * 700)  # бандл
    make(tmp_path / "node_modules/junk.bin", b"J" * 600)  # виключення
    nfc = unicodedata.normalize("NFC", "кафе́.bin")
    nfd = unicodedata.normalize("NFD", "кафе́.bin")
    make(tmp_path / "U1" / nfc, b"K" * 900)  # той самий вміст, інша нормалізація
    make(tmp_path / "U2" / nfd, b"K" * 900)
    return tmp_path


def snap(r: core.ScanResult) -> dict:
    """Семантичний знімок результату (без внутрішньої нумерації u:N)."""
    return {
        "files_seen": r.files_seen,
        "bytes_seen": r.bytes_seen,
        "partial": r.partial,
        "file_groups": sorted((g.size, tuple(sorted(g.paths))) for g in r.file_groups),
        "dir_groups": sorted(
            (g.size, g.n_files, tuple(sorted(g.paths))) for g in r.dir_groups
        ),
        "sim_pairs": sorted(
            (p.dir_a, p.dir_b, p.percent, p.shared_bytes, tuple(sorted(p.shared)))
            for p in r.sim_pairs
        ),
        "file_meta": sorted(r.file_meta),
        "dir_files": {d: sorted(v) for d, v in r.dir_files.items()},
        "dir_children": {d: sorted(v) for d, v in r.dir_children.items()},
        "dir_ok": dict(r.dir_ok),
        "errors": sorted(r.errors),
    }


def test_parallel_matches_sequential_semantics(tmp_path):
    root = build_tricky_tree(tmp_path)
    seq = core.scan([str(root)], walk_threads=1)
    par = core.scan([str(root)], walk_threads=8)
    assert snap(seq) == snap(par)
    # і санітарні очікування від самого дерева:
    dir_sets = [set(map(os.path.basename, g.paths)) for g in par.dir_groups]
    # A має додатковий symlink, тому повний маніфест правильно відрізняє A/B.
    assert {"A", "B"} not in dir_sets
    assert {"U1", "U2"} in dir_sets  # NFC/NFD-нормалізація працює
    assert any(
        {os.path.basename(p.dir_a), os.path.basename(p.dir_b)} == {"C", "D"}
        for p in par.sim_pairs
    )
    meta = "\n".join(par.file_meta)
    assert "Thing.app" in meta and "node_modules" not in meta
    assert "link_file" not in meta and "link_dir" not in meta


def test_parallel_unreadable_poisons_like_sequential(tmp_path):
    for top in ("A", "B"):
        make(tmp_path / top / "x.bin", b"X" * 800)
    make(tmp_path / "A/secret/inner.bin", b"S" * 500)
    os.chmod(tmp_path / "A/secret", 0o000)
    try:
        seq = core.scan([str(tmp_path)], walk_threads=1)
        par = core.scan([str(tmp_path)], walk_threads=8)
        assert snap(seq) == snap(par)
        names = [{os.path.basename(p) for p in g.paths} for g in par.dir_groups]
        assert {"A", "B"} not in names  # A недоказова — ніколи не дублікат
        assert par.errors  # помилка доступу зафіксована, а не проковтнута
    finally:
        os.chmod(tmp_path / "A/secret", 0o755)


def test_parallel_cancel_preset_stops(tmp_path):
    for i in range(30):
        make(tmp_path / f"d{i}" / "f.bin", bytes([i % 251]) * 5000)
    ev = threading.Event()
    ev.set()
    r = core.scan([str(tmp_path)], cancel=ev, walk_threads=8)
    assert r.partial is True
    assert r.file_groups == [] and r.dir_groups == []


def test_parallel_pause_blocks_walk(tmp_path):
    for i in range(40):
        make(tmp_path / f"d{i}" / "a.bin", b"A" * 1000)
        make(tmp_path / f"d{i}" / "b.bin", b"B" * 1000)
    pause = threading.Event()
    pause.set()  # пауза ще ДО старту — перша ж хвиля мусить завмерти
    out: list[core.ScanResult] = []
    t = threading.Thread(
        target=lambda: out.append(
            core.scan([str(tmp_path)], pause=pause, walk_threads=8)
        )
    )
    t.start()
    time.sleep(0.3)
    assert t.is_alive() and not out  # обхід заморожений, результату нема
    pause.clear()
    t.join(30)
    assert not t.is_alive()
    plain = core.scan([str(tmp_path)], walk_threads=8)
    assert snap(out[0]) == snap(plain)  # пауза не змінила результат


def test_is_external_gate(monkeypatch):
    assert core._is_external("/Volumes/L/Фото") is True
    assert core._is_external("/Users/alice/Desktop") is False
    assert core._is_external("/tmp/x") is False
    # завантажувальний том: /Volumes/Macintosh HD -> симлінк на "/"
    monkeypatch.setattr(core.os.path, "islink", lambda p: p == "/Volumes/Boot")
    monkeypatch.setattr(
        core.os.path, "realpath", lambda p: "/" if p == "/Volumes/Boot" else p
    )
    assert core._is_external("/Volumes/Boot/Users") is False


def test_auto_gate_picks_walker(tmp_path, monkeypatch):
    make(tmp_path / "a.bin", b"A" * 100)
    walked = []
    real_walk = core.os.walk

    def spy_walk(*a, **k):
        walked.append(1)
        return real_walk(*a, **k)

    monkeypatch.setattr(core.os, "walk", spy_walk)
    core.scan([str(tmp_path)])  # tmp = внутрішній диск -> послідовний
    assert walked
    walked.clear()
    monkeypatch.setattr(core, "_is_external", lambda p: True)
    core.scan([str(tmp_path)])  # «зовнішній» -> паралельний, без os.walk
    assert not walked


def test_nested_roots_no_duplicate_children(tmp_path):
    make(tmp_path / "A/x.bin", b"X" * 1000)
    make(tmp_path / "A/sub/y.bin", b"Y" * 2000)
    make(tmp_path / "B/z.bin", b"Z" * 3000)
    r = core.scan([str(tmp_path), str(tmp_path / "A")], walk_threads=8)
    for d, subs in r.dir_children.items():
        assert len(subs) == len(set(subs)), f"дубльовані діти в {d}"
    assert r.files_seen == 3  # файли не пораховано двічі
