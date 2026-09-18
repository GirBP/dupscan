"""Сесії як файли: meta-сайдкар (список без парсингу важкого payload),
міграція старих сесій, парне видалення/прунінг, експорт/імпорт."""

import gzip
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.domain.core as core  # noqa: E402
import dupscan.infra.session as session  # noqa: E402


def make(p, data: bytes):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def scanned(tmp_path) -> core.ScanResult:
    for top in ("A", "B"):
        make(tmp_path / "tree" / top / "x.bin", b"X" * 1000)
    return core.scan([str(tmp_path / "tree")])


def test_sidecar_written_and_list_ignores_payload(tmp_path):
    r = scanned(tmp_path)
    base = str(tmp_path / "data")
    p = session.save_session(r, [str(tmp_path / "tree")], base_dir=base)
    assert p and os.path.exists(p)
    assert p.endswith(".json.gz")
    assert open(p, "rb").read(2) == b"\x1f\x8b"
    meta_p = session._meta_path(p)
    assert os.path.exists(meta_p), "save_session мусить писати meta-сайдкар"
    # зіпсуємо payload: список мусить жити ЛИШЕ з сайдкара (без парсингу payload)
    with open(p, "wb") as fh:
        fh.write(("НЕ JSON" * 10).encode())
    metas = session.list_sessions(base_dir=base)
    assert len(metas) == 1
    assert metas[0]["counts"]["files"] == 1
    assert metas[0]["path"] == p


def test_legacy_payload_without_sidecar_migrates(tmp_path):
    r = scanned(tmp_path)
    base = str(tmp_path / "data")
    p = session.save_session(r, [str(tmp_path / "tree")], base_dir=base)
    meta_p = session._meta_path(p)
    os.remove(meta_p)  # симулюємо стару сесію без сайдкара
    metas = session.list_sessions(base_dir=base)
    assert len(metas) == 1 and metas[0]["counts"]["files"] == 1
    assert os.path.exists(meta_p), "list_sessions мусить домігрувати сайдкар"


def test_delete_session_removes_pair(tmp_path):
    r = scanned(tmp_path)
    base = str(tmp_path / "data")
    p = session.save_session(r, ["/roots"], base_dir=base)
    meta_p = session._meta_path(p)
    session.delete_session(p)
    assert not os.path.exists(p) and not os.path.exists(meta_p)


def test_prune_keeps_pairs(tmp_path):
    r = scanned(tmp_path)
    base = str(tmp_path / "data")
    paths = [session.save_session(r, ["/r"], base_dir=base) for _ in range(33)]
    sdir = os.path.dirname(paths[-1])
    payloads = [
        n
        for n in os.listdir(sdir)
        if session._is_payload(n)
    ]
    sidecars = [n for n in os.listdir(sdir) if n.endswith(".meta.json")]
    assert len(payloads) == 30, "прунінг лишає 30 payload-ів"
    assert len(sidecars) == 30, "сайдкари прунькаються ПАРНО з payload-ами"
    assert {
        next(
            payload for payload in payloads
            if session._meta_path(payload).endswith(n)
        )
        for n in sidecars
    } == set(payloads)


def test_refresh_save_pruning_preserves_loaded_source_snapshot(tmp_path):
    r = scanned(tmp_path)
    base = str(tmp_path / "data")
    paths = [
        session.save_session(r, ["/r"], base_dir=base)
        for _ in range(30)
    ]
    source = paths[0]

    current = session.save_session(
        r, ["/current"], base_dir=base, preserve_paths=(source,))

    assert current and os.path.isfile(current)
    assert os.path.isfile(source)
    assert os.path.isfile(session._meta_path(source))
    payloads = [
        name for name in os.listdir(os.path.dirname(current))
        if session._is_payload(name)
    ]
    assert len(payloads) == 31


def test_export_import_roundtrip(tmp_path):
    r = scanned(tmp_path)
    base = str(tmp_path / "data")
    p = session.save_session(r, [str(tmp_path / "tree")], base_dir=base)
    dst = str(tmp_path / "backup" / "моя-сесія.json")
    os.makedirs(os.path.dirname(dst))
    session.export_session(p, dst)
    assert os.path.exists(dst)
    session.delete_session(p)
    assert session.list_sessions(base_dir=base) == []
    newp = session.import_session(dst, base_dir=base)
    metas = session.list_sessions(base_dir=base)
    assert len(metas) == 1 and metas[0]["path"] == newp
    loaded = session.load_session(newp)
    assert sorted(tuple(sorted(g.paths)) for g in loaded.file_groups) == sorted(
        tuple(sorted(g.paths)) for g in r.file_groups
    )


def test_import_rejects_garbage(tmp_path):
    bad = tmp_path / "junk.json"
    bad.write_text(json.dumps({"hello": 1}))
    base = str(tmp_path / "data")
    try:
        session.import_session(str(bad), base_dir=base)
        raise AssertionError("імпорт сміття мусить кидати ValueError")
    except ValueError:
        pass
    assert session.list_sessions(base_dir=base) == []


