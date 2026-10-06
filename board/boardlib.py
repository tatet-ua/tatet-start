"""boardlib — общие правила и хранилище канбан-доски проектов.

API для board.py и server.py
============================

Данные меняются ТОЛЬКО через функции этого модуля: и команды агента (роль «агент», у verify — «проверяющий»),
и локальная программа страницы (роль «владелец»). Порядок записи взят у прежнего сборщика доски-
артефакта (удалён 2026-09-24): все проверки →
временный файл рядом → os.replace, замена только при успехе. Формат сообщений об ошибке тот же:
"ОШИБКА  поле: что не так".

Константы
---------
STATUSES            ключ статуса → русское название (восемь: шесть колонок и два архивных)
COLUMNS             шесть колонок по порядку: backlog, new, in_progress, review, done, accepted
ARCHIVE             архив: postponed, cancelled
BACKLOG NEW IN_PROGRESS REVIEW DONE ACCEPTED POSTPONED CANCELLED  ключи статусов строками
ROLES               owner/agent/reviewer/owner_chat → владелец/агент/проверяющий/владелец в чате
OWNER AGENT REVIEWER OWNER_CHAT          ключи ролей строками
SPRINT_STATUSES     next/current/closing/passed → следующий/текущий/ждёт закрытия/пройден
SPRINT_NEXT SPRINT_CURRENT SPRINT_CLOSING SPRINT_PASSED  ключи статусов спринта строками
SPRINT_FIELDS       поля спринта в том порядке, в каком они лежат в файле
FOG_MAX             сколько строк «что видно в тумане» помещается в файл (5)
ACCEPTANCE          owner/reviewer → владелец/проверяющий (кто принимает карточку)
PRIORITIES          A/B/C → сейчас/следом/потом
TAGS                block/urgent → блокирует остальное/срочно
KINDS               stage/task → этап/задача
STAGES              номера этапов пайплайна: 1, 3, 4, 5, 6, 7, 8
STAGE_ACCEPTANCE    номер этапа → кто принимает по умолчанию
LOCK_TIMEOUT        сколько секунд ждать замок файла проекта
STALE_DAYS          через сколько дней карточка «В работе» считается залежавшейся (2)
CARD_FIELDS         поля карточки в том порядке, в каком они лежат в файле
PROJECT_FIELDS      поля файла проекта в том же смысле
UNSET               «поле не передали» для edit_card (None там значит «снять значение»)
PROJECT_NAME        белый список имени проекта: [a-z0-9][a-z0-9-]{0,63}
RESERVED_NAMES      имена, занятые в Windows под устройства (con, nul, com1…)
SESSION_LINK        вид ссылки на сессию: claude://claude.ai/epitaxy/<идентификатор>

Записи в карточке: history — {seq, at, who, from, to, text}; comments — {seq, at, who, text, move},
где move=true у комментария, написанного вместе с переносом (возврат на доработку): в ленте он
виден, а в inbox его заменяет само событие переноса.

Путь проекта (спека доски проекта 2026-09-24)
---------------------------------------------
В файле проекта перед cards лежат start, finish, sprints, fog, progress_url:
  start        {date: "ГГГГ-ММ-ДД", event: "спека утверждена"} или None
  finish       {text, approved, approved_at, history: [{seq, at, who, from, to, said, text}]} или None;
               to — "proposed" или "approved"
  sprints      [{id: "s1", title, done_when, status, proof, opened_at, closed_at,
                 history: [{seq, at, who, from, to, said, text}]}]
  fog          до FOG_MAX строк «что уже видно» за последним известным спринтом
  progress_url адрес опубликованной страницы пути
Статусы спринта: next → current → closing (ждёт закрытия) → passed. Текущий (current или closing)
не больше одного; id — s<порядковый номер>, спринты не удаляются. У карточки поле sprint — id
спринта или None; события спринтов и финиша идут в общий event_counter и видны в read_inbox.

Роль «владелец в чате» (OWNER_CHAT). Решения владельца о финише и спринтах приходят словом в чате:
approve_finish, open_sprint, close_sprint, set_start (повторно) и set_card_sprint у принятой
карточки принимают роль owner_chat только вместе с непустой цитатой said (текст владельца как
есть, до 300 знаков, textcheck по ключу owner_said). Без цитаты — ValidationError со словами
«решает владелец: нужна его цитата из чата». Роль agent эти действия не может — ForbiddenError.
В остальном owner_chat равен агенту: карточки двигает по диаграмме, текст проходит textcheck.
Роль owner (страница) остаётся: те же действия без цитаты, для второй очереди.

Исключения (все наследуют BoardError)
-------------------------------------
BoardError          база; .messages — список готовых строк для печати
ValidationError     не заполнено обязательное поле, негодное значение, текст не прошёл textcheck
ForbiddenError      роли это делать нельзя; текст — человеческая фраза по-русски
ConflictError       запись по устаревшей version; программа отвечает на неё 409, файл не тронут
LockTimeoutError    файл проекта занят другим процессом дольше LOCK_TIMEOUT
NotFoundError       нет такого проекта или карточки

Разбор значений
---------------
parse_status(v)     ключ или русское название (регистр и «_» не важны) → ключ статуса
parse_role(v)       то же для роли
parse_acceptance(v) то же для «кто принимает»
parse_priority(v)   A/B/C, «сейчас/следом/потом»
parse_tag(v)        block/urgent/пусто
parse_kind(v)       stage/task, «этап»/«задача»
status_title(k)     русское название статуса (и role_title, acceptance_title)

Хранилище
---------
Каталог данных везде передаётся необязательным аргументом boards_dir; без него — переменная окружения
BOARD_DIR, затем $TATET_WIKI/boards, затем env.BOARD_DIR или env.TATET_WIKI из settings.json Claude Code,
затем папка раскладки project/llm-wiki/boards; ничего нет — BoardError с подсказкой про установщик
(см. data_dir). Файл проекта — <проект>.json.

data_dir(boards_dir=None)                  каталог данных
project_path(project, boards_dir=None)     путь к файлу проекта
list_projects(boards_dir=None)             имена проектов (по файлам каталога данных)
read_project(project, boards_dir=None)     весь файл проекта копией (только чтение)
create_project(project, phrase, project_dir, boards_dir=None)  создать пустой файл проекта
state_token(boards_dir=None)               дешёвая отметка состояния; меняется при любой записи

Карточки
--------
get_card(project, card_id, ...)                 одна карточка копией
list_cards(project, status=None, ...)           карточки в порядке доски
find_stage_card(project, stage, ...)            карточка-этап по номеру этапа (или None)
add_card(project, role, title=..., ...)         создать карточку
move_card(project, card_id, to, role, ...)      сменить статус (ask/proof/accept_how/comment/caveat)
edit_card(project, card_id, role, ...)          title, body, priority, tag, acceptance
reorder_card(project, card_id, role, ...)       порядок в колонке и приоритет
comment_card(project, card_id, role, text, ...) комментарий в ленту
handoff(project, card_id, role, ...)            stopped_at, resume, session — без смены статуса
delete_card(project, card_id, role, ...)        убрать карточку (только владелец); номер не вернётся
can_move(card, to, role)                        (можно, причина отказа) — без записи
set_card_sprint(project, card_id, role, sprint_id, version=None, said=None)  привязать к спринту

Приёмка проверяющим (спека 2026-09-29)
--------------------------------------
verify_card(project, card_id, verdict, command, output, comment, version)  ok → «Завершена» с
                                    verified; fail / need_criteria → «В работе» с комментарием
verify_queue(project=None)          карточки «Выполнена» с приёмкой reviewer, без поля proof
migrate_acceptance(project, dry_run)  owner → reviewer у непринятых карточек; одноразово на проект
                                    (отметка acceptance_migrated в файле), повтор → None
accepted_by(card)                   роль, принявшая карточку в «Завершена», или None
accept_how_parts(text) / accept_how_error(text)  критерий «что выполнить → что должно получиться»
Поле verified: {who, at, command, output, verdict: ok|fail, truncated}; перезаписывается при
каждой проверке. Роль reviewer в move_card не ходит — только verify_card.

Путь проекта
------------
set_start(project, role, date, event, said=None)      старт; агент — только если пусто
propose_finish(project, role, text)                    финиш одной фразой; пока не утверждён
approve_finish(project, role, said=None, text=None)    утвердить (owner_chat — с цитатой)
add_sprint(project, role, title, done_when)            новый спринт со статусом next
open_sprint(project, role, sprint_id, said=None)       next → current (owner_chat — с цитатой)
ready_sprint(project, role, sprint_id, proof=None)     current → closing; агенту proof обязателен
close_sprint(project, role, sprint_id, said=None)      closing или current → passed (с цитатой)
set_fog(project, role, lines)                          список «что видно» целиком, до FOG_MAX
set_progress_url(project, url)                         адрес опубликованной страницы
list_sprints(project) / get_sprint(project, sprint_id) / current_sprint(project)  чтение

Утро и обмен с владельцем
-------------------------
summary(boards_dir=None, now=None)  списки waiting, unfinished, done_yesterday (одна строка на
                                    карточку, reached — последний вчерашний статус),
                                    sessions_without_card (пустой, волна 2) и broken —
                                    [{project, error}] по файлам, которые не прочитались
read_inbox(project, ...)            что сделал владелец после курсора
ack_inbox(project, ...)             сдвинуть курсор; повторный вызов ничего не вернёт

ВЕРСИЯ КАРТОЧКИ: version=None ВЫКЛЮЧАЕТ проверку. Передавать version обязаны и board.py, и
server.py — иначе две одновременные правки затрут друг друга молча, без ConflictError.

Правила, которые модуль держит сам
----------------------------------
- Приёмка (acceptance) по умолчанию — «проверяющий» у задач и всех этапов. Агент при создании и
  правке может сузить её до «принимаю сам» (owner), но не может вернуть owner → reviewer: пометку
  «принимаю сам» снимает только владелец. Kind и stage карточки после создания не меняются
  ни одной ролью (в edit_card таких аргументов нет).
- Карточку в «Выполнена», «Завершена» и в архиве агент и проверяющий не правят (edit_card, reorder_card)
  — только комментарий и handoff.
- После возврата на доработку переход в «Выполнена» требует СВЕЖЕЕ доказательство: аргумент proof в
  этом же вызове, прежнее значение на карточке не засчитывается.
- Оговорку (caveat) при приёмке пишет только владелец.
- Имя проекта — по белому списку PROJECT_NAME, имена устройств Windows отказываются.
- Любое текстовое поле — строка или None; объект с __str__ отказывается («ожидалась строка»), иначе
  он прошёл бы мимо textcheck.

Проверка текста
---------------
Текст ролей «агент» и «проверяющий» проходит через модуль textcheck (check_fields); текст владельца
не проверяется — так записано в спеке. Ссылка на сессию в textcheck не идёт (там идентификатор) —
её вид проверяет SESSION_LINK.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:  # чтобы `import textcheck` работал у любого потребителя
    sys.path.insert(0, str(_HERE))

# Корень плагина (папка board лежит прямо в нём): нужен только для подсказки про установщик.
_PLUGIN_ROOT = _HERE.parent

# ---------------------------------------------------------------- модель

BACKLOG = "backlog"
NEW = "new"
IN_PROGRESS = "in_progress"
REVIEW = "review"
DONE = "done"
ACCEPTED = "accepted"
POSTPONED = "postponed"
CANCELLED = "cancelled"

STATUSES = {
    BACKLOG: "Бэклог",
    NEW: "Новая",
    IN_PROGRESS: "В работе",
    REVIEW: "На согласовании",
    DONE: "Выполнена",
    ACCEPTED: "Завершена",
    POSTPONED: "Отложена",
    CANCELLED: "Отменена",
}
COLUMNS = (BACKLOG, NEW, IN_PROGRESS, REVIEW, DONE, ACCEPTED)
ARCHIVE = (POSTPONED, CANCELLED)

OWNER = "owner"
AGENT = "agent"
REVIEWER = "reviewer"
OWNER_CHAT = "owner_chat"
ROLES = {OWNER: "владелец", AGENT: "агент", REVIEWER: "проверяющий", OWNER_CHAT: "владелец в чате"}
# Ключ reviewer в файлах не меняется (спека 2026-09-29, пункт 7); прежнее название «ревьюер»
# принимается как синоним при разборе.
ACCEPTANCE = {OWNER: "владелец", REVIEWER: "проверяющий"}
_ROLE_ALIASES = {REVIEWER: ("ревьюер",)}
_ACCEPTANCE_ALIASES = {REVIEWER: ("ревьюер", "агент"), OWNER: ("принимаю сам",)}

# Критерий проверки (accept_how): «<что выполнить> → <что должно получиться>», обе части непустые.
ACCEPT_HOW_SEP = "→"
ACCEPT_HOW_FORMAT = "«что выполнить → что должно получиться»"

# Вердикты проверяющего (verify_card).
VERIFY_OK = "ok"
VERIFY_FAIL = "fail"
VERIFY_NEED_CRITERIA = "need_criteria"
VERIFY_VERDICTS = (VERIFY_OK, VERIFY_FAIL, VERIFY_NEED_CRITERIA)
NEED_CRITERIA_COMMENT = "критерий проверки не задан: напиши, что выполнить и что должно получиться"
VERIFIED_FIELDS = ("who", "at", "command", "output", "verdict", "truncated")
MIGRATION_NOTE = "приёмка: владелец → проверяющий (миграция по спеке 2026-09-29)"

# Спринты пути проекта: следующий → текущий → ждёт закрытия → пройден.
SPRINT_NEXT = "next"
SPRINT_CURRENT = "current"
SPRINT_CLOSING = "closing"
SPRINT_PASSED = "passed"
SPRINT_STATUSES = {
    SPRINT_NEXT: "следующий",
    SPRINT_CURRENT: "текущий",
    SPRINT_CLOSING: "ждёт закрытия",
    SPRINT_PASSED: "пройден",
}
# Статусы, при которых спринт считается текущим: такой не больше одного.
SPRINT_ACTIVE = (SPRINT_CURRENT, SPRINT_CLOSING)
FOG_MAX = 5
SPRINT_ID = re.compile(r"s[1-9][0-9]*")
START_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
PROGRESS_URL = re.compile(r"https?://[^\s]+")

# Фраза отказа, когда решение владельца записывают без его цитаты; команда печатает её как есть.
NEED_OWNER_SAID = "решает владелец: нужна его цитата из чата"

PRIORITIES = {"A": "сейчас", "B": "следом", "C": "потом"}
TAGS = {"block": "блокирует остальное", "urgent": "срочно"}
KINDS = {"stage": "этап", "task": "задача"}

STAGES = (1, 3, 4, 5, 6, 7, 8)
# Приёмка по умолчанию — проверяющий у всех этапов и у задач (спека 2026-09-29, пункты 5 и 7);
# «принимаю сам» (owner) ставит владелец на странице, агент может лишь сузить до owner.
STAGE_ACCEPTANCE = {n: REVIEWER for n in STAGES}
DEFAULT_ACCEPTANCE = REVIEWER

LOCK_TIMEOUT = 10.0
STALE_DAYS = 2

# Имя проекта: латиница в нижнем регистре, цифры, дефис. Имена устройств Windows запрещены отдельно —
# файл con.json открыть нельзя.
PROJECT_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
RESERVED_NAMES = (
    {"con", "prn", "aux", "nul"}
    | {f"com{n}" for n in range(1, 10)}
    | {f"lpt{n}" for n in range(1, 10)}
)

# Ссылка на сессию, которую записывают команды board и хуки; модель её не придумывает.
SESSION_LINK = re.compile(r"claude://claude\.ai/epitaxy/[A-Za-z0-9_-]+")


class _Unset:
    """«Поле не передали» — отличается от None («снять значение»)."""

    def __repr__(self):  # pragma: no cover - для отладочного вывода
        return "UNSET"


UNSET = _Unset()

# Переходы, разрешённые агенту (и ревьюеру) — строго по диаграмме спеки.
AGENT_EDGES = frozenset({
    (BACKLOG, NEW),
    (NEW, BACKLOG),
    (NEW, IN_PROGRESS),
    (IN_PROGRESS, REVIEW),
    (REVIEW, IN_PROGRESS),
    (IN_PROGRESS, DONE),
})
# Статусы, которые агент может поставить сам (не выше «Выполнена»).
AGENT_MAX_STATUSES = frozenset({BACKLOG, NEW, IN_PROGRESS, REVIEW, DONE})

CARD_FIELDS = (
    "id", "kind", "stage", "title", "body", "status", "priority", "tag", "order", "sprint",
    "acceptance", "ask", "accept_how", "proof", "stopped_at", "resume", "session",
    "rework", "caveat", "verified", "comments", "history", "version", "created_at", "updated_at",
)
SPRINT_FIELDS = ("id", "title", "done_when", "status", "proof", "opened_at", "closed_at", "history")
PROJECT_FIELDS = (
    "project", "phrase", "path", "counter", "event_counter", "inbox_cursor",
    "start", "finish", "sprints", "fog", "progress_url", "acceptance_migrated", "cards",
)

# ---------------------------------------------------------------- исключения


class BoardError(Exception):
    """Любая отказанная операция доски. messages — готовые строки для печати."""

    def __init__(self, *messages):
        flat = []
        for m in messages:
            flat.extend(m if isinstance(m, (list, tuple)) else [m])
        self.messages = [str(m) for m in flat]
        super().__init__("\n".join(self.messages))


class ValidationError(BoardError):
    """Обязательное поле не заполнено, значение негодное или текст не прошёл textcheck."""


class ForbiddenError(BoardError):
    """Роли это делать нельзя. Сообщение — человеческая фраза по-русски."""


class ConflictError(BoardError):
    """Запись по устаревшей версии карточки. Файл не изменён; программа отвечает 409."""


class LockTimeoutError(BoardError):
    """Файл проекта занят другим процессом дольше LOCK_TIMEOUT."""


class NotFoundError(BoardError):
    """Нет такого проекта или карточки."""


def _err(field, message):
    """Строка ошибки в формате сборщика доски: два пробела после слова ОШИБКА."""
    return f"ОШИБКА  {field}: {message}"


def _text(value, field, required=False):
    """Текстовое поле: строка или None. Всё остальное — ошибка, а не «приведём к строке».

    Возвращает текст без пробелов по краям или None. Так объект с __str__ не проскочит мимо
    проверок textcheck и не ляжет в файл.
    """
    if value is None:
        if required:
            raise ValidationError(_err(field, "не заполнено"))
        return None
    if not isinstance(value, str):
        raise ValidationError(_err(field, "ожидалась строка"))
    text = value.strip()
    if required and not text:
        raise ValidationError(_err(field, "не заполнено"))
    return text


def _int(value, field):
    """Целое число из аргумента; мусор — ValidationError, а не ValueError из недр модуля."""
    if isinstance(value, bool) or isinstance(value, float) and value != int(value):
        raise ValidationError(_err(field, f"ожидалось целое число, а не «{value}»"))
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValidationError(_err(field, f"ожидалось целое число, а не «{value}»")) from None


# ---------------------------------------------------------------- разбор значений


def _norm(value):
    return str(value).strip().replace("_", " ").casefold()


def _parse(value, table, what, extra=None):
    if value is None:
        raise ValidationError(_err(what, "не указано"))
    key = _norm(value)
    for k, title in table.items():
        if key == _norm(k) or key == _norm(title):
            return k
    if extra:
        for k, aliases in extra.items():
            if any(key == _norm(a) for a in aliases):
                return k
    allowed = ", ".join(f"{k} ({v})" for k, v in table.items())
    raise ValidationError(_err(what, f"значение «{value}» не годится; допустимы {allowed}"))


def parse_status(value):
    """Ключ статуса по ключу или русскому названию; регистр и «_» не важны."""
    return _parse(value, STATUSES, "status")


def parse_role(value):
    """Ключ роли: owner, agent, reviewer (или владелец, агент, проверяющий; «ревьюер» — синоним)."""
    return _parse(value, ROLES, "role", extra=_ROLE_ALIASES)


def parse_acceptance(value):
    """Кто принимает карточку: owner или reviewer (владелец / принимаю сам, проверяющий / агент)."""
    return _parse(value, ACCEPTANCE, "acceptance", extra=_ACCEPTANCE_ALIASES)


def accept_how_parts(text):
    """Критерий проверки «что выполнить → что должно получиться» → (что, результат) или None.

    None — критерия нет, разделителя нет или одна из частей пуста. Разделитель — первая стрелка;
    вторая часть может содержать свои стрелки (редиректы и подобное).
    """
    if not isinstance(text, str):
        return None
    what, sep, expected = text.partition(ACCEPT_HOW_SEP)
    if not sep or not what.strip() or not expected.strip():
        return None
    return what.strip(), expected.strip()


def accept_how_error(text):
    """Строка ошибки для негодного критерия или None, если критерий годен."""
    if not (text or "").strip():
        return _err(
            "accept_how",
            f"критерий проверки обязателен в «Выполнена»: {ACCEPT_HOW_FORMAT}",
        )
    if accept_how_parts(text) is None:
        return _err(
            "accept_how",
            f"критерий проверки записывается в формате {ACCEPT_HOW_FORMAT}, разделитель « {ACCEPT_HOW_SEP} », "
            "обе части непустые",
        )
    return None


def parse_priority(value):
    """Приоритет A, B или C (сейчас, следом, потом)."""
    return _parse(value, PRIORITIES, "priority")


def parse_kind(value):
    """Вид карточки: stage (этап) или task (задача)."""
    return _parse(value, KINDS, "kind")


def parse_tag(value):
    """Метка block, urgent или пусто (None)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _parse(value, TAGS, "tag")


