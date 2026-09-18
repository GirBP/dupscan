"""AdaptiveConcurrency + інтеграція в core.scan.

control_step()-тести адаптовано з DupFinder tests/test_throttle.py
(детерміновано, без реального wall-clock тіку — той самий підхід
джерела: "Kept separate from run so tests can drive it deterministically").
Інтеграційні тести (гейт бере в дужки САМЕ фізичне читання, а не
хешування/стат; результати ідентичні з/без) адаптовано з підходу
DupFinder tests/test_scanner.py::test_hash_group_gate_bounds_reads_but_not_cpu_hashing
до фактичної форми core.scan (яка, на відміну від dupfinder, не розділяє
читання й хешування на фази — див. коментар у core.py, hash_stage.work).
"""

import os
import sys
import tempfile
import threading
import time

os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dupscan.domain.core as core  # noqa: E402
import dupscan.infra.throttle as throttle  # noqa: E402


def _adaptive(cap=8, start=2):
    return throttle.AdaptiveConcurrency(cap, throttle.DiskActivity(), start=start)


# --------------------------------------------------------------------------- #
# control_step — детермінований, без реального тіку
# --------------------------------------------------------------------------- #
def test_adaptive_climbs_while_throughput_improves():
    """Кожна проба +1, що дає >=5% пропускної здатності, лишається — підйом
    триває (випадок тисяч малих файлів: зайві читачі ховають затримку)."""
    a = _adaptive(cap=8, start=2)
    thr = 100.0
    for _ in range(12):
        before = a.limit
        a.control_step(thr)
        if a.limit > before:
            thr *= 1.3
    assert a.limit == a.cap


def test_adaptive_concurrency_does_not_grow_under_slow_flat_reads():
    """Повільне фейк-читання, що не поліпшується (той самий рівень щоразу)
    -> кожна проба відкочується, межа НЕ росте вище стартової."""
    a = _adaptive(cap=8, start=4)
    for _ in range(6):
        a.control_step(50.0)  # той самий "повільний" рівень щоразу
    assert a.limit <= 4


def test_adaptive_concurrency_grows_under_fast_improving_reads():
    """Швидке фейк-читання, що щоразу поліпшується -> межа росте, не
    знижується."""
    a = _adaptive(cap=8, start=2)
    thr = 1000.0
    for _ in range(6):
        a.control_step(thr)
        thr *= 1.2
    assert a.limit > 2


def test_adaptive_idle_ticks_hold_state():
    """Фаза обходу/пауза: байтів не рухалось -> control_step(None) -> без
    рішень, без дрейфу межі."""
    a = _adaptive(cap=8, start=3)
    for _ in range(10):
        a.control_step(None)
    assert a.limit == 3


def test_adaptive_bounds_always_respected():
    a = _adaptive(cap=2, start=1)
    for _ in range(20):
        a.control_step(1000.0)
        assert 1 <= a.limit <= a.cap


def test_adaptive_gate_serializes_readers_at_limit_one():
    a = _adaptive(cap=4, start=1)
    order: list[str] = []

    def reader(name: str):
        a.acquire()
        order.append(name + ":in")
        time.sleep(0.15)
        order.append(name + ":out")
        a.release()

    t1 = threading.Thread(target=reader, args=("a",))
    t2 = threading.Thread(target=reader, args=("b",))
    t1.start()
    time.sleep(0.05)
    t2.start()
    t1.join(timeout=3)
    t2.join(timeout=3)
    assert order in (
        ["a:in", "a:out", "b:in", "b:out"],
        ["b:in", "b:out", "a:in", "a:out"],
    )


def test_adaptive_acquire_lets_through_on_stop():
    """На cancel гейт не тримає потоки заручниками — has cancel сам
    зупиняє код хешування, це швидше."""
    a = _adaptive(cap=4, start=1)
    a.acquire()  # зайняти єдиний слот
    t0 = time.monotonic()
    a.acquire(should_stop=lambda: True)  # без stop-шляху тут був би дедлок
    assert time.monotonic() - t0 < 2.0