def test_legacy_raw_json_still_loads_and_migrates_sidecar(tmp_path):
    r = scanned(tmp_path)
    base = str(tmp_path / "data")
    compressed = session.save_session(
        r, [str(tmp_path / "tree")], base_dir=base)
    data = session._read_validated(compressed)
    legacy = tmp_path / "data" / "sessions" / "100.json"
    legacy.write_text(json.dumps(data), encoding="utf-8")
    os.remove(compressed)
    os.remove(session._meta_path(compressed))

    metas = session.list_sessions(base_dir=base)
    assert [meta["path"] for meta in metas] == [str(legacy)]
    assert os.path.isfile(session._meta_path(str(legacy)))
    loaded = session.load_session(str(legacy))
    assert len(loaded.file_groups) == 1


def test_compressed_payload_has_decompressed_size_limit(tmp_path, monkeypatch):
    bomb = tmp_path / "bomb.dupscan"
    with gzip.open(bomb, "wb") as fh:
        fh.write(b" " * 4096)
    monkeypatch.setattr(session, "_MAX_SESSION_BYTES", 1024)

    try:
        session.import_session(str(bomb), base_dir=str(tmp_path / "data"))
        raise AssertionError("завеликий розпакований payload треба відхилити")
    except ValueError as error:
        assert "розпаковані" in str(error)


def test_failed_export_preserves_existing_destination(tmp_path, monkeypatch):
    r = scanned(tmp_path)
    p = session.save_session(
        r, [str(tmp_path / "tree")], base_dir=str(tmp_path / "data"))
    dst = tmp_path / "existing.dupscan"
    dst.write_bytes(b"KEEP")

    def fail_copy(source, target, length):
        target.write(b"PARTIAL")
        raise OSError("disk full")

    monkeypatch.setattr(session.shutil, "copyfileobj", fail_copy)
    try:
        session.export_session(p, str(dst))
        raise AssertionError("помилка export мала бути видима")
    except OSError:
        pass
    assert dst.read_bytes() == b"KEEP"
    assert not list(tmp_path.glob("existing.dupscan.*.tmp"))


def test_prune_also_bounds_total_bytes_and_preserves_source(tmp_path):
    sdir = tmp_path / "sessions"
    sdir.mkdir()
    paths = []
    for number in range(4):
        payload = sdir / f"{number}.json"
        payload.write_bytes(b"x" * 100)
        session._write_meta(str(payload), {"created_ns": number})
        paths.append(str(payload))

    session._prune_old(
        str(sdir), keep=30, preserve_paths=(paths[0],), max_bytes=250)

    remaining = [path for path in paths if os.path.exists(path)]
    assert paths[0] in remaining
    assert len(remaining) == 2


def test_failed_payload_publication_removes_previously_written_sidecar(
    tmp_path,
    monkeypatch,
):
    result = scanned(tmp_path)
    base = str(tmp_path / "data")

    def fail_payload(*_args, **_kwargs):
        raise OSError("simulated ENOSPC after sidecar")

    monkeypatch.setattr(session, "_atomic_write_session", fail_payload)
    assert session.save_session(
        result,
        [str(tmp_path / "tree")],
        base_dir=base,
    ) == ""
    sdir = tmp_path / "data" / "sessions"
    # .lock — сталий fcntl-лок сховища, не
    # артефакт ЦЬОГО збереження; перевірка стосується саме сирітського
    # payload/meta після невдалого запису.
    assert [p for p in sdir.iterdir() if p.name != ".lock"] == []
    assert any("не вдалось зберегти сесію" in error for error in result.errors)


def test_session_save_streams_state_without_full_payload_dict(
    tmp_path,
    monkeypatch,
):
    result = scanned(tmp_path)
    real_dump = session.json.dump
    dumped_documents = []

    def guard_dump(value, *args, **kwargs):
        dumped_documents.append(value)
        assert not (
            isinstance(value, dict) and "state" in value
        ), "save_session must not build/dump one duplicate full payload"
        return real_dump(value, *args, **kwargs)

    monkeypatch.setattr(session.json, "dump", guard_dump)
    path = session.save_session(
        result,
        [str(tmp_path / "tree")],
        base_dir=str(tmp_path / "data"),
    )
    assert path
    assert dumped_documents and all(
        "state" not in value
        for value in dumped_documents
        if isinstance(value, dict)
    )
    loaded = session.load_session(path)
    assert len(loaded.file_groups) == len(result.file_groups)


def test_export_and_import_use_streaming_validator(
    tmp_path,
    monkeypatch,
):
    result = scanned(tmp_path)
    source = session.save_session(
        result,
        [str(tmp_path / "tree")],
        base_dir=str(tmp_path / "source-data"),
    )
    monkeypatch.setattr(
        session,
        "_read_validated",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("whole-buffer validator must not run")),
    )
    exported = tmp_path / "exported.dupscan"
    session.export_session(source, str(exported))
    imported = session.import_session(
        str(exported),
        base_dir=str(tmp_path / "import-data"),
    )
    assert session.load_session(imported).file_groups


