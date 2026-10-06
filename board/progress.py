"""Чистая логика доски-прогресс-бара проекта (спека владельца 2026-09-24,
план от 2026-09-24, часть B, задачи B1-B6).

Модуль без ввода-вывода: ничего не читает и не пишет на диск, ничего не печатает. На входе —
словарь проекта в виде, в каком его отдаёт `boardlib._normalize_project` (плюс новые ключи
`start`, `finish`, `sprints`, `fog` из раздела 2 плана — их в файле проекта может пока не быть,
тогда считаем их отсутствующими: `start=None`, `finish=None`, `sprints=[]`, `fog=[]`). На выходе —
структура `layout`, из которой отдельный модуль (`progress_page.py`, часть C) соберёт HTML.

Статусы карточек берутся из `boardlib` — единственного места, где они определены (см. `AGENT.md`
«Сначала оригинал»: `boardlib.py` не правится этим модулем, только читается).
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

try:
    import boardlib as _bl

    BACKLOG = _bl.BACKLOG
    NEW = _bl.NEW
    IN_PROGRESS = _bl.IN_PROGRESS
    REVIEW = _bl.REVIEW
    DONE = _bl.DONE
    ACCEPTED = _bl.ACCEPTED
    POSTPONED = _bl.POSTPONED
    CANCELLED = _bl.CANCELLED
    status_title = _bl.status_title
    # Статусы спринта и предел строк тумана — из boardlib (задача A1), единственного места, где они
    # определены; см. `AGENT.md` «Сначала оригинал».
    NEXT = _bl.SPRINT_NEXT
    CURRENT = _bl.SPRINT_CURRENT
    CLOSING = _bl.SPRINT_CLOSING
    PASSED = _bl.SPRINT_PASSED
    FOG_MAX = _bl.FOG_MAX
    ACCEPTANCE_REVIEWER = _bl.REVIEWER
    ROLE_REVIEWER = _bl.REVIEWER
except ImportError:  # pragma: no cover - запасной путь, если boardlib недоступен
    BACKLOG = "backlog"
    NEW = "new"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    DONE = "done"
    ACCEPTED = "accepted"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"

    def status_title(key):
        return str(key)

    NEXT = "next"
    CURRENT = "current"
    CLOSING = "closing"
    PASSED = "passed"
    FOG_MAX = 5
    ACCEPTANCE_REVIEWER = "reviewer"
    ROLE_REVIEWER = "reviewer"

SPRINT_STATUSES = (NEXT, CURRENT, CLOSING, PASSED)

# Пометка задачи в блоке «Сейчас» по колонке: waiting — ждёт владельца, done — принята.
TASK_MARKS = {"todo": "", "waiting": "ждёт тебя", "done": "готово"}
AGENT_ACCEPTED_NOTE = "вернуть принятое агентом можно на доске"

# Название виртуального спринта проекта без спринтов (решение 6 спеки, B3/B6 плана).
UNNAMED_SPRINT = "Без названия"

# Порядок приоритетов карточки при сортировке «Нужно от тебя» (A — сейчас, B — следом, C — потом).
_PRIORITY_ORDER = {"A": 0, "B": 1, "C": 2}


def _err(field, message):
    """Строка ошибки в формате доски (см. boardlib._err): два пробела после слова ОШИБКА."""
    return f"ОШИБКА  {field}: {message}"


def _note(field, message):
    """Строка мягкого замечания — не останавливает сборку, только предупреждает владельца."""
    return f"ЗАМЕЧАНИЕ  {field}: {message}"


# ---------------------------------------------------------------- B1: колонка карточки


def column_of(card):
    """Колонка прогресс-бара для карточки текущего спринта, либо None для архива.

    backlog/new/in_progress -> "todo" («Сделать»); review и done с приёмкой владельца -> "waiting"
    («Ждёт тебя»); done с приёмкой проверяющего -> "todo" с пометкой «ждёт проверки» (спека
    2026-09-29, пункт 14); accepted -> "done" («Готово»); postponed/cancelled (архив) и неизвестные
    статусы -> None, архив на полосе не показывается (спека, раздел «Понятия»).
    """
    status = card.get("status")
    if status in (BACKLOG, NEW, IN_PROGRESS):
        return "todo"
    if status == DONE:
        return "todo" if card.get("acceptance") == ACCEPTANCE_REVIEWER else "waiting"
    if status == REVIEW:
        return "waiting"
    if status == ACCEPTED:
        return "done"
    return None


def mark_of(card):
    """Пометка задачи в блоке «Сейчас»: «ждёт проверки» у «Выполнена» с приёмкой проверяющего,
    «принял агент» у принятой проверяющим, «ждёт тебя» / «готово» / пусто — по колонке."""
    col = column_of(card)
    status = card.get("status")
    if status == DONE and col == "todo":
        return "ждёт проверки"
    if status == ACCEPTED and _accepted_by(card) == ROLE_REVIEWER:
        return "принял агент"
    return TASK_MARKS.get(col, "")


def _accepted_by(card):
    for entry in reversed(card.get("history") or []):
        if entry.get("to") == ACCEPTED:
            return entry.get("who")
    return None


def agent_accepted_recently(cards, today, days=7):
    """Принял ли проверяющий хотя бы одну карточку за последние `days` дней (для блока «Нужно от тебя»)."""
    if isinstance(today, datetime):
        today = today.date()
    elif not isinstance(today, date):
        return False
    for card in cards:
        for entry in card.get("history") or []:
            if entry.get("to") != ACCEPTED or entry.get("who") != ROLE_REVIEWER:
                continue
            m = str(entry.get("at") or "")[:10]
            try:
                when = date.fromisoformat(m)
            except ValueError:
                continue
            if 0 <= (today - when).days <= days:
                return True
    return False


# ---------------------------------------------------------------- B2: карточки спринта


def cards_of_sprint(cards, sprint_id, is_current=False):
    """Карточки, которые показываются в спринте `sprint_id`.

    Привязанные к спринту (card["sprint"] == sprint_id) — всегда. Открытые карточки без
    спринта (card["sprint"] is None) — только для текущего спринта (`is_current=True`);
    завершённые карточки без спринта и архив (column_of -> None) нигде не показываются.
    Для проекта без спринтов `sprint_id` тоже `None` — тогда все открытые карточки без
    спринта естественно попадают в единственный (виртуальный) спринт.
    """
    result = []
    for card in cards:
        col = column_of(card)
        if col is None:
            continue  # архив не показывается
        card_sprint = card.get("sprint")
        if sprint_id is not None and card_sprint == sprint_id:
            result.append(card)
        elif is_current and card_sprint is None and col != "done":
            result.append(card)
    return result


def _card_brief(card):
    """Минимальный набор полей карточки для колонки прогресс-бара."""
    return {
        "id": card.get("id", ""),
        "title": card.get("title", ""),
        "note": ((card.get("body") or "").strip().splitlines() or [""])[0],
        "status": card.get("status"),
        "priority": card.get("priority"),
    }


def _sorted_cards(cards):
    return sorted(cards, key=lambda c: (c.get("order") or 0, c.get("id") or ""))


# ---------------------------------------------------------------- B3: счётчик «N из M»


def counter(sprints):
    """«Спринт N из известных M». Без спринтов — виртуальный один из одного: (1, 1).

    N — порядковый номер текущего (current/closing) спринта считая пройденные; если текущего
    нет (только "next"-спринты, спринт ещё не открыт) — N = 0, сборщик layout добавит замечание.
    """
    if not sprints:
        return {"n": 1, "m": 1}
    m = len(sprints)
    n = 0
    for i, sprint in enumerate(sprints, start=1):
        status = sprint.get("status")
        if status == PASSED:
            n = i
        elif status in (CURRENT, CLOSING):
            n = i
            break
    return {"n": n, "m": m}


# ---------------------------------------------------------------- B4: полоса пути


def bar(data):
    """Отрезки полосы по порядку: спринты (или виртуальный «Без названия»), затем туман
    (всегда), затем финиш («финиш не утверждён», пока approved не True).
    """
    segments = []
    sprints = data.get("sprints") or []
    if not sprints:
        segments.append({"kind": CURRENT, "title": UNNAMED_SPRINT})
    else:
        for sprint in sprints:
            segments.append({
                "kind": sprint.get("status") or NEXT,
                "title": sprint.get("title", ""),
            })
    segments.append({"kind": "fog", "title": "туман"})
    finish = data.get("finish") or {}
    if finish.get("approved"):
        segments.append({"kind": "finish", "title": finish.get("text") or ""})
    else:
        segments.append({"kind": "finish", "title": "финиш не утверждён"})
    return segments


# ---------------------------------------------------------------- B5: «Нужно от тебя»


def need(data, sprints, current_sprint, waiting_cards):
    """Список того, что ждёт решения владельца, в порядке: спринт ждёт закрытия, неутверждённый
    финиш, предложенный следующий спринт (только если текущего спринта — current или closing — нет:
    пока он идёт, открытие следующего всё равно будет отказано boardlib.open_sprint), затем
    карточки в «Ждёт тебя» по приоритету (A, B, C).
    """
    items = []
    if current_sprint is not None and current_sprint.get("status") == CLOSING:
        items.append({
            "title": current_sprint.get("title", ""),
            "why": "спринт ждёт закрытия",
        })
    finish = data.get("finish") or {}
    if finish.get("text") and not finish.get("approved"):
        items.append({"title": finish.get("text", ""), "why": "финиш ждёт подтверждения"})
    if current_sprint is None:
        next_sprint = next((s for s in sprints if s.get("status") == NEXT), None)
        if next_sprint is not None:
            items.append({"title": next_sprint.get("title", ""), "why": "новый спринт ждёт открытия"})
    for card in sorted(
        waiting_cards,
        key=lambda c: (_PRIORITY_ORDER.get(c.get("priority"), len(_PRIORITY_ORDER)), c.get("order") or 0),
    ):
        # У карточки в «На согласовании» есть ask — вопрос владельцу (пишет boardlib.move_card при
        # `board move … --ask`); он точнее, чем название колонки, поэтому в «why» приоритет за ним.
        ask = str(card.get("ask") or "").strip()
        why = ask if ask else status_title(card.get("status"))
        items.append({"title": card.get("title", ""), "why": why})
    return items


def need_agent_note(cards, today):
    """Строка «вернуть принятое агентом можно на доске» — только если за неделю проверяющий
    принял хотя бы одну карточку (спека 2026-09-29, пункт 14)."""
    if agent_accepted_recently(cards, today):
        return [{"title": AGENT_ACCEPTED_NOTE, "why": "принято агентом за неделю"}]
    return []


# ---------------------------------------------------------------- B6: проверка данных


def _loose_cards_note(data):
    """Замечание: в проекте со спринтами есть открытые карточки без спринта (архив не считается).
    Открытые такие карточки страница показывает в текущем спринте, завершённые — нигде, поэтому
    владельцу стоит знать, что их не привязали. Для проекта без спринтов замечания нет: там любая
    открытая карточка и так попадает в единственный виртуальный спринт.
    """
    if not data.get("sprints"):
        return []
    loose = [
        c.get("id") for c in data.get("cards", [])
        if c.get("sprint") is None and c.get("status") not in (POSTPONED, CANCELLED)
    ]
    if not loose:
        return []
    shown = ", ".join(loose[:5]) + (" …" if len(loose) > 5 else "")
    return [
        f"ЗАМЕЧАНИЕ  cards: без спринта карточек {len(loose)} ({shown}) — открытые показаны в текущем "
        "спринте, завершённые на странице не видны; привяжи: board sprint assign <карточка> --sprint sN"
    ]


def check(data):
    """Проверка данных проекта. Возвращает список строк: жёсткие ошибки («ОШИБКА …» — два
    текущих спринта, карточка ссылается на неизвестный id спринта, тумана больше FOG_MAX строк)
    и мягкие замечания («ЗАМЕЧАНИЕ …» — нет текущего спринта, только следующие; открытые карточки
    без спринта). Пустой список — данные в порядке. Предел тумана и чужой id спринта у карточки
    проверяются и у проекта без спринтов: тумана может быть больше нормы, а карточка — ссылаться на
    спринт, которого никогда не было (известных id тогда нет вовсе — любой sprint у карточки чужой).
    """
    warnings = []
    sprints = data.get("sprints") or []

    current_like = [s for s in sprints if s.get("status") in (CURRENT, CLOSING)]
    if len(current_like) > 1:
        warnings.append(_err("sprints", f"текущих спринтов {len(current_like)}, должен быть один"))

    known_ids = {s.get("id") for s in sprints}
    for card in data.get("cards", []):
        sprint_id = card.get("sprint")
        if sprint_id is not None and sprint_id not in known_ids:
            warnings.append(
                _err("cards", f"карточка «{card.get('id')}» ссылается на неизвестный спринт «{sprint_id}»")
            )

    fog = data.get("fog") or []
    if len(fog) > FOG_MAX:
        warnings.append(_err("fog", f"строк тумана {len(fog)}, допустимо не больше {FOG_MAX}"))

    if sprints and not current_like and any(s.get("status") == NEXT for s in sprints):
        warnings.append(_note("sprints", "нет текущего спринта, только следующие"))

    warnings.extend(_loose_cards_note(data))

    return warnings


# ---------------------------------------------------------------- сборка layout


def _past(sprints):
    return [
        {"title": s.get("title", ""), "closed_at": s.get("closed_at", "")}
        for s in sprints
        if s.get("status") == PASSED
    ]


def _next_title(sprints):
    for s in sprints:
        if s.get("status") == NEXT:
            return s.get("title", "")
    return None


def _passed_full(sprints):
    """Пройденные спринты с условием — для пути на странице (вид Б): название, условие, дата."""
    return [
        {"title": s.get("title", ""), "done_when": s.get("done_when", "") or "",
         "closed_at": s.get("closed_at", "") or ""}
        for s in sprints
        if s.get("status") == PASSED
    ]


def _upcoming(sprints):
    """Все следующие спринты по порядку (ключ next отдаёт только название первого)."""
    return [
        {"title": s.get("title", ""), "done_when": s.get("done_when", "") or ""}
        for s in sprints
        if s.get("status") == NEXT
    ]


def _tasks(sprint_cards):
    """Задачи текущего спринта одним списком в порядке доски, с колонкой и пометкой."""
    result = []
    for card in sprint_cards:
        col = column_of(card)
        if col is None:
            continue
        item = _card_brief(card)
        item["column"] = col
        item["mark"] = mark_of(card)
        result.append(item)
    return result


def layout(data, today=None):
    """Собирает структуру страницы пути из данных проекта. `today` — дата сборки (по умолчанию
    сегодня, параметр — ради воспроизводимых тестов). Без ввода-вывода и без исключений: любые
    неполадки данных попадают в `warnings`, сборка всё равно возвращается.
    """
    if today is None:
        today = date.today()
    updated = today.isoformat() if hasattr(today, "isoformat") else str(today)

    sprints = data.get("sprints") or []
    cards = data.get("cards") or []
    warnings = check(data)

    current_sprint = None
    for sprint in sprints:
        if sprint.get("status") in (CURRENT, CLOSING):
            current_sprint = sprint
            break

    if current_sprint is not None:
        current = current_sprint
        sprint_id = current.get("id")
    else:
        # Нет открытого спринта: либо проект без спринтов (виртуальный «Без названия»),
        # либо всё ещё в «next» — колонки пусты, но страница собирается (решение 6 спеки).
        current = {"id": None, "title": UNNAMED_SPRINT, "done_when": "", "status": CURRENT}
        sprint_id = None

    sprint_cards = _sorted_cards(cards_of_sprint(cards, sprint_id, is_current=True)) if (
        current_sprint is not None or not sprints
    ) else []

    columns = {"todo": [], "waiting": [], "done": []}
    for card in sprint_cards:
        col = column_of(card)
        if col is not None:
            columns[col].append(card)

    finish_raw = data.get("finish") or {}
    finish = {
        "text": finish_raw.get("text", "") or "",
        "approved": bool(finish_raw.get("approved", False)),
    }

    return {
        "project": data.get("project", ""),
        "phrase": data.get("phrase", ""),
        "updated": updated,
        "start": data.get("start"),
        "finish": finish,
        "bar": bar(data),
        "counter": counter(sprints),
        "current": {
            "title": current.get("title", ""),
            "done_when": current.get("done_when", ""),
            "status": current.get("status", CURRENT),
        },
        "columns": {
            "todo": [_card_brief(c) for c in columns["todo"]],
            "waiting": [_card_brief(c) for c in columns["waiting"]],
            "done": [_card_brief(c) for c in columns["done"]],
        },
        "need": need(data, sprints, current_sprint, columns["waiting"]) + need_agent_note(cards, today),
        "past": _past(sprints),
        "next": _next_title(sprints),
        "fog": list(data.get("fog") or []),
        "warnings": warnings,
        # Для вида Б (решение владельца 2026-09-24: «дорога к финишу»): путь и список задач «Сейчас».
        "passed": _passed_full(sprints),
        "upcoming": _upcoming(sprints),
        "tasks": _tasks(sprint_cards),
    }
