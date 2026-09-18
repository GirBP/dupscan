"""Червона команда для core.is_appledouble: чи може приховати від доказів
щось, що не є справжнім супутником AppleDouble. Три вектори:

B1. Тека з іменем «._X» (не файл): is_appledouble перевіряє лише ім'я,
    без урахування типу запису. У паралельному обході (scan_one_dir,
    вмикається на зовнішніх томах) перевірка типу запису йде до
    is_appledouble — інакше тека з таким іменем зникає з обходу цілком,
    разом з усім вмістом. У послідовному обході (walk_sequential), плані
    злиття (merge_plan) і гейті Кошика (_verify_then_trash_dir) перевірка
    й так стоїть лише на filenames з os.walk (dirnames не фільтруються) —
    там вектора нема; тести нижче доводять усі чотири точки.

B2. Файл «._secret» без сусіднього «secret»: is_appledouble бачить лише
    ім'я, тому гейт Кошика пропускає такий самотній файл без доказу
    копії лише коли в тому ж каталозі є байтовий сусід X для «._X» —
    інакше тека з унікальними даними під іменем супутника могла б
    поїхати в Кошик мовчки.

B3. NFD/NFC: сусідська перевірка з B2 навмисне байтова (без
    unicodedata.normalize) — інакше пошук шляху сам нормалізував би
    NFD/NFC докупи на цій ФС і замаскував би сироту. Тест фіксує цю
    (консервативну) поведінку, жодної «розумної» нормалізації не додано.

`core.snapshot_directory` і `core.snapshot_directory_state` (Merkle-докази
для TOCTOU-замка `fsops._verify_then_trash_dir` до/після) застосовують ті
самі два правила: перевіряють тип запису до is_appledouble, як
scan_one_dir (B1), і пропускають самотній супутник лише за байтовим
сусідом (core._appledouble_sibling_exists — тому й живе в core, а не у
fsops), як гейт Кошика (B2). Без цього тека «._X» чи самотній
«._secret» лишалися б невидимими обом знімкам, і мутація між ними
пройшла б непоміченою.
"""

import os
import sys
import tempfile
import unicodedata

os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dupscan.domain.core as core  # noqa: E402
import dupscan.infra.fsops as fsops  # noqa: E402