def status_title(key):
    """Русское название статуса."""
    return STATUSES.get(key, str(key))


def role_title(key):
    """Русское название роли."""
    return ROLES.get(key, str(key))


def acceptance_title(key):
    """Русское название того, кто принимает."""
    return ACCEPTANCE.get(key, str(key))


# ---------------------------------------------------------------- время


def _now():
    return datetime.now().astimezone()


def _iso(moment=None):
    return (moment or _now()).isoformat(timespec="seconds")


def _parse_dt(value):
    """ISO-строка → datetime с часовым поясом (наивная считается местной)."""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment


# ---------------------------------------------------------------- каталог данных и замок


def _claude_settings_path():
    """<CLAUDE_CONFIG_DIR или ~/.claude>/settings.json — туда установщик пишет переменные набора."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    root = Path(base).expanduser() if base else Path.home() / ".claude"
    return root / "settings.json"


def _data_dir_from_settings():
    """env.BOARD_DIR, иначе env.TATET_WIKI + /boards из settings.json Claude Code; нет — None.

    Переменные из settings.json Claude Code отдаёт только своим сессиям; в обычный терминал (команда
    `board` руками, `board serve` из автозапуска) они не попадают — поэтому файл читается напрямую.
    Битый или нечитаемый файл — молча None: дальше идёт поиск раскладки и понятная ошибка.
    """
    try:
        with open(_claude_settings_path(), encoding="utf-8-sig") as fh:
            settings = json.load(fh)
    except (OSError, ValueError):
        return None
    env = settings.get("env") if isinstance(settings, dict) else None
    if not isinstance(env, dict):
        return None
    board = env.get("BOARD_DIR")
    if isinstance(board, str) and board.strip():
        return Path(board.strip()).expanduser()
    wiki = env.get("TATET_WIKI")
    if isinstance(wiki, str) and wiki.strip():
        return Path(wiki.strip()).expanduser() / "boards"
    return None


def _layout_candidates():
    """Где лежит каталог данных по раскладке набора: <диск>:/project/llm-wiki/boards на Windows
    (диски C…Z по порядку), ~/project/llm-wiki/boards на остальных ОС."""
    if os.name == "nt":
        return [Path(f"{letter}:/") / "project" / "llm-wiki" / "boards" for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ"]
    return [Path.home() / "project" / "llm-wiki" / "boards"]


def _no_data_dir_message():
    setup = _PLUGIN_ROOT / "scripts" / "setup.py"
    return (
        "ПОМИЛКА  дані дошки: не знайдено папку з даними. Шукала: змінні BOARD_DIR і TATET_WIKI, "
        f"розділ env у {_claude_settings_path()}, папку project/llm-wiki/boards за розкладкою набору. "
        f'Запусти установник: python "{setup}" — і відкрий термінал заново.'
    )


def data_dir(boards_dir=None):
    """Каталог файлов проектов. Порядок: аргумент → BOARD_DIR → $TATET_WIKI/boards → env.BOARD_DIR
    или env.TATET_WIKI/boards из settings.json Claude Code → первая существующая папка раскладки
    (<диск>:/project/llm-wiki/boards, не на Windows — ~/project/llm-wiki/boards) → BoardError с
    подсказкой запустить установщик.

    Запасного каталога рядом с кодом нет намеренно: в плагине папка board лежит в кэше плагинов,
    данные там не видны владельцу и пропадают при обновлении плагина.
    """
    if boards_dir:
        return Path(boards_dir)
    env = os.environ.get("BOARD_DIR")
    if env:
        return Path(env)
    wiki = os.environ.get("TATET_WIKI")
    if wiki:
        return Path(wiki) / "boards"
    found = _data_dir_from_settings()
    if found is not None:
        return found
    for candidate in _layout_candidates():
        try:
            if candidate.is_dir():
                return candidate
        except OSError:
            continue
    raise BoardError(_no_data_dir_message())


def project_path(project, boards_dir=None):
    """Путь к файлу проекта <каталог данных>/<проект>.json.

    Имя проекта — по белому списку: латиница в нижнем регистре, цифры и дефис, до 64 знаков (так
    называются проекты в этом репозитории). Всё прочее отказ: «C:побег», «проект:поток», «..» и
    имена устройств Windows увели бы файл за каталог данных или в никуда.
    """
    if not isinstance(project, str):
        raise ValidationError(_err("project", "имя проекта — строка"))
    name = project.strip()
    if not PROJECT_NAME.fullmatch(name):
        raise ValidationError(_err(
            "project",
            f"имя «{project}» не годится: латиница в нижнем регистре, цифры и дефис, до 64 знаков, "
            "первый знак — буква или цифра",
        ))
    if name in RESERVED_NAMES:
        raise ValidationError(_err("project", f"имя «{name}» занято в Windows под устройство"))
    return data_dir(boards_dir) / f"{name}.json"


def _lock_path(project, boards_dir=None):
    return project_path(project, boards_dir).with_suffix(".lock")


if os.name == "nt":  # pragma: no cover - ветка выбирается операционной системой
    import msvcrt

    def _lock_fd(fd):
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock_fd(fd):
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:  # pragma: no cover - ветка выбирается операционной системой
    import fcntl

    def _lock_fd(fd):
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_fd(fd):
        fcntl.flock(fd, fcntl.LOCK_UN)


_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS = {}


@contextlib.contextmanager
def _project_lock(project, boards_dir=None, timeout=None):
    """Замок на файл проекта между процессами и потоками.

    Замок живёт в отдельном файле <проект>.lock: на Windows — msvcrt.locking, иначе fcntl.flock.
    Операционная система снимает такой замок вместе с закрытием дескриптора и вместе с процессом,
    поэтому зависшего замка после падения процесса не остаётся — файл .lock остаётся пустым и
    никому не мешает. Внутри одного процесса замок неповторный: потоки сначала выстраиваются на
    threading.Lock, иначе Windows не пустил бы второй дескриптор того же процесса.
    """
    timeout = LOCK_TIMEOUT if timeout is None else timeout
    lock_file = _lock_path(project, boards_dir)
    key = str(lock_file).casefold()
    with _LOCKS_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(key, threading.Lock())
    deadline = time.monotonic() + timeout
    if not thread_lock.acquire(timeout=timeout):
        raise LockTimeoutError(f"Файл проекта «{project}» занят другой задачей дольше {timeout:g} с. Повтори позже.")
    fd = None
    try:
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT, 0o644)
        while True:
            try:
                _lock_fd(fd)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockTimeoutError(
                        f"Файл проекта «{project}» занят другим процессом дольше {timeout:g} с "
                        "(доска и команда пишут одновременно). Повтори команду."
                    )
                time.sleep(0.03)
        yield
    finally:
        if fd is not None:
            try:
                _unlock_fd(fd)
            except OSError:
                pass
            os.close(fd)
        thread_lock.release()


# ---------------------------------------------------------------- чтение и запись файла проекта


def _blank_card():
    return {
        "id": "", "kind": "task", "stage": None, "title": "", "body": "", "status": BACKLOG,
        "priority": "B", "tag": None, "order": 0, "sprint": None, "acceptance": DEFAULT_ACCEPTANCE,
        "ask": "", "accept_how": "", "proof": "", "stopped_at": "", "resume": "", "session": None,
        "rework": "", "caveat": "", "verified": None,
        "comments": [], "history": [], "version": 1, "created_at": "", "updated_at": "",
    }


def _normalize_card(raw):
    card = _blank_card()
    for key in CARD_FIELDS:
        if key in raw:
            card[key] = raw[key]
    return {key: card[key] for key in CARD_FIELDS}


def _blank_sprint():
    return {
        "id": "", "title": "", "done_when": "", "status": SPRINT_NEXT, "proof": "",
        "opened_at": None, "closed_at": None, "history": [],
    }


def _normalize_sprint(raw):
    sprint = _blank_sprint()
    for key in SPRINT_FIELDS:
        if key in raw:
            sprint[key] = raw[key]
    return {key: sprint[key] for key in SPRINT_FIELDS}


def _normalize_start(raw):
    if not isinstance(raw, dict):
        return None
    return {"date": raw.get("date") or "", "event": raw.get("event") or ""}


def _normalize_finish(raw):
    if not isinstance(raw, dict):
        return None
    return {
        "text": raw.get("text") or "",
        "approved": bool(raw.get("approved", False)),
        "approved_at": raw.get("approved_at"),
        "history": list(raw.get("history") or []),
    }


def _normalize_project(raw, name):
    """Старые файлы без полей пути читаются как есть: умолчания None, None, [], [], ""."""
    data = {
        "project": raw.get("project") or name,
        "phrase": raw.get("phrase", ""),
        "path": raw.get("path", ""),
        "counter": int(raw.get("counter", 0)),
        "event_counter": int(raw.get("event_counter", 0)),
        "inbox_cursor": int(raw.get("inbox_cursor", 0)),
        "start": _normalize_start(raw.get("start")),
        "finish": _normalize_finish(raw.get("finish")),
        "sprints": [_normalize_sprint(s) for s in raw.get("sprints") or []],
        "fog": [str(line) for line in raw.get("fog") or []],
        "progress_url": raw.get("progress_url") or "",
        # Дата одноразовой миграции приёмки (спека 2026-09-29); пусто — файл старого кода, миграция
        # ещё не шла. Новый проект получает дату при создании: мигрировать в нём нечего.
        "acceptance_migrated": str(raw.get("acceptance_migrated") or ""),
        "cards": [_normalize_card(c) for c in raw.get("cards", [])],
    }
    return data


def _load(project, boards_dir=None):
    file = project_path(project, boards_dir)
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise NotFoundError(f"Проекта «{project}» на доске нет: файл {file.name} не найден.") from None
    except ValueError as exc:
        raise BoardError(_err("файл проекта", f"{file.name} не читается как JSON: {exc}")) from None
    if not isinstance(raw, dict):
        raise BoardError(_err("файл проекта", f"{file.name}: ожидался объект JSON"))
    return _normalize_project(raw, project)


def _dump(data):
    ordered = {key: data[key] for key in PROJECT_FIELDS}
    ordered["sprints"] = [{k: s[k] for k in SPRINT_FIELDS} for s in data["sprints"]]
    ordered["cards"] = [{k: c[k] for k in CARD_FIELDS} for c in data["cards"]]
    return json.dumps(ordered, ensure_ascii=False, indent=2) + "\n"


def _save(project, data, boards_dir=None):
    """Запись: сериализация → временный файл рядом → os.replace. Ошибка — прежний файл цел."""
    file = project_path(project, boards_dir)
    file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(file.parent), prefix=f"{file.stem}.", suffix=".tmp")
    done = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(_dump(data))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, str(file))
        done = True
    except OSError as exc:
        raise BoardError(_err("файл проекта", f"запись не удалась ({exc}); прежний файл не тронут")) from exc
    finally:
        if not done:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    return file


# ---------------------------------------------------------------- события


def _event(data):
    data["event_counter"] = int(data["event_counter"]) + 1
    return data["event_counter"]


def _history(data, card, frm, to, role, text=""):
    card["history"].append({
        "seq": _event(data),
        "at": _iso(),
        "who": role,
        "from": frm,
        "to": to,
        "text": text or "",
    })


def _comment(data, card, role, text, with_move=False):
    """Комментарий в ленту карточки. with_move=True — комментарий написан вместе с переносом
    (возврат на доработку): в ленте он виден, а в inbox его заменяет само событие переноса."""
    card["comments"].append({
        "seq": _event(data), "at": _iso(), "who": role, "text": text, "move": bool(with_move),
    })


def _touch(card):
    card["version"] = int(card["version"]) + 1
    card["updated_at"] = _iso()


# ---------------------------------------------------------------- проверки


def _check_text(role, fields):
    """Текст ролей агент и ревьюер — через textcheck; текст владельца не проверяется."""
    if role == OWNER:
        return
    payload = {k: v for k, v in fields.items() if isinstance(v, str) and v.strip()}
    if not payload:
        return
    import textcheck  # ленивый импорт: модуль лежит рядом, у тестов есть возможность подменить

    errors = textcheck.check_fields(payload)
    if errors:
        raise ValidationError(errors)


def _find(data, card_id):
    for card in data["cards"]:
        if card["id"] == card_id:
            return card
    raise NotFoundError(f"Карточки «{card_id}» на доске нет.")


def _owner_decision(role, said, action):
    """Действие, которое решает только владелец: утвердить финиш, открыть и закрыть спринт.

    Владелец (страница) — без цитаты. Владелец в чате — только с непустой цитатой said: агент
    записывает его слово дословно. Агент и ревьюер — ForbiddenError. Возвращает цитату (или None).
    Цитата проверяется как текст, который набрал агент (ключ owner_said), — это владелец сказал,
    но записал агент.
    """
    role = parse_role(role)
    if role in (AGENT, REVIEWER):
        raise ForbiddenError(
            f"{action} может только владелец: спроси его в чате и запиши ответ командой с его цитатой "
            "(роль «владелец в чате», --owner-said)."
        )
    said = _text(said, "said")
    if role == OWNER_CHAT:
        if not said:
            raise ValidationError(_err("said", NEED_OWNER_SAID))
        _check_text(role, {"owner_said": said})
    return said or None


def _find_sprint(data, sprint_id):
    if not isinstance(sprint_id, str) or not SPRINT_ID.fullmatch(sprint_id.strip()):
        raise ValidationError(_err("sprint", f"номер спринта вида s1, s2…, а не «{sprint_id}»"))
    for sprint in data["sprints"]:
        if sprint["id"] == sprint_id.strip():
            return sprint
    raise NotFoundError(f"Спринта «{sprint_id}» в проекте «{data['project']}» нет.")


def _active_sprint(data):
    """Текущий спринт (current или closing) или None."""
    for sprint in data["sprints"]:
        if sprint["status"] in SPRINT_ACTIVE:
            return sprint
    return None


def _sprint_history(data, sprint, frm, to, role, said=None, text=""):
    sprint["history"].append({
        "seq": _event(data),
        "at": _iso(),
        "who": role,
        "from": frm,
        "to": to,
        "said": said or "",
        "text": text or "",
    })


def _check_version(card, version):
    """version=None ВЫКЛЮЧАЕТ проверку версии. Передавать её обязаны board.py и server.py."""
    if version is None:
        return
    if _int(version, "version") != int(card["version"]):
        raise ConflictError(
            f"Карточку «{card['id']}» уже изменили: у тебя версия {version}, на доске {card['version']}. "
            "Перечитай карточку и повтори."
        )


def can_move(card, to, role):
    """Можно ли роли перевести карточку в статус `to`. Возвращает (можно, причина отказа).

    Владелец двигает карточку по доске куда угодно (перетаскивание не должно мешать без причины),
    обязательные поля при этом всё равно проверяются. Агент и ревьюер ходят строго по диаграмме.
    """
    frm = card["status"]
    to = parse_status(to)
    role = parse_role(role)
    if frm == to:
        return False, f"Карточка уже в колонке «{status_title(to)}»: переносить некуда."
    if role == OWNER:
        return True, ""
    if frm in ARCHIVE:
        return False, (
            f"Карточка лежит в архиве («{status_title(frm)}»). Вернуть её оттуда может только владелец — "
            "предложи это комментарием."
        )
    if to in ARCHIVE:
        return False, (
            f"Отложить или отменить карточку может только владелец. Напиши комментарием, почему её стоит "
            f"перевести в «{status_title(to)}»."
        )
    if frm == ACCEPTED:
        return False, "Карточка уже завершена: трогать её может только владелец."
    if role == REVIEWER:
        # Проверяющий ходит только из «Выполнена»: принять (→ Завершена) или вернуть (→ В работе),
        # и только у карточек с приёмкой «проверяющий». Запись — verify_card, не move_card.
        if frm != DONE or to not in (ACCEPTED, IN_PROGRESS):
            return False, (
                f"Проверяющий принимает или возвращает только карточку в «Выполнена»; "
                f"переход «{status_title(frm)}» → «{status_title(to)}» ему не положен."
            )
        if card["acceptance"] != REVIEWER:
            return False, "Эту карточку принимает владелец: проверяющий её не трогает."
        return True, ""
    if to == ACCEPTED:
        if frm != DONE:
            return False, (
                f"В «Завершена» карточка попадает только из «Выполнена». Сейчас карточка в «{status_title(frm)}»."
            )
        return False, (
            "Ставить «Завершена» агент не может: карточку принимает проверяющий (board verify) или владелец. "
            "Оставь её в «Выполнена»."
        )
    if frm == DONE and to == IN_PROGRESS:
        return False, (
            "Вернуть карточку на доработку может владелец на странице или проверяющий командой "
            "board verify --fail — и обязательно с комментарием, что не так."
        )
    if to not in AGENT_MAX_STATUSES:
        return False, f"Ставить «{status_title(to)}» агент не может."
    if (frm, to) not in AGENT_EDGES:
        return False, (
            f"Перехода «{status_title(frm)}» → «{status_title(to)}» на доске нет. "
            "Агент ходит по порядку: Бэклог → Новая → В работе → На согласовании → Выполнена."
        )
    return True, ""


def _refuse_owner_only_card(card):
    """Карточки, которые агент и ревьюер не правят: их уже смотрит владелец или они в архиве.

    Комментарий и handoff при этом разрешены — они ничего не переписывают.
    """
    status = card["status"]
    if status == DONE:
        raise ForbiddenError(
            "Карточка ждёт приёмки: правки — комментарием, либо владелец вернёт её на доработку."
        )
    if status == ACCEPTED or status in ARCHIVE:
        raise ForbiddenError(
            f"Карточка в «{status_title(status)}»: менять её может только владелец. "
            "Предложи правку комментарием."
        )


def _required_for_move(card, frm, to, role, ask, proof, accept_how, comment):
    """Обязательные поля целевой колонки. Возвращает список строк ошибок."""
    errors = []
    if to == REVIEW and not (ask or card["ask"]).strip():
        errors.append(_err("ask", "в «На согласовании» нужно написать, что решить владельцу"))
    if to == DONE:
        was_rework = bool(str(card["rework"] or "").strip())
        if was_rework and not (proof or "").strip():
            errors.append(_err(
                "proof",
                "карточка возвращалась на доработку: нужно свежее доказательство, прежнее не засчитывается",
            ))
        elif not was_rework and not (proof or card["proof"]).strip():
            errors.append(_err("proof", "в «Выполнена» нужно доказательство: чем доказано и кем проверено"))
        # Критерий проверки обязателен у карточки с любой приёмкой (спека 2026-09-29, пункт 1):
        # новый в этом же вызове или уже записанный, но обязательно в формате «а → б».
        problem = accept_how_error(accept_how or card["accept_how"])
        if problem:
            errors.append(problem)
    if frm == DONE and to == IN_PROGRESS and not (comment or "").strip():
        errors.append(_err("comment", "возврат на доработку без комментария невозможен: напиши, что не так"))
    if frm == ACCEPTED and to == IN_PROGRESS and not (comment or "").strip():
        errors.append(_err("comment", "вернуть принятую карточку без комментария нельзя: напиши, почему возвращаешь"))
    return errors


# ---------------------------------------------------------------- проекты


def list_projects(boards_dir=None):
    """Имена проектов на доске (по файлам каталога данных), по алфавиту."""
    folder = data_dir(boards_dir)
    if not folder.is_dir():
        return []
    return sorted(p.stem for p in folder.glob("*.json") if p.is_file())


def read_project(project, boards_dir=None):
    """Файл проекта целиком — копией; менять возвращённое бесполезно, запись только функциями модуля."""
    return _load(project, boards_dir)


def create_project(project, phrase="", project_dir="", boards_dir=None):
    """Создать пустой файл проекта: имя, фраза (одна строка, что это), путь к проекту."""
    file = project_path(project, boards_dir)
    phrase = _text(phrase, "phrase") or ""
    project_dir = _text(project_dir, "path") or ""
    with _project_lock(project, boards_dir):
        if file.exists():
            raise BoardError(_err("project", f"проект «{project}» на доске уже есть"))
        data = _normalize_project(
            {"project": project.strip(), "phrase": phrase, "path": project_dir, "acceptance_migrated": _iso()},
            project.strip(),
        )
        _save(project, data, boards_dir)
    return read_project(project, boards_dir)


def state_token(boards_dir=None):
    """Дешёвая отметка состояния всех файлов проектов; меняется при любой записи.

    Страница спрашивает её раз в пять секунд: изменилась — перечитывает данные.
    """
    folder = data_dir(boards_dir)
    if not folder.is_dir():
        return "0"
    marks = []
    for file in sorted(folder.glob("*.json")):
        try:
            st = file.stat()
        except OSError:
            continue
        marks.append(f"{file.name}:{st.st_mtime_ns}:{st.st_size}")
    return hashlib.sha1("|".join(marks).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- карточки


def list_cards(project, status=None, boards_dir=None):
    """Карточки проекта в порядке доски: колонка → приоритет → порядок внутри колонки."""
    data = _load(project, boards_dir)
    cards = data["cards"]
    if status is not None:
        want = parse_status(status)
        cards = [c for c in cards if c["status"] == want]
    order = {s: i for i, s in enumerate(COLUMNS + ARCHIVE)}
    return sorted(cards, key=lambda c: (order.get(c["status"], 99), c["priority"], c["order"], c["id"]))


def get_card(project, card_id, boards_dir=None):
    """Одна карточка копией."""
    return copy.deepcopy(_find(_load(project, boards_dir), card_id))


def find_stage_card(project, stage, boards_dir=None):
    """Карточка-этап по номеру этапа или None. Нужна импорту блока «Пайплайн»: без дублей."""
    number = _int(stage, "stage")
    for card in _load(project, boards_dir)["cards"]:
        if card["kind"] == "stage" and card["stage"] == number:
            return copy.deepcopy(card)
    return None


def add_card(project, role, title, body="", kind="task", stage=None, status=BACKLOG, priority="B",
             tag=None, acceptance=None, ask="", proof="", accept_how="", sprint=UNSET, boards_dir=None):
    """Создать карточку. Владелец и агент могут оба; агент — не выше «Выполнена».

    Кто принимает карточку, задаёт только владелец: у агента и ревьюера acceptance либо не указан,
    либо равен умолчанию по виду карточки (задача — владелец, этап — по STAGE_ACCEPTANCE). Иначе
    агент завёл бы карточку с приёмкой «ревьюер» и принял её сам, минуя владельца.

    sprint — id спринта или None. Не передан: у агента карточка идёт в текущий спринт (если он
    есть), у владельца остаётся без спринта — открытая карточка без спринта и так считается
    задачей текущего.
    """
    role = parse_role(role)
    kind = parse_kind(kind)
    status = parse_status(status)
    priority = parse_priority(priority)
    tag = parse_tag(tag)
    title = _text(title, "title")
    body = _text(body, "body")
    ask = _text(ask, "ask")
    proof = _text(proof, "proof")
    accept_how = _text(accept_how, "accept_how")

    errors = []
    if not title:
        errors.append(_err("title", "заголовок карточки в одну строку обязателен"))
    if kind == "stage":
        if stage is None:
            errors.append(_err("stage", "у карточки-этапа нужен номер этапа"))
        elif _int(stage, "stage") not in STAGES:
            errors.append(_err("stage", f"этап «{stage}»; допустимы {', '.join(str(s) for s in STAGES)}"))
    elif stage is not None:
        errors.append(_err("stage", "номер этапа бывает только у карточки-этапа"))
    if errors:
        raise ValidationError(errors)

    stage = _int(stage, "stage") if kind == "stage" else None
    # Умолчание — проверяющий у всех (спека 2026-09-29, пункт 7). Агент при создании вправе сузить
    # свои права до «принимаю сам» (owner); расширить нечего — шире умолчания приёмки нет.
    default_acceptance = STAGE_ACCEPTANCE[stage] if kind == "stage" else DEFAULT_ACCEPTANCE
    acceptance = default_acceptance if acceptance is None else parse_acceptance(acceptance)

    if role != OWNER and status not in AGENT_MAX_STATUSES:
        raise ForbiddenError(
            f"Агент не заводит карточку сразу в «{status_title(status)}»: выше «Выполнена» карточку "
            "переводит владелец."
        )
    _check_text(role, {"title": title, "body": body, "ask": ask, "proof": proof, "accept_how": accept_how})

    with _project_lock(project, boards_dir):
        data = _load(project, boards_dir)
        if kind == "stage" and any(c["kind"] == "stage" and c["stage"] == stage for c in data["cards"]):
            raise BoardError(_err("stage", f"карточка этапа {stage} в проекте «{project}» уже есть"))
        if sprint is UNSET:
            active = _active_sprint(data) if role != OWNER else None
            sprint_id = active["id"] if active else None
        elif sprint is None:
            sprint_id = None
        else:
            sprint_id = _find_sprint(data, sprint)["id"]
        data["counter"] = int(data["counter"]) + 1
        card = _blank_card()
        card.update({
            "id": f"{data['project']}-{data['counter']}",
            "kind": kind,
            "stage": stage,
            "title": title,
            "body": body or "",
            "status": status,
            "priority": priority,
            "tag": tag,
            "order": data["counter"],
            "sprint": sprint_id,
            "acceptance": acceptance,
            "ask": ask or "",
            "proof": proof or "",
            "accept_how": accept_how or "",
            "created_at": _iso(),
            "updated_at": _iso(),
        })
        if status in (REVIEW, DONE):
            problems = _required_for_move(card, None, status, role, ask, proof, accept_how, None)
            if problems:
                raise ValidationError(problems)
        _history(data, card, None, status, role)
        data["cards"].append(card)
        _save(project, data, boards_dir)
        return copy.deepcopy(card)


def move_card(project, card_id, to, role, version=None, ask=None, proof=None, accept_how=None,
              comment=None, caveat=None, boards_dir=None):
    """Сменить статус карточки. Проверяет права роли, обязательные поля и версию.

    Роль «проверяющий» сюда не ходит: её переходы («Выполнена» → «Завершена» / «В работе») делает
    только verify_card — с записью verified и дословным выводом.
    """
    role = parse_role(role)
    if role == REVIEWER:
        raise ForbiddenError(
            "Проверяющий принимает и возвращает карточки только командой board verify "
            "(verify_card): с командой проверки и дословным выводом."
        )
    to = parse_status(to)
    ask = _text(ask, "ask")
    proof = _text(proof, "proof")
    accept_how = _text(accept_how, "accept_how")
    comment = _text(comment, "comment")
    caveat = _text(caveat, "caveat")
    _check_text(role, {
        "ask": ask, "proof": proof, "accept_how": accept_how, "comment": comment, "caveat": caveat,
    })
    if caveat:
        if role != OWNER:
            raise ForbiddenError(
                "Принять карточку с оговоркой может только владелец: оговорку пишет он, а не агент."
            )
        if to != ACCEPTED:
            raise ValidationError(_err("caveat", "оговорка записывается только при приёмке в «Завершена»"))
    with _project_lock(project, boards_dir):
        data = _load(project, boards_dir)
        card = _find(data, card_id)
        _check_version(card, version)
        frm = card["status"]

        allowed, why = can_move(card, to, role)
        if not allowed:
            raise ForbiddenError(why)

        problems = _required_for_move(card, frm, to, role, ask, proof, accept_how, comment)
        if problems:
            raise ValidationError(problems)

        if ask:
            card["ask"] = ask
        if proof:
            card["proof"] = proof
        if accept_how:
            card["accept_how"] = accept_how

        note = ""
        if frm in (DONE, ACCEPTED) and to == IN_PROGRESS:
            # Возврат на доработку — из «Выполнена» (не принято) или из «Завершена» (владелец
            # вернул принятое, спека 2026-09-29, пункт 4): метка «на доработку», комментарий в ленту.
            note = comment
            card["rework"] = note
            _comment(data, card, role, note, with_move=True)
        elif comment:
            note = comment
            _comment(data, card, role, note, with_move=True)

        if to == DONE:
            card["rework"] = ""
        if to == ACCEPTED:
            card["caveat"] = caveat or ""
            if card["caveat"]:
                note = note or card["caveat"]

        card["status"] = to
        _history(data, card, frm, to, role, note)
        _touch(card)
        _save(project, data, boards_dir)
        return copy.deepcopy(card)


def edit_card(project, card_id, role, title=None, body=None, priority=None, tag=UNSET,
              acceptance=None, version=None, boards_dir=None):
    """Правка полей карточки: заголовок, текст, приоритет, метка, кто принимает.

    Поле, которому ничего не передали, остаётся прежним. Метку снимает tag=None или пустая строка.
    Агент и ревьюер не правят карточку, которую уже смотрит владелец («Выполнена», «Завершена») и
    карточку из архива, а поле acceptance не меняют никогда.
    """
    role = parse_role(role)
    title = _text(title, "title")
    body = _text(body, "body")
    _check_text(role, {"title": title, "body": body})
    if acceptance is not None:
        acceptance = parse_acceptance(acceptance)
    if priority is not None:
        priority = parse_priority(priority)
    if tag is not UNSET:
        tag = parse_tag(tag)

    with _project_lock(project, boards_dir):
        data = _load(project, boards_dir)
        card = _find(data, card_id)
        _check_version(card, version)
        if role != OWNER:
            _refuse_owner_only_card(card)
            # Агент может сузить приёмку до «принимаю сам», но не снять пометку владельца
            # (owner → reviewer) — спека 2026-09-29, пункт 7 и раздел «Защита».
            if acceptance == REVIEWER and card["acceptance"] == OWNER:
                raise ForbiddenError(
                    "Приёмку «принимаю сам» поставил владелец: вернуть её на проверяющего агент не может."
                )
        if title is not None:
            if not title:
                raise ValidationError(_err("title", "заголовок карточки в одну строку обязателен"))
            card["title"] = title
        if body is not None:
            card["body"] = body
        if priority is not None:
            card["priority"] = priority
        if tag is not UNSET:
            card["tag"] = tag
        if acceptance is not None:
            card["acceptance"] = acceptance
        _touch(card)
        _save(project, data, boards_dir)
        return copy.deepcopy(card)


def reorder_card(project, card_id, role, order=None, priority=None, version=None, boards_dir=None):
    """Порядок карточки внутри колонки и приоритет. Владелец и агент могут оба."""
    role = parse_role(role)
    if priority is not None:
        priority = parse_priority(priority)
    if order is not None:
        order = _int(order, "order")
    with _project_lock(project, boards_dir):
        data = _load(project, boards_dir)
        card = _find(data, card_id)
        _check_version(card, version)
        if role != OWNER:
            _refuse_owner_only_card(card)
        if order is not None:
            card["order"] = order
        if priority is not None:
            card["priority"] = priority
        _touch(card)
        _save(project, data, boards_dir)
        return copy.deepcopy(card)


def comment_card(project, card_id, role, text, version=None, boards_dir=None):
    """Комментарий в ленту карточки. Владелец и агент могут оба, в любой колонке."""
    role = parse_role(role)
    text = _text(text, "comment")
    if not text:
        raise ValidationError(_err("comment", "пустой комментарий не записывается"))
    _check_text(role, {"comment": text})
    with _project_lock(project, boards_dir):
        data = _load(project, boards_dir)
        card = _find(data, card_id)
        _check_version(card, version)
        _comment(data, card, role, text)
        _touch(card)
        _save(project, data, boards_dir)
        return copy.deepcopy(card)


def handoff(project, card_id, role, stopped_at=None, resume=None, session=None, version=None, boards_dir=None):
    """На чём остановились, готовый запрос и ссылка на сессию — без смены статуса.

    session — {link, title}: ссылку вида claude://claude.ai/epitaxy/<идентификатор> записывают
    команды board и хуки, модель её не придумывает. В textcheck ссылка не идёт: там идентификатор.
    """
    role = parse_role(role)
    stopped_at = _text(stopped_at, "stopped_at")
    resume = _text(resume, "resume")
    _check_text(role, {"stopped_at": stopped_at, "resume": resume})
    if session is not None:
        if not isinstance(session, dict):
            raise ValidationError(_err("session", "нужен объект с полями link и title"))
        link = _text(session.get("link"), "session.link") or ""
        session_title = _text(session.get("title"), "session.title") or ""
        if not link and not session_title:
            raise ValidationError(_err("session", "нужен объект с полями link и title"))
        if link and not SESSION_LINK.fullmatch(link):
            raise ValidationError(_err(
                "session.link", "ссылка на сессию — claude://claude.ai/epitaxy/<идентификатор>"
            ))
        _check_text(role, {"title": session_title})
    with _project_lock(project, boards_dir):
        data = _load(project, boards_dir)
        card = _find(data, card_id)
        _check_version(card, version)
        if stopped_at is not None:
            card["stopped_at"] = stopped_at
        if resume is not None:
            card["resume"] = resume
        if session is not None:
            card["session"] = {"link": link, "title": session_title}
        _touch(card)
        _save(project, data, boards_dir)
        return copy.deepcopy(card)


def set_card_sprint(project, card_id, role, sprint_id, version=None, said=None, boards_dir=None):
    """Привязать карточку к спринту (id) или отвязать (None). Версия карточки проверяется.

    Агент и ревьюер — только у карточек, которые ещё не смотрит владелец («Выполнена», «Завершена»
    и архив — отказ). Владелец — у любой; владелец в чате — у любой, но с цитатой said.
    """
    role = parse_role(role)
    with _project_lock(project, boards_dir):
        data = _load(project, boards_dir)
        card = _find(data, card_id)
        _check_version(card, version)
        if role != OWNER:
            try:
                _refuse_owner_only_card(card)
            except ForbiddenError:
                if role != OWNER_CHAT:
                    raise
                _owner_decision(role, said, "Менять спринт у принятой карточки")
        target = None if sprint_id is None else _find_sprint(data, sprint_id)["id"]
        card["sprint"] = target
        _touch(card)
        _save(project, data, boards_dir)
        return copy.deepcopy(card)


def delete_card(project, card_id, role, version=None, boards_dir=None):
    """Убрать карточку с доски. Только владелец; номер карточки не переиспользуется."""
    role = parse_role(role)
    if role != OWNER:
        raise ForbiddenError("Удалять карточки может только владелец. Предложи это комментарием.")
    with _project_lock(project, boards_dir):
        data = _load(project, boards_dir)
        card = _find(data, card_id)
        _check_version(card, version)
        data["cards"] = [c for c in data["cards"] if c["id"] != card_id]
        _save(project, data, boards_dir)
        return copy.deepcopy(card)


# ---------------------------------------------------------------- приёмка проверяющим (спека 2026-09-29)


def accepted_by(card):
    """Кто принял карточку в «Завершена» по последней записи истории: роль или None (не принята)."""
    if card.get("status") != ACCEPTED:
        return None
    for entry in reversed(card.get("history") or []):
        if entry.get("to") == ACCEPTED:
            return entry.get("who") or None
    return None


def verify_card(project, card_id, verdict, command=None, output=None, comment=None, version=None,
                boards_dir=None):
    """Запись проверяющего (роль reviewer) по карточке в «Выполнена» с приёмкой «проверяющий».

    verdict:
      ok             критерий совпал → «Завершена»; command и output обязательны, в историю
                     «принято проверяющим», verified = {who, at, command, output, verdict, truncated}
      fail           не совпало → «В работе» с меткой «на доработку»; comment, command, output обязательны
      need_criteria  критерия нет или без разделителя → «В работе» с комментарием NEED_CRITERIA_COMMENT
                     (comment, если передан, дописывается после); command и output не нужны,
                     verified не пишется
    Отказы: карточка не в «Выполнена» (ValidationError), приёмка owner (ForbiddenError), у ok/fail
    критерий негоден (ValidationError — верни --need-criteria), вывод пуст или содержит секрет
    (ValidationError, ничего не записано). Вывод хранится дословно, обрезка по textcheck.OUTPUT_LIMIT
    с пометкой «(обрезано)»; пути и хеши в выводе разрешены.
    """
    if verdict not in VERIFY_VERDICTS:
        raise ValidationError(_err("verdict", f"вердикт «{verdict}»; допустимы {', '.join(VERIFY_VERDICTS)}"))
    command = _text(command, "verify_command")
    comment = _text(comment, "comment")
    output_text = _text(output, "output")
    import textcheck  # ленивый импорт, как в _check_text

    truncated = False
    if verdict in (VERIFY_OK, VERIFY_FAIL):
        if not command:
            raise ValidationError(_err("verify_command", "укажи команду или действие, как выполнялась проверка"))
        if not output_text:
            raise ValidationError(_err("output", "нужен дословный вывод команды: файл вывода пуст или не передан"))
        problems = textcheck.check_output(command, "verify_command") + textcheck.check_output(output_text, "output")
        problems += textcheck.check_text(command, "verify_command", limit=textcheck.LIMITS["verify_command"],
                                         allow_paths=True)
        if problems:
            raise ValidationError(problems)
        output_text, truncated = textcheck.trim_output(output_text)
    if verdict == VERIFY_FAIL and not comment:
        raise ValidationError(_err("comment", "возврат на доработку без комментария невозможен: напиши, что не совпало"))
    if comment:
        _check_text(REVIEWER, {"comment": comment})

    with _project_lock(project, boards_dir):
        data = _load(project, boards_dir)
        card = _find(data, card_id)
        _check_version(card, version)
        frm = card["status"]
        if frm != DONE:
            raise ValidationError(_err(
                "status", f"проверяющий принимает только карточку в «Выполнена»; сейчас «{status_title(frm)}»",
            ))
        if card["acceptance"] != REVIEWER:
            raise ForbiddenError("Эту карточку принимает владелец: проверяющий её не трогает.")
        criteria_ok = accept_how_parts(card["accept_how"]) is not None
        if verdict in (VERIFY_OK, VERIFY_FAIL) and not criteria_ok:
            raise ValidationError(_err(
                "accept_how", "критерий проверки не задан — верни карточку запросом критерия (--need-criteria)",
            ))

        if verdict == VERIFY_OK:
            card["verified"] = {
                "who": REVIEWER, "at": _iso(), "command": command, "output": output_text,
                "verdict": VERIFY_OK, "truncated": truncated,
            }
            if comment:
                _comment(data, card, REVIEWER, comment, with_move=True)
            card["status"] = ACCEPTED
            card["caveat"] = ""
            _history(data, card, frm, ACCEPTED, REVIEWER, "принято проверяющим")
        else:
            if verdict == VERIFY_FAIL:
                card["verified"] = {
                    "who": REVIEWER, "at": _iso(), "command": command, "output": output_text,
                    "verdict": VERIFY_FAIL, "truncated": truncated,
                }
                note = comment
            else:
                note = NEED_CRITERIA_COMMENT + (f" — {comment}" if comment else "")
            card["rework"] = note
            _comment(data, card, REVIEWER, note, with_move=True)
            card["status"] = IN_PROGRESS
            _history(data, card, frm, IN_PROGRESS, REVIEWER, note)
        _touch(card)
        _save(project, data, boards_dir)
        return copy.deepcopy(card)


def verify_queue(project=None, boards_dir=None):
    """Очередь проверяющего: карточки в «Выполнена» с приёмкой «проверяющий» — без поля proof.

    project=None — все проекты доски. Каждая запись: {project, id, title, body, accept_how,
    criteria_ok, rework}. Файлы, которые не читаются, пропускаются (их покажет summary в broken).
    """
    names = [project] if project else list_projects(boards_dir)
    queue = []
    for name in names:
        try:
            data = _load(name, boards_dir)
        except BoardError:
            if project:
                raise
            continue
        for card in data["cards"]:
            if card["status"] != DONE or card["acceptance"] != REVIEWER:
                continue
            queue.append({
                "project": data["project"], "id": card["id"], "title": card["title"], "body": card["body"],
                "accept_how": card["accept_how"], "criteria_ok": accept_how_parts(card["accept_how"]) is not None,
                "kind": card["kind"], "stage": card["stage"], "priority": card["priority"],
            })
    queue.sort(key=lambda c: (c["project"], c["priority"], c["id"]))
    return queue


def migrate_acceptance(project, dry_run=False, boards_dir=None):
    """Одноразовый перенос приёмки на новое умолчание (спека 2026-09-29, раздел «Миграция»).

    У карточек не в «Завершена» и не в архиве с приёмкой owner приёмка становится reviewer; в историю
    пишется MIGRATION_NOTE ролью agent. Принятые и архивные не трогаются. Возвращает список
    изменённых карточек [{id, title, status}]; dry_run=True — только список, файл не пишется.

    Одноразовость держит отметка acceptance_migrated в файле проекта: после первого запуска без
    dry_run (даже если менять было нечего) в неё пишется дата, и любой следующий запуск, включая
    dry_run, возвращает None — «уже выполнена», ничего не трогая. Иначе повтор снимал бы
    «принимаю сам», поставленное владельцем или агентом после миграции (спека, раздел «Защита»).
    Новый проект получает отметку при создании файла.
    """
    with _project_lock(project, boards_dir):
        data = _load(project, boards_dir)
        if data["acceptance_migrated"]:
            return None
        changed = []
        for card in data["cards"]:
            if card["status"] == ACCEPTED or card["status"] in ARCHIVE or card["acceptance"] != OWNER:
                continue
            changed.append({"id": card["id"], "title": card["title"], "status": card["status"]})
            if dry_run:
                continue
            card["acceptance"] = REVIEWER
            _history(data, card, card["status"], card["status"], AGENT, MIGRATION_NOTE)
            _touch(card)
        if not dry_run:
            data["acceptance_migrated"] = _iso()
            _save(project, data, boards_dir)
        return changed


# ---------------------------------------------------------------- путь проекта: старт, финиш, спринты, туман


def list_sprints(project, boards_dir=None):
    """Спринты проекта копией, в порядке файла (то есть по номеру)."""
    return copy.deepcopy(_load(project, boards_dir)["sprints"])


def get_sprint(project, sprint_id, boards_dir=None):
    """Один спринт копией; нет такого — NotFoundError."""
    return copy.deepcopy(_find_sprint(_load(project, boards_dir), sprint_id))


def current_sprint(project, boards_dir=None):
    """Текущий спринт (current или closing) копией или None."""
    sprint = _active_sprint(_load(project, boards_dir))
    return copy.deepcopy(sprint) if sprint else None


def set_start(project, role, date, event, said=None, boards_dir=None):
    """Старт проекта: дата ГГГГ-ММ-ДД и событие («спека утверждена»).

    Агент задаёт старт один раз — пока он пуст; менять заданный старт — решение владельца
    (владелец в чате — с цитатой said).
    """
    role = parse_role(role)
    date = _text(date, "date", required=True)
    event = _text(event, "start_event", required=True)
    if not START_DATE.fullmatch(date):
        raise ValidationError(_err("date", f"дата старта вида ГГГГ-ММ-ДД, а не «{date}»"))
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise ValidationError(_err("date", f"такой даты нет: «{date}»")) from None
    _check_text(role, {"start_event": event})
    with _project_lock(project, boards_dir):
        data = _load(project, boards_dir)
        if data["start"] is not None:
            _owner_decision(role, said, "Менять заданный старт")
        elif role == OWNER_CHAT:
            _owner_decision(role, said, "Задать старт")
        data["start"] = {"date": date, "event": event}
        _save(project, data, boards_dir)
        return copy.deepcopy(data["start"])


def propose_finish(project, role, text, boards_dir=None):
    """Финиш одной фразой — предложение. Пока финиш не утверждён, менять текст может любая роль;
    утверждённый меняется только словом владельца (approve_finish с новым text)."""
    role = parse_role(role)
    text = _text(text, "finish", required=True)
    _check_text(role, {"finish": text})
    with _project_lock(project, boards_dir):
        data = _load(project, boards_dir)
        finish = data["finish"]
        if finish is not None and finish["approved"]:
            raise ForbiddenError(
                f"Финиш «{finish['text']}» утверждён владельцем: менять его может только владелец — "
                "новая формулировка записывается с его цитатой (approve_finish, --owner-said)."
            )
        if finish is None:
            finish = _normalize_finish({})
        finish["text"] = text
        finish["history"].append({
            "seq": _event(data), "at": _iso(), "who": role, "from": None, "to": "proposed",
            "said": "", "text": text,
        })
        data["finish"] = finish
        _save(project, data, boards_dir)
        return copy.deepcopy(finish)


def approve_finish(project, role, said=None, text=None, boards_dir=None):
    """Утвердить финиш — решение владельца: роль owner_chat только с цитатой said, роль owner без.

    text — новая формулировка словом владельца; без него утверждается предложенная. Повторное
    утверждение без нового текста — ValidationError: утверждать нечего.
    """
    said = _owner_decision(role, said, "Утвердить финиш")
    role = parse_role(role)
    text = _text(text, "finish")
    _check_text(role, {"finish": text})
    with _project_lock(project, boards_dir):
        data = _load(project, boards_dir)
        finish = data["finish"]
        if finish is None and not text:
            raise ValidationError(_err("finish", "финиш ещё не предложен: сначала propose_finish или text"))
        if finish is None:
            finish = _normalize_finish({})
        if finish["approved"] and not text:
            raise ValidationError(_err("finish", f"финиш «{finish['text']}» уже утверждён"))
        before = finish["text"]
        if text:
            finish["text"] = text
        finish["approved"] = True
        finish["approved_at"] = _iso()
        finish["history"].append({
            "seq": _event(data), "at": _iso(), "who": role,
            "from": before or None, "to": "approved", "said": said or "", "text": finish["text"],
        })
        data["finish"] = finish
        _save(project, data, boards_dir)
        return copy.deepcopy(finish)


def add_sprint(project, role, title, done_when, boards_dir=None):
    """Новый спринт со статусом «следующий»: id — s<номер по порядку>, спринты не удаляются."""
    role = parse_role(role)
    title = _text(title, "sprint_title", required=True)
    done_when = _text(done_when, "done_when", required=True)
    _check_text(role, {"sprint_title": title, "done_when": done_when})
    with _project_lock(project, boards_dir):
        data = _load(project, boards_dir)
        sprint = _blank_sprint()
        sprint.update({"id": f"s{len(data['sprints']) + 1}", "title": title, "done_when": done_when})
        _sprint_history(data, sprint, None, SPRINT_NEXT, role)
        data["sprints"].append(sprint)
        _save(project, data, boards_dir)
        return copy.deepcopy(sprint)


def open_sprint(project, role, sprint_id, said=None, boards_dir=None):
    """Открыть спринт: «следующий» → «текущий». Решение владельца (owner_chat — с цитатой).

    Текущий спринт всегда один: пока прежний не закрыт, второй не открывается (ValidationError).
    """
    said = _owner_decision(role, said, "Открыть спринт")
    role = parse_role(role)
    with _project_lock(project, boards_dir):
        data = _load(project, boards_dir)
        sprint = _find_sprint(data, sprint_id)
        if sprint["status"] != SPRINT_NEXT:
            raise ValidationError(_err(
                "sprint", f"спринт «{sprint['title']}» ({sprint['id']}) уже {SPRINT_STATUSES[sprint['status']]}"
            ))
        active = _active_sprint(data)
        if active is not None:
            raise ValidationError(_err(
                "sprint",
                f"текущий спринт уже есть: «{active['title']}» ({active['id']}, "
                f"{SPRINT_STATUSES[active['status']]}); сначала закрой его",
            ))
        sprint["status"] = SPRINT_CURRENT
        sprint["opened_at"] = _iso()
        _sprint_history(data, sprint, SPRINT_NEXT, SPRINT_CURRENT, role, said)
        _save(project, data, boards_dir)
        return copy.deepcopy(sprint)


def ready_sprint(project, role, sprint_id, proof=None, boards_dir=None):
    """Спринт готов: «текущий» → «ждёт закрытия». Агенту обязателен proof — чем доказано, что
    условие «закрыт, когда …» выполнено; закрывает спринт владелец (close_sprint)."""
    role = parse_role(role)
    proof = _text(proof, "sprint_proof")
    if role != OWNER and not proof:
        raise ValidationError(_err(
            "sprint_proof", "спринт переводится в «ждёт закрытия» только с доказательством: чем доказано, кем проверено"
        ))
    _check_text(role, {"sprint_proof": proof})
    with _project_lock(project, boards_dir):
        data = _load(project, boards_dir)
        sprint = _find_sprint(data, sprint_id)
        if sprint["status"] != SPRINT_CURRENT:
            raise ValidationError(_err(
                "sprint",
                f"в «ждёт закрытия» переходит только текущий спринт; «{sprint['title']}» ({sprint['id']}) — "
                f"{SPRINT_STATUSES[sprint['status']]}",
            ))
        if proof:
            sprint["proof"] = proof
        sprint["status"] = SPRINT_CLOSING
        _sprint_history(data, sprint, SPRINT_CURRENT, SPRINT_CLOSING, role, text=proof)
        _save(project, data, boards_dir)
        return copy.deepcopy(sprint)


def close_sprint(project, role, sprint_id, said=None, boards_dir=None):
    """Закрыть спринт: «ждёт закрытия» (или сразу «текущий») → «пройден». Решение владельца
    (owner_chat — с цитатой). Агент не может — даже спринт, который сам перевёл в «ждёт закрытия»."""
    said = _owner_decision(role, said, "Закрыть спринт")
    role = parse_role(role)
    with _project_lock(project, boards_dir):
        data = _load(project, boards_dir)
        sprint = _find_sprint(data, sprint_id)
        frm = sprint["status"]
        if frm not in SPRINT_ACTIVE:
            raise ValidationError(_err(
                "sprint", f"закрывается текущий спринт; «{sprint['title']}» ({sprint['id']}) — {SPRINT_STATUSES[frm]}"
            ))
        sprint["status"] = SPRINT_PASSED
        sprint["closed_at"] = _iso()
        _sprint_history(data, sprint, frm, SPRINT_PASSED, role, said)
        _save(project, data, boards_dir)
        return copy.deepcopy(sprint)


def set_fog(project, role, lines, boards_dir=None):
    """Список «что уже видно» за последним известным спринтом — целиком, до FOG_MAX строк."""
    role = parse_role(role)
    if isinstance(lines, str) or not isinstance(lines, (list, tuple)):
        raise ValidationError(_err("fog", "ожидался список строк"))
    clean = []
    for i, line in enumerate(lines, 1):
        text = _text(line, f"fog[{i}]")
        if not text:
            raise ValidationError(_err(f"fog[{i}]", "пустая строка в тумане не записывается"))
        clean.append(text)
    if len(clean) > FOG_MAX:
        raise ValidationError(_err("fog", f"{len(clean)} строк, предел {FOG_MAX}: туман — только то, что уже видно"))
    for line in clean:  # каждая строка отдельно — под пределом ключа fog
        _check_text(role, {"fog": line})
    with _project_lock(project, boards_dir):
        data = _load(project, boards_dir)
        data["fog"] = clean
        _save(project, data, boards_dir)
        return list(clean)


def set_progress_url(project, url, boards_dir=None):
    """Адрес опубликованной страницы пути (http или https); пустая строка снимает адрес."""
    url = _text(url, "progress_url") or ""
    if url and not PROGRESS_URL.fullmatch(url):
        raise ValidationError(_err("progress_url", f"адрес страницы начинается с http:// или https://, а не «{url}»"))
    with _project_lock(project, boards_dir):
        data = _load(project, boards_dir)
        data["progress_url"] = url
        _save(project, data, boards_dir)
        return url


# ---------------------------------------------------------------- inbox: что сделал владелец

_SPRINT_EVENT_KINDS = {
    (SPRINT_NEXT, SPRINT_CURRENT): "sprint_opened",
    (SPRINT_CURRENT, SPRINT_CLOSING): "sprint_ready",
    (SPRINT_CLOSING, SPRINT_PASSED): "sprint_closed",
    (SPRINT_CURRENT, SPRINT_PASSED): "sprint_closed",
    (None, SPRINT_NEXT): "sprint_added",
}


def read_inbox(project, boards_dir=None):
    """События владельца после курсора: возвраты на доработку, ответы, комментарии, новые карточки,
    приёмки, отложено и отменено. Каждое событие — {seq, kind, card_id, title, at, text, status}.

    События пути проекта (владелец на странице или в чате): sprint_opened, sprint_closed,
    finish_approved (и sprint_ready, sprint_added, finish_proposed, если их сделал владелец) —
    те же ключи, card_id None, title — название спринта или текст финиша, плюс sprint_id и said.
    """
    data = _load(project, boards_dir)
    cursor = int(data["inbox_cursor"])
    events = []
    owners = (OWNER, OWNER_CHAT)
    for sprint in data["sprints"]:
        for entry in sprint["history"]:
            if entry.get("who") not in owners or int(entry.get("seq", 0)) <= cursor:
                continue
            frm, to = entry.get("from"), entry.get("to")
            events.append({
                "seq": int(entry["seq"]), "kind": _SPRINT_EVENT_KINDS.get((frm, to), "sprint_move"),
                "card_id": None, "title": sprint["title"], "at": entry.get("at", ""),
                "text": entry.get("said") or entry.get("text", ""), "from": frm, "to": to,
                "status": sprint["status"], "sprint_id": sprint["id"], "said": entry.get("said", ""),
            })
    finish = data["finish"]
    if finish is not None:
        for entry in finish["history"]:
            if entry.get("who") not in owners or int(entry.get("seq", 0)) <= cursor:
                continue
            events.append({
                "seq": int(entry["seq"]),
                "kind": "finish_approved" if entry.get("to") == "approved" else "finish_proposed",
                "card_id": None, "title": entry.get("text") or finish["text"], "at": entry.get("at", ""),
                "text": entry.get("said") or entry.get("text", ""), "from": entry.get("from"),
                "to": entry.get("to"), "status": "approved" if finish["approved"] else "proposed",
                "sprint_id": None, "said": entry.get("said", ""),
            })
    for card in data["cards"]:
        for entry in card["history"]:
            if entry.get("who") != OWNER or int(entry.get("seq", 0)) <= cursor:
                continue
            frm, to = entry.get("from"), entry.get("to")
            if frm is None:
                kind = "new_card"
            elif frm in (DONE, ACCEPTED) and to == IN_PROGRESS:
                kind = "rework"
            elif frm == REVIEW and to == IN_PROGRESS:
                kind = "answer"
            elif to == ACCEPTED:
                kind = "accepted"
            elif to in ARCHIVE:
                kind = "archived"
            else:
                kind = "move"
            events.append({
                "seq": int(entry["seq"]), "kind": kind, "card_id": card["id"], "title": card["title"],
                "at": entry.get("at", ""), "text": entry.get("text", ""),
                "from": frm, "to": to, "status": card["status"],
            })
        for note in card["comments"]:
            if note.get("who") != OWNER or int(note.get("seq", 0)) <= cursor or note.get("move"):
                continue  # комментарий, написанный вместе с переносом, уже показан самим переносом
            events.append({
                "seq": int(note["seq"]), "kind": "comment", "card_id": card["id"], "title": card["title"],
                "at": note.get("at", ""), "text": note.get("text", ""),
                "from": None, "to": None, "status": card["status"],
            })
    return sorted(events, key=lambda e: e["seq"])


def ack_inbox(project, upto=None, boards_dir=None):
    """Сдвинуть курсор inbox. Повторный вызов read_inbox тех же событий уже не покажет."""
    with _project_lock(project, boards_dir):
        data = _load(project, boards_dir)
        cursor = int(data["event_counter"]) if upto is None else _int(upto, "upto")
        data["inbox_cursor"] = max(int(data["inbox_cursor"]), cursor)
        _save(project, data, boards_dir)
        return data["inbox_cursor"]


# ---------------------------------------------------------------- сводка «все проекты»


def summary(boards_dir=None, now=None):
    """Списки по всем проектам: ждёт тебя, не закончено, сделано вчера, сессии без карточки, broken.

    now — текущее время (для тестов). Список sessions_without_card пока всегда пуст (волна 2).
    broken — файлы проектов, которые не прочитались: [{project, error}]. Страница показывает их
    первой строкой сводки, иначе проект с испорченным файлом молча пропал бы с доски.
    """
    moment = _parse_dt(now) if isinstance(now, str) else now
    moment = moment or _now()
    if moment.tzinfo is None:
        moment = moment.astimezone()
    yesterday = (moment - timedelta(days=1)).date()
    stale_after = timedelta(days=STALE_DAYS)

    waiting, unfinished, yesterday_done, broken = [], [], [], []
    verify_pending = 0
    for name in list_projects(boards_dir):
        try:
            data = _load(name, boards_dir)
        except BoardError as exc:
            broken.append({"project": name, "error": str(exc)})
            continue
        for card in data["cards"]:
            base = {
                "project": data["project"], "id": card["id"], "title": card["title"],
                "kind": card["kind"], "stage": card["stage"], "status": card["status"],
            }
            touched = _parse_dt(card["updated_at"]) or _parse_dt(card["created_at"])
            # «Ждёт тебя» — «На согласовании» и «Выполнена» с приёмкой владельца; «Выполнена» с приёмкой
            # проверяющего идёт счётчиком verify_pending (спека 2026-09-29, пункт 13).
            if card["status"] == DONE and card["acceptance"] == REVIEWER:
                verify_pending += 1
            elif card["status"] in (REVIEW, DONE):
                waiting.append({
                    **base, "ask": card["ask"], "accept_how": card["accept_how"], "proof": card["proof"],
                    "acceptance": card["acceptance"], "tag": card["tag"], "priority": card["priority"],
                    "touched_at": card["updated_at"],
                })
            if card["status"] == IN_PROGRESS:
                idle = (moment - touched) if touched else timedelta(0)
                unfinished.append({
                    **base, "stopped_at": card["stopped_at"], "resume": card["resume"],
                    "session": card["session"], "rework": card["rework"], "touched_at": card["updated_at"],
                    "idle_days": round(idle.total_seconds() / 86400, 2),
                    "stale": idle > stale_after,
                })
            # Одна строка на карточку: reached — последний статус, которого она вчера достигла.
            last = None
            for entry in card["history"]:
                if entry.get("to") not in (DONE, ACCEPTED):
                    continue
                when = _parse_dt(entry.get("at"))
                if when is None or when.astimezone(moment.tzinfo).date() != yesterday:
                    continue
                last = entry
            if last is not None:
                yesterday_done.append({
                    **base, "reached": last["to"], "at": last.get("at", ""), "who": last.get("who", ""),
                    "proof": card["proof"], "caveat": card["caveat"],
                })

    waiting.sort(key=lambda c: (c["project"], c["priority"], c["id"]))
    unfinished.sort(key=lambda c: (-c["idle_days"], c["project"], c["id"]))
    yesterday_done.sort(key=lambda c: (c["at"], c["id"]))
    return {
        "now": _iso(moment),
        "waiting": waiting,
        "unfinished": unfinished,
        "done_yesterday": yesterday_done,
        "verify_pending": verify_pending,
        "sessions_without_card": [],
        "broken": broken,
    }
