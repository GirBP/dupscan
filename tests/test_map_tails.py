"""Два дешеві хвости для рідкісних, але реальних сценаріїв злиття.

C1 (крок 5, «10 000 колізій імені»): _transfer_files уже мав `for...else:
raise FileExistsError(...)` на вичерпання суфікс-циклу — це БУВ
прийнятний GAP (код є, тесту нема), не бага. Тест доводить: цикл
СКІНЧЕННИЙ (не зависає) і помилка зрозуміла, без реальних 10 000
колізій — межу винесено в іменовану `fsops._MAX_SUFFIX_ATTEMPTS`
(рефактор без зміни поведінки) саме заради дешевого monkeypatch.

C2 (дерево L1, «кеш віддав digest»), два тести: (a) показує, ДЕ ризик —
core.scan довіряє cache.get_many за точною ідентичністю, без перечитування
(задокументований, свідомий ризик, не бага); (b) показує, ЧОМУ це не веде
до втрати даних — деструктивний ланцюг перед Кошиком на недовіреній ФС
завжди перечитує повністю і відмовляє на розбіжності. Чесна межа
зафіксована в докстринзі (b): на довіреній ФС з точним, незмінним збігом
ідентичності захист суто ймовірнісний (гранулярність ctime/ino), а не
безумовний — так само, як і на скані.
"""

import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dupscan.ui.app as app_mod  # noqa: E402
import dupscan.infra.cache as cache  # noqa: E402
import dupscan.domain.core as core  # noqa: E402
import dupscan.infra.fsops as fsops  # noqa: E402


def make(p, data: bytes) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


# ================================ C1 ========================================


def test_c1_suffix_retry_is_finite_and_fails_with_a_clear_message(
        tmp_path, monkeypatch):
    """Межа звужена до 3 (monkeypatch _MAX_SUFFIX_ATTEMPTS) — без 10 000
    реальних файлів. target уже займає base.bin, base (2).bin, base
    (3).bin, base (4).bin — усі суфікс-слоти, доступні воркеру, зайняті
    ІНШИМ вмістом, тож жоден не підходить ні як публікація, ні як
    «той самий вміст, пропустити»."""
    monkeypatch.setattr(fsops, "_MAX_SUFFIX_ATTEMPTS", 3)
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    make(source / "base.bin", b"unique-source-content")
    make(target / "base.bin", b"occupied-0")
    make(target / "base (2).bin", b"occupied-2")
    make(target / "base (3).bin", b"occupied-3")
    make(target / "base (4).bin", b"occupied-4")

    result = core.scan([str(source), str(target)])
    plan, _total = core.merge_plan(result, str(source), str(target))

    started = time.monotonic()
    copied, errors = app_mod._copy_files(result, plan, str(target))
    elapsed = time.monotonic() - started

    assert copied == [], "жодного вільного імені не було — нічого не публікується"
    assert errors and "не вдалося підібрати вільне ім" in errors[0], (
        f"мала бути чітка відмова про вичерпання суфіксів: {errors}")
    assert elapsed < 5, "цикл мусить бути скінченним, а не зависати"
    # Джерело й усі зайняті імена в цілі лишаються недоторканими.
    assert (source / "base.bin").read_bytes() == b"unique-source-content"
    assert (target / "base.bin").read_bytes() == b"occupied-0"
    assert (target / "base (2).bin").read_bytes() == b"occupied-2"
    assert (target / "base (3).bin").read_bytes() == b"occupied-3"
    assert (target / "base (4).bin").read_bytes() == b"occupied-4"


def test_c1_default_limit_unchanged_at_ten_thousand():
    """Регресія: рефактор на іменовану константу не змінив саме число."""
    assert fsops._MAX_SUFFIX_ATTEMPTS == 10_000


# ================================ C2 ========================================