def _make(path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _no_op_trash(paths, **_kwargs):
    return []


# ================================ B1 ========================================


def test_b1_parallel_scan_must_see_directory_named_appledouble(tmp_path):
    """До фіксу: is_appledouble(e.name) у scan_one_dir спрацьовував ДО
    розрізнення файл/тека -> тека «._X» зникала цілком з паралельного
    обходу (walk_threads>1, реальний режим зовнішніх томів)."""
    root = tmp_path / "t"
    payload = os.urandom(4096)
    _make(root / "._X" / "dup.bin", payload)
    _make(root / "normal" / "dup.bin", payload)

    result = core.scan([str(root)], walk_threads=8)  # форсує паралельний walk

    inside = str(root / "._X" / "dup.bin")
    assert inside in result.file_meta, (
        "скан не побачив файл усередині теки «._X» (паралельний обхід)")
    assert result.file_groups and len(result.file_groups[0].paths) == 2, (
        "обидва dup.bin мали утворити одну групу дублікатів")


def test_b1_sequential_scan_already_sees_directory_named_appledouble(tmp_path):
    """Регресійний якір: послідовний обхід (внутрішній диск) вектора не
    мав і до фіксу — walk_sequential фільтрує is_appledouble лише
    filenames, dirnames проходять крізь _excluded/excluded_by_profile."""
    root = tmp_path / "t"
    payload = os.urandom(4096)
    _make(root / "._X" / "dup.bin", payload)
    _make(root / "normal" / "dup.bin", payload)

    result = core.scan([str(root)], walk_threads=1)

    inside = str(root / "._X" / "dup.bin")
    assert inside in result.file_meta
    assert result.file_groups and len(result.file_groups[0].paths) == 2


def test_b1_merge_plan_includes_files_inside_appledouble_named_directory(tmp_path):
    """merge_plan робить власний os.walk і фільтрує лише filenames ->
    dirnames типу «._X» не виключаються, вектора нема; тест це фіксує."""
    source = tmp_path / "src"
    destination = tmp_path / "dst"
    source.mkdir()
    destination.mkdir()
    _make(source / "._X" / "keepme.bin", os.urandom(4096))

    result = core.scan([str(tmp_path)])
    plan, _total = core.merge_plan(result, str(source), str(destination))

    planned = {rel for _size, _src, rel in plan}
    assert os.path.join("._X", "keepme.bin") in planned, (
        f"файл усередині теки «._X» мусив бути в плані: {sorted(planned)}")


def test_b1_trash_gate_requires_proof_for_files_inside_appledouble_directory(
        tmp_path):
    """Гейт Кошика теж робить власний os.walk без фільтра dirnames ->
    файл усередині «._X» без доказу деінде МУСИТЬ заблокувати Кошик."""
    source = tmp_path / "copy"
    _make(source / "._X" / "f.bin", os.urandom(4096))  # копії деінде нема

    result = core.scan([str(tmp_path)])
    kind, bad = fsops._verify_then_trash_dir(
        result, str(source), trash=_no_op_trash)

    assert kind == "abort" and bad >= 1, (
        "файл усередині теки «._X» без доказу дубліката мав заблокувати Кошик")


def test_b1_trash_gate_passes_when_files_inside_appledouble_directory_are_proven(
        tmp_path):
    source = tmp_path / "copy"
    destination = tmp_path / "original"
    payload = os.urandom(4096)
    _make(source / "._X" / "f.bin", payload)
    _make(destination / "._X" / "f.bin", payload)

    result = core.scan([str(tmp_path)])
    kind, errors = fsops._verify_then_trash_dir(
        result, str(source), trash=_no_op_trash)

    assert (kind, errors) == ("ok", []), (
        "з доказаним дублікатом деінде тека «._X» має пройти гейт")


# ================================ B2 ========================================


def test_b2_lone_appledouble_named_file_without_sibling_blocks_trash(tmp_path):
    """Файл «._secret» БЕЗ сусіднього «secret» — унікальні дані під
    іменем, схожим на супутник. Гейт МАЄ вимагати для нього звичайний
    доказ (якого нема) і відмовити, а не мовчки пропустити за іменем."""
    source = tmp_path / "copy"
    _make(source / "._secret", os.urandom(4096))  # унікальний вміст, сусіда нема

    result = core.scan([str(tmp_path)])
    kind, bad = fsops._verify_then_trash_dir(
        result, str(source), trash=_no_op_trash)

    assert kind == "abort" and bad >= 1, (
        "самотній «._secret» без сусіда не повинен мовчки їхати в Кошик")


def test_b2_paired_appledouble_still_passes_like_2_19(tmp_path):
    """Пара secret+._secret — справжній супутник, поведінка 2.18/2.19
    жива: гейт не вимагає для «._secret» окремого доказу дубліката, лише
    щоб байтовий сусід «secret» був присутній у ТІЙ Ж теці."""
    source = tmp_path / "copy"
    destination = tmp_path / "original"
    payload = os.urandom(4096)
    _make(source / "secret", payload)
    _make(destination / "secret", payload)  # доказ для самого secret
    _make(source / "._secret", os.urandom(4096))  # супутник, вміст довільний

    result = core.scan([str(tmp_path)])
    kind, errors = fsops._verify_then_trash_dir(
        result, str(source), trash=_no_op_trash)

    assert (kind, errors) == ("ok", []), (
        "пара X+._X у тій самій теці мала пройти гейт без доказу для ._X")


# ================================ B3 ========================================


def test_b3_nfd_companion_next_to_nfc_original_requires_its_own_proof(tmp_path):
    """Реальний файл записаний як NFC («café.bin»), «супутник» іменований
    «._» + NFD-форма ТОГО САМОГО візуального імені. Байтова сусідська
    перевірка (B2) НЕ визнає їх парою — задокументована консервативна
    поведінка, не баг: неоднозначна пара -> вимога звичайного доказу ->
    копії деінде нема -> abort."""
    visual_name = "café.bin"
    nfc_name = unicodedata.normalize("NFC", visual_name)
    nfd_name = unicodedata.normalize("NFD", visual_name)
    assert nfc_name != nfd_name, "тест вимагає ФС, де NFC і NFD — різні байти"

    source = tmp_path / "copy"
    source.mkdir(parents=True)
    _make(source / nfc_name, os.urandom(2048))          # "X", NFC
    _make(source / ("._" + nfd_name), os.urandom(4096))  # "._X", NFD

    # Документуємо факт: це справді два РІЗНІ dentry на цій ФС, не один.
    actual_names = sorted(os.listdir(source))
    assert len(actual_names) == 2, (
        f"тест вимагає два різні записи (NFC та «._»+NFD): {actual_names}")

    result = core.scan([str(tmp_path)])
    kind, bad = fsops._verify_then_trash_dir(
        result, str(source), trash=_no_op_trash)

    assert kind == "abort" and bad >= 1, (
        "NFD-іменований супутник без байтового NFD-сусіда мусить вимагати доказу")


# ================================ G1 ========================================


def test_g1_snapshot_directory_sees_mutation_inside_appledouble_named_directory(
        tmp_path):
    """До фіксу: is_appledouble(entry.name) у snapshot_directory спрацьовував
    ДО розрізнення файл/тека -> тека «._X» і її вміст були невидимі
    Merkle-знімку. Мутація всередині такої теки МІЖ доказовим обходом і
    send2trash лишалась би непоміченою — саме той TOCTOU-замок, задля
    якого ця функція існує."""
    root = tmp_path / "t"
    _make(root / "._X" / "f.bin", b"original")
    before, _b, _n = core.snapshot_directory(str(root))

    _make(root / "._X" / "new.bin", b"added-after-first-snapshot")
    after, _b2, _n2 = core.snapshot_directory(str(root))

    assert before != after, (
        "додавання файла всередину теки «._X» мало змінити Merkle-знімок")


def test_g1_snapshot_directory_state_sees_mutation_inside_appledouble_named_directory(
        tmp_path):
    """Те саме для TOCTOU-замка гейту Кошика: fsops._verify_then_trash_dir
    бере знімки «до» і «після» саме через snapshot_directory_state."""
    root = tmp_path / "t"
    _make(root / "._X" / "f.bin", b"original")
    before, _b, _n = core.snapshot_directory_state(str(root))

    _make(root / "._X" / "new.bin", b"added-after-first-snapshot")
    after, _b2, _n2 = core.snapshot_directory_state(str(root))

    assert before != after, (
        "додавання файла всередину теки «._X» мало змінити TOCTOU-знімок")


def test_g1_snapshot_directory_sees_mutation_of_lone_appledouble_named_file(
        tmp_path):
    """Самотній «._secret» (без сусіда) — не супутник, а файл з даними.
    Зміна вмісту між знімками МАЄ бути видимою, а не мовчки ігнорованою."""
    root = tmp_path / "t"
    _make(root / "._secret", b"original-secret-content")
    before, _b, files_before = core.snapshot_directory(str(root))

    (root / "._secret").write_bytes(b"mutated-secret-content-of-other-length")
    after, _b2, files_after = core.snapshot_directory(str(root))

    assert before != after, (
        "зміна вмісту самотнього «._secret» мала змінити Merkle-знімок")
    assert files_before == files_after == 1, (
        "самотній «._secret» мав рахуватись як звичайний файл, а не 0")


def test_g1_snapshot_directory_state_sees_mutation_of_lone_appledouble_named_file(
        tmp_path):
    root = tmp_path / "t"
    _make(root / "._secret", b"original-secret-content")
    before, _b, files_before = core.snapshot_directory_state(str(root))

    (root / "._secret").write_bytes(b"mutated-secret-content-of-other-length")
    after, _b2, files_after = core.snapshot_directory_state(str(root))

    assert before != after, (
        "зміна identity самотнього «._secret» мала змінити TOCTOU-знімок")
    assert files_before == files_after == 1


def test_g1_real_companion_pair_still_excluded_from_both_snapshots(tmp_path):
    """Справжня пара X+«._X» (обидва файли, сусід f.bin існує в тій самій
    теці): «._f.bin» не входить у жоден знімок. macOS сама переписує
    супутники (напр. provenance) — TOCTOU-замок не повинен спрацьовувати
    на змінах, яких користувач не робив (той самий rationale, що в
    докстрингах цих двох функцій)."""
    root = tmp_path / "t"
    _make(root / "f.bin", b"real-file-content")
    _make(root / "._f.bin", os.urandom(4096))  # супутник; сусід f.bin є

    dir_before, _b1, files_before = core.snapshot_directory(str(root))
    state_before, _b2, state_files_before = core.snapshot_directory_state(str(root))

    # "macOS переписує супутник сам" — інший вміст, той самий факт: сусід є.
    (root / "._f.bin").write_bytes(os.urandom(4096))

    dir_after, _b3, files_after = core.snapshot_directory(str(root))
    state_after, _b4, state_files_after = core.snapshot_directory_state(str(root))

    assert dir_before == dir_after, (
        "зміна СПРАВЖНЬОГО супутника не мала вплинути на snapshot_directory")
    assert state_before == state_after, (
        "зміна СПРАВЖНЬОГО супутника не мала вплинути на snapshot_directory_state")
    assert files_before == files_after == 1
    assert state_files_before == state_files_after == 1
