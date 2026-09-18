"""Ланцюг злиття через ті самі воркери, якими користується GUI, без вікна Main.

Виділено з tests/test_owner_scenario.py, щоб E2E на APFS (tmp_path) і E2E на
реальному exFAT-образі ганяли БУКВАЛЬНО той самий ланцюг. Якщо ці два прогони
розійдуться кодом, вони перестануть доводити паритет ФС — а саме заради
паритету матриця й існує.
"""

from __future__ import annotations

import dupscan.domain.core as core
import dupscan.ui.workers as workers


def run_scan(base) -> workers.ScanWorker:
    worker = workers.ScanWorker([str(base)])
    failed: list = []
    worker.failed.connect(failed.append)
    worker.run()
    assert not failed, f"ScanWorker не мав падати: {failed}"
    return worker


def run_pair_verification(dir_a: str, dir_b: str) -> core.ScanResult:
    worker = workers.PairVerificationWorker(dir_a, dir_b)
    results: list = []
    failed: list = []
    worker.done.connect(results.append)
    worker.failed.connect(failed.append)
    worker.run()
    assert not failed, f"PairVerificationWorker не мав падати: {failed}"
    assert len(results) == 1
    return results[0]


def run_merge_prep(result: core.ScanResult, src: str, dst: str):
    worker = workers.MergePreparationWorker(result, src, dst)
    results: list = []
    failed: list = []
    worker.done.connect(results.append)
    worker.failed.connect(failed.append)
    worker.run()
    assert not failed, f"MergePreparationWorker не мав падати: {failed}"
    assert len(results) == 1 and results[0] is not None
    return results[0]
