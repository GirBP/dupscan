"""ETA-помічник статус-рядка довгих операцій.

ETA обчислюється ЛИШЕ з наявного потоку progress(phase, done, total), без
жодної зміни core.py/session.py/workers.py/perceptual.py: ProgressEta живе
в app.py і рахує суто арифметику над числами, які й так приходять у
Main.on_progress. Тести подають синтетичний потік через injectable-годинник
(FakeClock) — жодного time.sleep, жодного random/datetime.now, жодного
справжнього воркера чи диска.
"""

import builtins
import os
import sys
import tempfile
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication  # noqa: E402

import dupscan.ui.app as app_mod  # noqa: E402
from dupscan.format import human  # noqa: E402

_qapp = QApplication.instance() or QApplication([])


class FakeClock:
    """Детермінований лічильник часу замість time.monotonic — без sleep,
    без random. Тести самі керують плином часу через tick()."""

    def __init__(self, start: float = 1_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


def make_eta(start: float = 1_000.0):
    clock = FakeClock(start)
    return app_mod.ProgressEta(clock=clock), clock


# --------------------------------------------------------- _mib_phase_label
def test_mib_phase_label_detects_suffix_and_ignores_embedded_path():
    # "Повне хешування · МіБ" (core.scan) -- без шляху всередині
    assert app_mod._mib_phase_label("Повне хешування · МіБ") == "Повне хешування"
    # "Перевіряю перед злиттям · <шлях> · МіБ" (workers.MergePreparationWorker)
    # і "Знімок теки · <шлях> · МіБ" (fsops._verify_then_trash_dir) -- шлях
    # посередині ігнорується, підпис -- лише перший сегмент
    assert app_mod._mib_phase_label(
        "Перевіряю перед злиттям · /a/b/f.bin · МіБ") == "Перевіряю перед злиттям"
    assert app_mod._mib_phase_label(
        "Знімок теки · /Volumes/L/тека з пробілами · МіБ") == "Знімок теки"
    # НЕ байтові фази (штуки, не МіБ): "Обхід тек", "Порівняння початків"
    assert app_mod._mib_phase_label("Обхід тек") is None
    assert app_mod._mib_phase_label("Порівняння початків") is None
    assert app_mod._mib_phase_label("Готово") is None


# ------------------------------------------------------------ _format_remaining
def test_format_remaining_seconds_minutes_hours():
    assert app_mod._format_remaining(5) == "~5 с"
    assert app_mod._format_remaining(45) == "~45 с"
    assert app_mod._format_remaining(360) == "~6 хв"   # приклад із ТЗ: "~6 хв"
    assert app_mod._format_remaining(7200) == "~2.0 год"
    assert app_mod._format_remaining(-5) == "~1 с"  # захист від'ємного вводу


# ------------------------------------------------------- монотонність оцінки
def test_remaining_estimate_never_jumps_up_on_uniform_stream():
    """ЗАЛІЗНИЙ ІНВАРІАНТ: на рівномірному потоці залишок часу лише
    спадає (чи лишається на місці через округлення semples), ніколи не
    стрибає вгору -- інакше власник бачить "6 хв" потім раптом "9 хв"."""
    eta, clock = make_eta()
    total = 1000
    remaining_values = []
    for step in range(1, 40):
        clock.tick(0.5)  # рівний крок часу
        remaining = eta.update("Повне хешування · МіБ", step * 20, total)  # рівний крок прогресу
        if remaining is not None:
            remaining_values.append(remaining)
    assert len(remaining_values) >= 5, "оцінка мала з'явитися після кількох вимірів"
    for earlier, later in zip(remaining_values, remaining_values[1:]):
        assert later <= earlier + 1e-9, (
            f"залишок стрибнув вгору: {earlier} -> {later}")


# --------------------------------------------------- ховається до вибірки
def test_estimate_hidden_until_enough_samples_and_elapsed_time():
    """Перші секунди/виміри потоку -- оцінка ХОВАНА (без брехливих чисел).
    Требa і кількість вимірів (>=3), і час від початку потоку (>=1.5с)."""
    eta, clock = make_eta()
    # тік 1: єдиний вимір
    assert eta.update("Порівняння початків", 10, 100) is None
    clock.tick(0.5)
    # тік 2: два виміри -- все ще замало
    assert eta.update("Порівняння початків", 20, 100) is None
    clock.tick(0.5)
    # тік 3: вже три виміри, але від старту потоку лише 1.0с -- замало часу
    assert eta.update("Порівняння початків", 30, 100) is None
    clock.tick(1.0)
    # тік 4: і вимірів досить, і часу (2.0с) досить -- оцінка з'являється
    remaining = eta.update("Порівняння початків", 40, 100)
    assert remaining is not None


def test_render_hides_remaining_tail_on_first_ticks():
    eta, clock = make_eta()
    first = eta.render("Обхід тек", 10, 5000)
    assert "лишилось" not in first
    clock.tick(0.3)
    second = eta.render("Обхід тек", 20, 5000)
    assert "лишилось" not in second
    clock.tick(0.3)
    third = eta.render("Обхід тек", 30, 5000)
    assert "лишилось" not in third
    clock.tick(2.0)
    fourth = eta.render("Обхід тек", 500, 5000)
    assert "лишилось" in fourth


# ------------------------------------------------------- формат розміру/часу
def test_mib_phase_renders_human_size_matching_spec_example():
    """ТЗ: «Перевірено 4.2 з 18.4 ГіБ · лишилось ~6 хв» -- застосунок скрізь
    використовує format.human() (формат "ГБ", не "ГіБ"; те саме двійкове
    ділення на 1024 -- лише підпис одиниці інший, як і в усьому app.py)."""
    eta, clock = make_eta()
    total_mib = 18_841  # -> 18.4 ГБ через human()
    # накопичити виміри, щоб оцінка встигла з'явитися
    for done_mib in (500, 1500, 2800, 4300):
        clock.tick(1.0)
        text = eta.render("Повне хешування · МіБ", done_mib, total_mib)
    expected_prefix = (
        f"Повне хешування: {human(4300 * 1024 * 1024)} з "
        f"{human(total_mib * 1024 * 1024)}")
    assert expected_prefix == "Повне хешування: 4.2 ГБ з 18.4 ГБ"
    assert text.startswith(expected_prefix)
    assert "лишилось" in text


def test_count_phase_keeps_existing_done_of_total_format():
    """Не-МіБ фаза (файли/групи, не байти) -- формат лишається "фаза: done
    / total", як і до Блоку E; ETA лише ДОДАЄ хвіст, нічого не міняючи."""
    eta, clock = make_eta()
    for done in (10, 25, 40, 60):
        clock.tick(1.0)
        text = eta.render("Групую точні дублікати", done, 100)
    assert text.startswith("Групую точні дублікати: 60 / 100")


# ------------------------------------------------------- total=0 (невідомо)
def test_unknown_total_shows_only_done_without_estimate():
    eta, clock = make_eta()
    texts = []
    for done in (0, 5120, 9000, 15000):
        clock.tick(1.0)
        texts.append(eta.render("Обхід тек", done, 0))
    assert texts[0] == "Обхід тек"  # done=0 -- гола фаза, без числа
    assert texts[1] == "Обхід тек: 5120"
    assert texts[-1] == "Обхід тек: 15000"
    assert all("лишилось" not in t for t in texts)


def test_unknown_total_mib_phase_shows_only_done_size():
    eta, clock = make_eta()
    texts = []
    for done_mib in (10, 200, 900):
        clock.tick(1.0)
        texts.append(eta.render("Знімок теки · /tmp/x · МіБ", done_mib, 0))
    assert texts[-1] == f"Знімок теки: {human(900 * 1024 * 1024)}"
    assert all("лишилось" not in t for t in texts)


# ------------------------------------------------------------------ завершення
def test_completion_renders_clean_final_line_without_remaining():
    eta, clock = make_eta()
    total = 100
    text = ""
    for done in (10, 25, 40, 60):
        clock.tick(1.0)
        text = eta.render("Порівняння початків", done, total)
    assert "лишилось" in text  # оцінка встигла з'явитися до фінішу
    clock.tick(1.0)
    final = eta.render("Порівняння початків", total, total)
    assert final == f"Порівняння початків: {total} / {total}"
    assert "лишилось" not in final


def test_mib_completion_renders_clean_final_line():
    eta, clock = make_eta()
    total_mib = 500
    for done_mib in (50, 150, 300):
        clock.tick(1.0)
        eta.render("Повне хешування · МіБ", done_mib, total_mib)
    clock.tick(1.0)
    final = eta.render("Повне хешування · МіБ", total_mib, total_mib)
    assert final == (
        f"Повне хешування: {human(total_mib * 1024 * 1024)} з "
        f"{human(total_mib * 1024 * 1024)}")
    assert "лишилось" not in final


# ------------------------------- один потік попри мінливий текст МіБ-фази
def test_mib_stream_survives_per_file_path_and_verb_changes():
    """Регресійний захист: "Перевіряю перед злиттям · <шлях> · МіБ" і
    "Перевірено перед злиттям · <шлях> · МіБ" (workers.py,
    MergePreparationWorker) чергуються і несуть РІЗНИЙ шлях на кожному
    файлі. Наївне порівняння повного тексту фази скидало б вікно вимірів
    на кожному файлі -- оцінка ніколи не з'являлася б на довгій перевірці
    перед злиттям. Ключ потоку -- (МіБ-ознака, total), а не текст фази."""
    eta, clock = make_eta()
    total = 500  # MiB, стабільний для всієї операції
    done = 0
    remaining_seen = None
    for i in range(20):
        path = f"/src/f{i}.bin"
        clock.tick(0.2)
        done += 5
        eta.update(f"Перевіряю перед злиттям · {path} · МіБ", done, total)
        clock.tick(0.2)
        done += 5
        remaining = eta.update(f"Перевірено перед злиттям · {path} · МіБ", done, total)
        if remaining is not None:
            remaining_seen = remaining
    assert remaining_seen is not None, (
        "оцінка так і не з'явилась -- вікно скидається на кожному файлі")


def test_phase_change_resets_the_window():
    """Інший масштаб одиниць (штуки -> МіБ) -- новий потік: стара швидкість
    не тягнеться в нову оцінку, навіть якщо старий потік уже мав досить
    вимірів і часу для власної оцінки."""
    eta, clock = make_eta()
    for done in (200, 500, 900, 1400):
        clock.tick(1.0)
        eta.update("Порівняння початків", done, 5000)
    # старий потік уже готовий видавати оцінку -- переконаємось перед зміною фази
    assert eta.update("Порівняння початків", 1800, 5000) is not None
    clock.tick(0.2)
    # нова фаза, інший масштаб (штуки -> МіБ) -- перший тік нового потоку
    # має бути хованим, попри те що попередній потік вже мав достатньо
    # вимірів і часу
    first_in_new_phase = eta.update("Повне хешування · МіБ", 1, 500)
    assert first_in_new_phase is None


def test_total_change_within_same_unit_kind_resets_the_window():
    """Той самий тип фази (без "· МіБ"), але INШИЙ total -- це фактично
    інша операція (напр. нова перевірка з іншою кількістю файлів), а не
    продовження попередньої. Має скинути вікно вимірів."""
    eta, clock = make_eta()
    for done in (100, 300, 700, 1200):
        clock.tick(1.0)
        eta.update("Порівняння початків", done, 5000)
    assert eta.update("Порівняння початків", 1600, 5000) is not None
    clock.tick(0.2)
    # той самий текст фази, але ІНШИЙ total -- нова операція
    first_of_new_total = eta.update("Порівняння початків", 5, 900)
    assert first_of_new_total is None


def test_done_regression_forces_reset():
    """done, що впав нижче останнього виміру того самого потоку (той самий
    total), означає нову операцію -- не помилковий "негативний" прогрес."""
    eta, clock = make_eta()
    for done in (100, 200, 300, 400):
        clock.tick(0.5)
        eta.update("Порівняння початків", done, 5000)
    clock.tick(0.5)
    # done впав -- нова операція повторно використала ті самі total/фазу
    result = eta.update("Порівняння початків", 5, 5000)
    assert result is None  # щойно почався новий потік -- вимірів замало


# ---------------------------------------------------- нуль диска в GUI-потоці
def test_estimator_never_touches_disk(monkeypatch):
    """ETA -- ЛИШЕ арифметика над числами прогресу. Патчимо os.stat/os.open/
    open так, щоб будь-яке звернення до диска впало винятком -- і жоден з
    них не спрацьовує за весь синтетичний прогін.

    Конструюємо eta/clock ДО патчу і знімаємо патч у finally: інакше, якщо
    щось під патчем впаде НЕ через ProgressEta (напр. сам об'єкт іще не
    існує), звітність pytest теж потребує os.stat для власних службових
    цілей і крашиться разом із сесією замість чистого FAILED -- патч має
    жити рівно навколо коду, що перевіряється, і ніде більше."""

    eta, clock = make_eta()

    def boom(*_a, **_k):
        raise AssertionError("ProgressEta торкнувся диска -- заборонено в GUI-потоці")

    monkeypatch.setattr(os, "stat", boom)
    monkeypatch.setattr(os, "lstat", boom)
    monkeypatch.setattr(os, "open", boom)
    monkeypatch.setattr(os, "scandir", boom)
    monkeypatch.setattr(builtins, "open", boom)
    try:
        for i in range(1, 30):
            clock.tick(0.3)
            eta.render("Повне хешування · МіБ", i * 37, 1200)
            eta.render("Обхід тек", i * 12, 0)
    finally:
        monkeypatch.undo()


# -------------------------------------------- інтеграція: Main._render_progress
def _fake_worker() -> object:
    class FakeWorker:
        def __init__(self):
            self.pause = threading.Event()
            self.cancel = threading.Event()

        def isRunning(self):
            return True

    return FakeWorker()


def test_main_status_line_gains_eta_tail_via_on_progress():
    """Наскрізна перевірка: Main.on_progress -> _render_progress справді
    використовує ProgressEta (той самий об'єкт, self._eta), а не
    паралельний окремий шлях."""
    m = app_mod.Main()
    m.worker = _fake_worker()
    clock = FakeClock(2_000.0)
    m._eta = app_mod.ProgressEta(clock=clock)
    for done_mib in (500, 1200, 2000, 3200):
        clock.tick(1.0)
        m.on_progress("Повне хешування · МіБ", done_mib, 18_841)
    text = m.status.text()
    assert text.startswith("Повне хешування: ")
    assert " з " in text
    assert "лишилось" in text
    assert m.bar.maximum() == 18_841
    assert m.bar.value() == 3200


def test_main_new_scan_resets_eta_between_operations():
    """Новий скан (start_scan) не має тягнути швидкість/час старту від
    попередньої операції -- self._eta.reset() поруч із self._progress =
    None на кожному з відомих стартів операції."""
    m = app_mod.Main()
    m.worker = _fake_worker()
    clock = FakeClock(5_000.0)
    m._eta = app_mod.ProgressEta(clock=clock)
    for done_mib in (100, 400, 900, 1600):
        clock.tick(1.0)
        m.on_progress("Повне хешування · МіБ", done_mib, 10_000)
    assert "лишилось" in m.status.text()

    m._eta.reset()
    m._progress = None
    clock.tick(0.1)
    m._render_progress("Повне хешування · МіБ", 1, 10_000)
    assert "лишилось" not in m.status.text()