def test_c2a_scan_trusts_a_poisoned_cache_entry_this_is_the_known_display_risk(
        tmp_path):
    """Крок 1: показати, ДЕ саме живе кеш-ризик. `core.scan` спершу пробує
    (kind="p"); файл ≤128 КіБ читається пробою ЦІЛКОМ, тож проба і є
    повним дайджестом (core.py, «Проба прочитала файл ЦІЛКОМ») — для
    МАЛИХ файлів kind="f" у кеші взагалі не консультується, лише
    kind="p". Пряме `put_many` НЕПРАВИЛЬНОГО digest під kind="p" для
    ДВОХ файлів із РІЗНИМ реальним вмістом (симуляція отруєння — сам
    механізм ЯК воно туди потрапляє поза темою) під їхню РЕАЛЬНУ
    ідентичність змушує скан повірити: обидва «влучають» в один клас і
    навіть формують ФАЛЬШИВУ групу дублікатів. Це ВЖЕ задокументований
    ризик («кеш віддав digest» -> OK, з приміткою
    D3) — тест лише фіксує його письмово, а не «виправляє» (виправлення
    = завжди повне читання на скані, що вбиває сенс кешу)."""
    victim = tmp_path / "t" / "A" / "f.bin"
    other = tmp_path / "t" / "B" / "f.bin"
    make(victim, os.urandom(4096))
    make(other, os.urandom(4096))  # РІЗНИЙ реальний вміст — не мали бути парою
    st_victim = os.lstat(victim)
    st_other = os.lstat(other)
    poisoned_digest = "f" * 64  # НЕ реальний digest жодного з двох файлів

    hc = cache.HashCache.open(base_dir=str(tmp_path / "hcache"))
    hc.put_many([
        (str(victim), "p", st_victim.st_size, st_victim.st_mtime_ns,
         st_victim.st_ctime_ns, st_victim.st_dev, st_victim.st_ino,
         poisoned_digest),
        (str(other), "p", st_other.st_size, st_other.st_mtime_ns,
         st_other.st_ctime_ns, st_other.st_dev, st_other.st_ino,
         poisoned_digest),
    ])
    hc.flush()

    result = core.scan([str(tmp_path / "t")], cache=hc)
    hc.close()

    expected_class = f"{st_victim.st_size}:{poisoned_digest}"
    assert result.file_class.get(str(victim)) == expected_class, (
        "скан мав повірити кешу за точною ідентичністю — саме це й "
        "означає рядок дерева «кеш віддав digest -> вірить лише при "
        "точному size+mtime+ctime+dev+ino»")
    assert result.file_class.get(str(other)) == expected_class
    assert any(
        {str(victim), str(other)} <= set(g.paths) for g in result.file_groups
    ), "отруєння МАЄ зʼєднати два насправді різні файли у фальшиву групу"


def test_c2b_pre_trash_gate_refuses_when_metadata_shortcut_is_untrusted(
        tmp_path, monkeypatch):
    """Крок 2: чому це НЕ веде до втрати даних. Деструктивний ланцюг
    (`_verified_survivor` -> `_proven_stat`) перед Кошиком на exFAT/
    невідомій/недовіреній ФС ЗАВЖДИ
    перечитує повністю, незалежно від того, звідки взявся клас —
    зі свіжого хешу чи з отруєного кешу, з (а) тесту вище. Симулюємо
    підсумок (а): ScanResult, де file_class несе НЕПРАВИЛЬНИЙ digest
    для реального вмісту жертви (як після отруєння + точний збіг
    ідентичності). На недовіреній ФС `_verified_survivor` МАЄ провалити
    жертву — фактичний BLAKE3 не збігається з тим, що записано в класі,
    тому видалення відмовляє (fail-closed), а не губить дані.

    Чесна межа (не приховую): на ДОВІРЕНІЙ ФС (APFS, _fs_trusts_metadata
    -> True) з ідентичним, незмінним stat shortcut спрацював би так само,
    як і на скані — захист там суто ймовірнісний: «гранулярність
    ctime/ino» робить точний збіг ідентичності
    для РІЗНОГО вмісту практично неможливим, а не неможливим за
    побудовою коду. Це задокументована, свідома межа, не дира цього
    тесту."""
    monkeypatch.setattr(fsops, "_fs_trusts_metadata", lambda _path: False)
    victim = tmp_path / "t" / "A" / "f.bin"
    make(victim, os.urandom(4096))
    st = os.lstat(victim)
    poisoned_digest = "f" * 64  # НЕ реальний digest вмісту f.bin
    class_id = f"{st.st_size}:{poisoned_digest}"

    poisoned_result = core.ScanResult(live=True)
    victim_path = str(victim)
    poisoned_result.file_class = {victim_path: class_id}
    poisoned_result.class_paths = {class_id: [victim_path]}
    poisoned_result.file_meta = {
        victim_path: core.FileInfo(
            victim_path, st.st_size, st.st_mtime_ns, 0, st.st_ctime_ns,
            st.st_dev, st.st_ino),
    }

    safe = fsops._verified_survivor(poisoned_result, victim_path, {victim_path})

    assert safe is None, (
        "отруєний клас МАЄ провалити повторне читання перед Кошиком — "
        "фактичний BLAKE3 не збігається з тим, що записано в class_id")
    assert victim.exists(), "жертва без підтвердженого доказу лишається на місці"
