# DupScan

Локальний macOS-застосунок для безпечного очищення великих робочих архівів,
фототек і зовнішніх дисків. Він знаходить точні дублікати файлів, повністю
однакові папки та папки зі спільним вмістом. На відміну від «очищувачів в
один клік», DupScan показує доказ збігу й повторно читає незалежну копію
безпосередньо перед переміщенням у Кошик.

## Можливості

- каскад розмір → частковий → повний BLAKE3 із захистом від зміни файла;
- подібні папки з порогом, preview різниці, перевіреним copy або safe move;
- «Кластери тек» і «Схожі фото (підказка)» як окремі вкладки;
- окрема вкладка «Проблеми» з точною діагностикою (fskit-фантоми на exFAT
  тощо) і, де можливо, локальним полагодженням імені;
- інтелектуальний рескан завантаженої сесії: повторно використовує лише
  перевірені BLAKE3 незмінених файлів, хешує тільки нові або змінені;
- журнал Кошика з консервативним відновленням без перезапису;
- CSV/HTML-звіти й приватний діагностичний ZIP без вмісту сканованих файлів;
- перевірка оновлень лише вручну, обмежений HTTPS JSON-маніфест
  (`docs/UPDATE_CHANNEL.md`).

Повніше — у `CHANGELOG.md` (по версіях) і `docs/architecture.md` (шари,
потік сканування, межі довіри під час видалення).

## Вимоги для збірки

- macOS, Python 3.12–3.14;
- залежності з `requirements.lock` (PySide6, blake3, ijson, Send2Trash;
  для збірки й тестів додатково pytest, ruff, pyinstaller; mypy ставиться окремо,
  див. `CONTRIBUTING.md`).

## Збірка, запуск, тести

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
PYTHONPATH=src .venv/bin/python -m dupscan   # запуск з джерела
.venv/bin/pytest -q                          # тести (pythonpath=src у pyproject.toml)
.venv/bin/ruff check .                       # лінт
```

Частина тестів файлової системи (exFAT-образи через `hdiutil`) вимкнена за
замовчуванням і вмикається `DUPSCAN_FS_MATRIX=1`. Запуск без дисплея
(наприклад CI): `QT_QPA_PLATFORM=offscreen`.

macOS `.app` і DMG:

```bash
scripts/build_macos.sh
```

Скрипт запускає тести й ruff, збирає `.app` через PyInstaller
(`packaging/DupScan.spec`), виконує smoke test, підписує і пакує DMG.
Локальна збірка без додаткових змінних середовища має ad-hoc підпис. Для
публічного розповсюдження:

```bash
DUPSCAN_SIGN_IDENTITY="Developer ID Application: …" \
DUPSCAN_NOTARY_PROFILE="dupscan-notary" \
DUPSCAN_UPDATE_MANIFEST_URL="https://updates.example/dupscan.json" \
DUPSCAN_SUPPORT_URL="https://support.example/dupscan" \
DUPSCAN_REQUIRE_DEVELOPER_ID=1 \
DUPSCAN_REQUIRE_NOTARIZATION=1 \
DUPSCAN_REQUIRE_PRODUCTION_ENDPOINTS=1 \
scripts/build_macos.sh
```

`DUPSCAN_TARGET_ARCH=universal2` вмикає universal-збірку, якщо Python і всі
бінарні залежності також universal2.

## Структура коду

```
.
├── src/dupscan/  — пакет застосунку (src-layout)
│   ├── domain/   — чисті обчислення без Qt: скан, кластери, класифікація продукту
│   ├── infra/    — файлова система, кеш, сесії, оновлення, діагностика
│   ├── ui/       — усе, що імпортує PySide6: вікно, моделі подання, воркери
│   ├── version.py    — метадані випуску
│   └── __main__.py   — точка входу (`python -m dupscan`)
├── docs/         — архітектура, приватність, ліцензії третіх сторін, чекліст релізу
├── assets/       — іконка застосунку
├── licenses/     — повні тексти ліцензій пакованих залежностей
├── packaging/    — PyInstaller spec і entitlements для macOS-збірки
├── qa/           — допоміжні скрипти для ручної перевірки (образи ФС, бенчмарки, воркери гонитви)
├── scripts/      — збірка, пакування, перевірка залежностей і release endpoints
└── tests/        — pytest-набір
```

Межа шарів (`domain`/`infra`/`ui`) описана в `docs/architecture.md`.

## Відомі обмеження

- Немає universal2-збірки за замовчуванням; лише `arm64`, доки не задано
  `DUPSCAN_TARGET_ARCH=universal2`.
- exFAT-специфічні тести (`DUPSCAN_FS_MATRIX=1`) вимагають `hdiutil` і не
  запускаються автоматично.
- Публічна DMG потребує окремого Developer ID підпису й нотаризації —
  локальна збірка лишається ad-hoc.
- Production update/support endpoints задаються власником випуску; без
  них перевірка оновлень і кнопка підтримки використовують лише
  development-значення з середовища.
