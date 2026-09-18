#!/bin/zsh
# Повний прогін матриці файлових систем (hdiutil-образи).
set -euo pipefail
ROOT="${0:A:h:h}"
cd "$ROOT"
DUPSCAN_FS_MATRIX=1 "$ROOT/.venv/bin/python" -m pytest tests/test_fs_matrix.py -v "$@"