# --------------------------------------------------------------------------- #
# інтеграція в core.scan — гейт бере в дужки САМЕ фізичне читання
# --------------------------------------------------------------------------- #
def test_scan_gate_bounds_concurrent_full_reads(tmp_path, monkeypatch):
    """limit=1 не пускає більше одного одночасного full-читання під час
    core.scan, попри пул із кількома потоками (parallel_files > 1).

    Файли МУСЯТЬ бути справжніми дублікатами (однаковий вміст): унікальні
    файли відсіюються ще на пробному читанні й ніколи не доходять до
    full-фази (де саме й діє гейт)."""
    for i in range(6):
        content = bytes([i % 2]) * (200 * 1024)  # > PARTIAL; 2 групи по 3
        (tmp_path / f"f{i}.bin").write_bytes(content)

    activity = throttle.DiskActivity()
    adaptive = throttle.AdaptiveConcurrency(4, activity, start=1)

    lock = threading.Lock()
    state = {"active": 0, "peak": 0}
    real_acquire, real_release = adaptive.acquire, adaptive.release

    def tracking_acquire(*a, **kw):
        real_acquire(*a, **kw)
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])

    def tracking_release():
        with lock:
            state["active"] -= 1
        real_release()

    monkeypatch.setattr(adaptive, "acquire", tracking_acquire)
    monkeypatch.setattr(adaptive, "release", tracking_release)

    real_hash_file = core._hash_file

    def slow_hash_file(*a, **kw):
        time.sleep(0.03)  # досить довго, щоб перекриття було спостережним
        return real_hash_file(*a, **kw)

    monkeypatch.setattr(core, "_hash_file", slow_hash_file)

    result = core.scan([str(tmp_path)], adaptive=adaptive, activity=activity)

    assert not result.errors
    assert state["peak"] == 1, f"гейт limit=1 порушено: peak={state['peak']}"
    assert activity.bytes > 0, "DiskActivity не отримав жодного байта"


def test_scan_gate_does_not_throttle_stat_or_walk(tmp_path, monkeypatch):
    """acquire() кличеться лише стільки разів, скільки файлів пройшло
    full-фазу — жодного разу за час обходу/стату дерева."""
    for i in range(4):
        content = bytes([i % 2]) * (200 * 1024)  # 2 групи по 2 — усі full-хешуються
        (tmp_path / f"g{i}.bin").write_bytes(content)

    activity = throttle.DiskActivity()
    adaptive = throttle.AdaptiveConcurrency(4, activity, start=4)
    calls = {"n": 0}
    real_acquire = adaptive.acquire

    def counting_acquire(*a, **kw):
        calls["n"] += 1
        real_acquire(*a, **kw)

    monkeypatch.setattr(adaptive, "acquire", counting_acquire)

    result = core.scan([str(tmp_path)], adaptive=adaptive, activity=activity)

    assert not result.errors
    assert calls["n"] == 4, f"очікувалось рівно 4 acquire (по файлу), маємо {calls['n']}"


def test_scan_results_identical_with_and_without_adaptive_gate(tmp_path):
    """Гейт лише сповільнює — WHAT читається і фінальний результат не
    міняється жодним байтом."""
    root = tmp_path / "tree"
    root.mkdir()
    for i in range(9):
        (root / f"f{i}.bin").write_bytes(f"content-{i % 3}-".encode() * 20000)

    plain = core.scan([str(root)])

    activity = throttle.DiskActivity()
    adaptive = throttle.AdaptiveConcurrency(4, activity, start=2)
    throttled = core.scan([str(root)], adaptive=adaptive, activity=activity)

    assert plain.file_class == throttled.file_class
    assert plain.class_paths == throttled.class_paths
    assert plain.class_size == throttled.class_size
    assert plain.errors == throttled.errors
    assert plain.files_seen == throttled.files_seen


# --------------------------------------------------------------------------- #
# температура — best-effort, ніколи не кидає
# --------------------------------------------------------------------------- #
def test_read_disk_temperature_none_when_smartctl_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(throttle, "_smartctl_path", lambda: None)
    assert throttle.read_disk_temperature(str(tmp_path)) is None


def test_temperature_governor_sample_handles_no_readable_device(monkeypatch, tmp_path):
    monkeypatch.setattr(throttle, "read_disk_temperature", lambda p, should_stop=None: None)
    gov = throttle.TemperatureGovernor([str(tmp_path)], cap_c=70.0)
    assert gov.sample() is None
    assert gov.too_hot is False
    assert gov.last_temp is None


def test_temperature_governor_toggles_too_hot_with_hysteresis(monkeypatch, tmp_path):
    readings = iter([75.0, 74.0, 71.0, 66.0])
    monkeypatch.setattr(
        throttle, "read_disk_temperature",
        lambda p, should_stop=None: next(readings))
    gov = throttle.TemperatureGovernor([str(tmp_path)], cap_c=70.0)
    assert gov.sample() == 75.0 and gov.too_hot is True
    assert gov.sample() == 74.0 and gov.too_hot is True  # ще над cap - hysteresis
    assert gov.sample() == 71.0 and gov.too_hot is True  # 71 > 70-3=67
    assert gov.sample() == 66.0 and gov.too_hot is False  # <= 67: охололо