def test_streaming_loader_rejects_invalid_counts(tmp_path):
    result = scanned(tmp_path)
    source = session.save_session(
        result,
        [str(tmp_path / "tree")],
        base_dir=str(tmp_path / "data"),
    )
    payload = session._read_validated(source)
    payload["counts"] = {"files": -1, "dirs": 0, "pairs": 0}
    malformed = tmp_path / "bad-counts.json"
    malformed.write_text(json.dumps(payload), encoding="utf-8")

    try:
        session.load_session(str(malformed))
        raise AssertionError("negative counts must be rejected")
    except ValueError as error:
        assert "статистика" in str(error)


# ---- Сесія з майбутньої версії ----------------------------------------


def test_future_version_session_gets_clear_rejection(tmp_path):
    """version=99 (з майбутньої версії застосунку) МАЄ дати зрозумілу
    відмову («новішої версії»), а не загальне «непідтримувана версія» і
    не crash — власник має зрозуміти, що треба оновити застосунок."""
    result = scanned(tmp_path)
    source = session.save_session(
        result, [str(tmp_path / "tree")], base_dir=str(tmp_path / "data"))
    payload = session._read_validated(source)
    payload["version"] = 99
    future = tmp_path / "future-version.json"
    future.write_text(json.dumps(payload), encoding="utf-8")

    try:
        session.load_session(str(future))
        raise AssertionError("сесія з майбутньої версії мусить бути відхилена")
    except ValueError as error:
        assert "новіш" in str(error).lower(), (
            f"повідомлення мусить пояснювати, що сесія новішої версії: {error}")


def test_future_version_with_unknown_field_fails_closed_not_silently(tmp_path):
    """version=99 РАЗОМ з полем, якого ця версія не знає (правдоподібний
    знімок майбутнього формату) — так само чітка відмова-виняток, а НЕ
    crash і НЕ мовчазний частковий результат (повернене значення)."""
    result = scanned(tmp_path)
    source = session.save_session(
        result, [str(tmp_path / "tree")], base_dir=str(tmp_path / "data"))
    payload = session._read_validated(source)
    payload["version"] = 99
    payload["hypothetical_future_field"] = {"anything": [1, 2, 3]}
    future = tmp_path / "future-version-unknown-field.json"
    future.write_text(json.dumps(payload), encoding="utf-8")

    try:
        partial = session.load_session(str(future))
    except ValueError:
        pass  # чітка, очікувана відмова — єдиний прийнятний результат
    else:
        raise AssertionError(
            f"мовчазний частковий результат замість відмови: {partial}")


def test_legacy_migration_validator_gives_same_future_version_message(tmp_path):
    """_validate_payload — шлях міграції старих сесій без сайдкара в
    list_sessions — формулює відмову так само, як основний load_session."""
    result = scanned(tmp_path)
    source = session.save_session(
        result, [str(tmp_path / "tree")], base_dir=str(tmp_path / "data"))
    payload = session._read_validated(source)
    payload["version"] = 99

    try:
        session._validate_payload(payload)
        raise AssertionError("version=99 мусить бути відхилена і тут")
    except ValueError as error:
        assert "новіш" in str(error).lower()


def test_genuinely_old_v1_shaped_session_still_loads(tmp_path):
    """Регресійний якір: справжня v1-форма (з похідними class_paths/
    dir_files присутніми — на відміну від v3, який їх не пише) і далі
    читається; уточнення повідомлення для МАЙБУТНІХ версій її не чіпає.
    Форма — як у test_session.py::test_streaming_loader_rejects_
    unhashable_ignored_pair_as_value_error (єдиний спосіб чесно
    відрізнити v1/v2 від v3 — руками, save_session завжди пише v3)."""
    root = str(tmp_path / "root")
    payload = {
        "version": 1,
        "created_ns": 1,
        "roots": [root],
        "state": {
            "file_meta": {
                os.path.join(root, "a.bin"): [4096, 1_400_000_000_000_000_000,
                                               1_400_000_000_000_000_000],
                os.path.join(root, "b.bin"): [4096, 1_400_000_000_000_000_000,
                                               1_400_000_000_000_000_000],
            },
            "file_class": {
                os.path.join(root, "a.bin"): "4096:" + "a" * 64,
                os.path.join(root, "b.bin"): "4096:" + "a" * 64,
            },
            "class_size": {"4096:" + "a" * 64: 4096},
            "class_paths": {
                "4096:" + "a" * 64: [
                    os.path.join(root, "a.bin"), os.path.join(root, "b.bin")],
            },
            "dir_files": {
                root: [os.path.join(root, "a.bin"), os.path.join(root, "b.bin")],
            },
            "dir_children": {},
            "dir_links": {},
            "dir_aliases": {},
            "dir_ok": {root: True},
        },
    }
    path = tmp_path / "genuine-v1.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = session.load_session(str(path))
    assert loaded.file_groups, "справжня v1-сесія мусить читатися, як і раніше"
