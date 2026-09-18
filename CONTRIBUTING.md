# Участь у розробці DupScan

## Структура коду

DupScan — пакет під `src/` (src-layout): `src/dupscan/domain` (чисті
обчислення без Qt), `src/dupscan/infra` (файлова система, кеш, сесії,
оновлення, діагностика), `src/dupscan/ui` (усе, що імпортує PySide6).
Межа перевірна фактичними імпортами — модуль з `PySide6` не може лежати
в `domain` чи `infra`. `setuptools` знаходить пакет через
`[tool.setuptools.packages.find] where = ["src"]`; pytest бачить його
через `pythonpath = ["src"]`. Поділ на шари описаний у `docs/architecture.md`.

## Налаштування середовища

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
```

`requirements.lock` фіксує і runtime-залежності (PySide6, blake3, ijson,
Send2Trash), і інструменти тестів/збірки з CI (pytest, ruff, pyinstaller).

## Запуск застосунку

```bash
PYTHONPATH=src .venv/bin/python -m dupscan
```

## Тести

```bash
.venv/bin/pytest -q
```

Тести без дисплея запускаються через `QT_QPA_PLATFORM=offscreen`. Тести
файлової системи на реальних образах exFAT/HFS+ (`hdiutil`) пропускаються,
якщо не задано `DUPSCAN_FS_MATRIX=1`.

## Лінт

```bash
.venv/bin/ruff check .
```

## Перевірка типів

`requirements.lock` не містить mypy. Поставте його окремо, версію задає набір `dev` у `pyproject.toml`:

```bash
.venv/bin/pip install "mypy>=2.3,<3"
```

```bash
PYTHONPATH=src .venv/bin/mypy src/dupscan/ui/app.py src/dupscan/infra/cache.py \
  src/dupscan/domain/clusters.py src/dupscan/domain/core.py \
  src/dupscan/ui/clusters_tab_controller.py \
  src/dupscan/infra/devices.py src/dupscan/infra/diagnostics.py \
  src/dupscan/infra/fsops.py src/dupscan/ui/table_models.py \
  src/dupscan/ui/perceptual.py src/dupscan/ui/perceptual_tab_controller.py \
  src/dupscan/infra/preferences.py \
  src/dupscan/domain/product.py src/dupscan/ui/problems_tab_controller.py \
  src/dupscan/infra/removal_history.py \
  src/dupscan/ui/folder_comparison_controller.py \
  src/dupscan/ui/removal_history_controller.py \
  src/dupscan/infra/reports.py src/dupscan/ui/reports_controller.py \
  src/dupscan/ui/scale_ui.py src/dupscan/ui/settings_controller.py \
  src/dupscan/infra/session.py src/dupscan/infra/storage_guard.py \
  src/dupscan/infra/throttle.py src/dupscan/infra/updates.py \
  src/dupscan/version.py src/dupscan/format.py src/dupscan/ui/workers.py \
  src/dupscan/ui/workflow_ui.py
```

## Інваріанти безпеки видалення (без винятків)

DupScan ніколи не видаляє файли користувача безповоротно. Видалення
доведених дублікатів іде тільки через `to_trash()` (`fsops.py`) і мусить
зберігати все нижченаведене. (Внутрішні `os.unlink` в `fsops.py` для
тимчасових заглушок під час publish/merge — окрема, не user-facing
відповідальність і цього інваріанту не порушують.)

1. **Тільки Кошик.** `to_trash()` — єдиний шлях видалення файлів
   користувача; прямого `os.remove`/`shutil.rmtree` для них у коді немає.
2. **Доказ перед видаленням.** Перед відправкою файла в Кошик його поточний
   стан на диску мусить збігатися з тим, що записав скан, і перевірена
   копія-переможець мусить уже існувати. Файл, що не пройшов цю перевірку,
   перериває видалення всієї теки, а не пропускається мовчки.
3. **Незалежне повторне читання.** Перевірка перед видаленням читає файл з
   диска заново в момент видалення, а не покладається на дані зі скану.
4. **Fail-closed на ненадійних файлових системах.** На файлових системах, де
   метадані часу зміни ненадійні (exFAT та інші поза списком `apfs`/`hfs`),
   короткий шлях за метаданими вимкнений і завжди виконується повна
   перевірка.

Зміна, що послаблює ці гарантії, не приймається. Додавайте або оновлюйте
тести в `tests/`, коли торкаєтесь логіки видалення в `fsops.py` чи `core.py`.
