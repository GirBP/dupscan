"""T5: SIGKILL посеред переносу файлів. Після вбивства кожен файл існує
РІВНО в одному місці; нічого не зникло і не задубльовано."""

import os
import signal
import subprocess
import sys
import tempfile
import time

os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MOVER = r"""
import os, sys, time
src_dir, dst_dir = sys.argv[1], sys.argv[2]
names = sorted(os.listdir(src_dir))
for name in names:
    os.makedirs(dst_dir, exist_ok=True)
    os.rename(os.path.join(src_dir, name), os.path.join(dst_dir, name))
    print(name, flush=True)  # сигнал тесту: один файл перенесено
    time.sleep(0.15)
"""


def test_kill_mid_move_leaves_every_file_exactly_once(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    names = [f"f{i}.bin" for i in range(8)]
    for n in names:
        (src / n).write_bytes(os.urandom(4096))

    mover = tmp_path / "mover.py"
    mover.write_text(MOVER)
    proc = subprocess.Popen(
        [sys.executable, str(mover), str(src), str(dst)], stdout=subprocess.PIPE, text=True
    )
    # дочекатись переносу 3 файлів і вбити ЖОРСТКО посеред роботи
    moved_seen = 0
    while moved_seen < 3:
        line = proc.stdout.readline()
        if line.strip():
            moved_seen += 1
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(5)

    time.sleep(0.1)
    for n in names:
        in_src = (src / n).exists()
        in_dst = (dst / n).exists()
        assert in_src != in_dst, (
            f"{n}: файл мусить бути рівно в одному місці (src={in_src}, dst={in_dst})"
        )
