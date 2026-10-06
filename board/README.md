# Дошка проєкту

Локальна канбан-дошка: власник рухає картки на сторінці, агент змінює їх командами `board`. Python 3.12 (за кодом має
працювати від 3.8, не перевірялось), сторонніх пакетів немає. Як дошкою користується агент — скіл `skills/project-board/SKILL.md`.

| Що | Де |
|---|---|
| Дані | `BOARD_DIR` → `$TATET_WIKI/boards` → ті самі змінні з `env` у `settings.json` Claude Code → `project/llm-wiki/boards` за розкладкою; нічого — помилка з підказкою запустити установник |
| Сторінка | `http://127.0.0.1:$BOARD_PORT`, за замовчуванням порт 8790; сервер слухає тільки `127.0.0.1` |
| Команди агента | `board.py`; коротку команду `board` створює установник `scripts/setup.py` |
| Сервер | `server.py`, сторінка — `page/`, шаблон сторінки шляху — `templates/progress.html` |
| Журнал сервера | `board.log` у папці даних (більше 1 МБ — перейменовується в `board.log.1`) |

Запуск сервера: `board serve [--port N]` (або той самий код без обгортки — `python board/server.py`, на Windows без
вікна — `pythonw`, на macOS і Linux — `python3`). Порт — `--port`, інакше `BOARD_PORT`, інакше 8790. Другий екземпляр на зайнятому порту пише рядок
у журнал і виходить з кодом 0; папку даних не знайдено — код 1. Команди працюють і без сервера: вони пишуть у файли напряму.

Команди: `board --help` і `board <команда> --help` (list, show, add, move, comment, handoff, inbox, summary, verify,
verify-queue, import-pipeline, sync-pipeline, start, finish, fog, sprint, progress, serve). Коди виходу: 0 — успіх,
1 — відмова правил або перевірки, 2 — помилка аргументів.

Тести (unittest, працюють у тимчасових папках через `BOARD_DIR`, живих даних не чіпають):
`cd board && PYTHONIOENCODING=utf-8 python -m unittest discover -s tests`.
Інтерфейс і повідомлення дошки — російською, як в оригіналі; переклад — окрема задача.
