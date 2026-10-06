"""Страница пути проекта: результат progress.layout(...) → HTML по шаблону templates/progress.html.

Вид Б «дорога к финишу» (решение владельца 2026-09-24): слева вертикальный путь снизу
вверх (старт → пройденные → «Сейчас» со списком задач → следующие → туман → финиш), справа блоки
«До финиша», «Нужно от тебя», «Что уже видно за туманом». До 820 px — одна колонка.

Чистая функция render(layout) -> str, без чтения доски и без записи файлов. Весь текст из layout
проходит через html.escape. Процентов на странице нет (спека 2026-09-24, решение 3).

Шаблон размечен блоками <!-- BOARD:NAME --> … <!-- /BOARD:NAME -->; put() меняет содержимое блока,
метки остаются (тот же приём, что был в прежнем сборщике доски-артефакта, удалён 2026-09-24).

Ключи layout, которые читает render:
  project, phrase, updated,
  start {date, event}, finish {text, approved},
  bar [{kind: start|passed|current|closing|next|fog|finish, title}] — полоса «До финиша» и признак,
      есть ли открытый спринт,
  current {title, done_when, status: current|closing},
  tasks [{title, note, column: todo|waiting|done, mark}] — задачи «Сейчас» одним списком
      (нет ключа — собираются из columns {todo, waiting, done}),
  passed [{title, done_when, closed_at}] (нет — из past [{title, closed_at}]),
  upcoming [{title, done_when}] (нет — из next: строка | {title, done_when} | список),
  need [{title, why}], fog [строка]. counter — подписью «спринт N из известных M» в блоке «До финиша»; warnings на странице не показываются.
Отсутствующий ключ даёт пустое состояние словами, а не ошибку.

Проверка: PYTHONIOENCODING=utf-8 python board/progress_page.py <куда-записать.html>
"""

import html
import re
import sys
from datetime import date, datetime
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent / "templates" / "progress.html"
TITLE_STUB = "<title>Путь ПРОЕКТ</title>"
BLOCKS = ("HEAD", "ROAD", "TOFINISH", "NEED", "FOG", "FOOT")


def esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def put(page, name, body):
    rx = re.compile(r"(<!-- BOARD:%s -->)(.*?)(<!-- /BOARD:%s -->)" % (name, name), re.S)
    if len(rx.findall(page)) != 1:
        raise ValueError(f"в шаблоне нет блока BOARD:{name} или он не один")
    return rx.sub(lambda m: m.group(1) + "\n" + body.strip("\n") + "\n" + m.group(3), page)


def short_date(value):
    """'2026-09-23' или '2026-09-23T10:00:00+03:00' → '23.09'; иное — как есть."""
    if not value:
        return ""
    if isinstance(value, (date, datetime)):
        return value.strftime("%d.%m")
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(value))
    return f"{m.group(3)}.{m.group(2)}" if m else str(value)


def plural(n, one, few, many):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


# --- данные из layout (с запасом для layout без новых ключей) ---------------------------------------

def finish_of(layout):
    fin = layout.get("finish") or {}
    return (fin.get("text") or "").strip(), bool(fin.get("approved"))


def segments(layout):
    """Отрезки полосы без старта и финиша: их рисуют отдельно из start/finish."""
    return [b for b in (layout.get("bar") or []) if b.get("kind") not in ("start", "finish")]


def has_now(layout):
    """Открытый спринт есть, если на полосе есть current или closing (у проекта без спринтов —
    виртуальный current). Без полосы — смотрим на current."""
    bar = layout.get("bar")
    if bar is None:
        return bool(layout.get("current"))
    return any(b.get("kind") in ("current", "closing") for b in bar)


def passed_of(layout):
    items = layout.get("passed")
    if items is None:
        items = layout.get("past") or []
    return [i for i in items if isinstance(i, dict)]


def upcoming_of(layout):
    items = layout.get("upcoming")
    if items is None:
        nxt = layout.get("next")
        items = nxt if isinstance(nxt, list) else ([nxt] if nxt else [])
    out = []
    for i in items:
        if isinstance(i, dict) and i.get("title"):
            out.append({"title": i.get("title"), "done_when": i.get("done_when") or ""})
        elif i and not isinstance(i, dict):
            out.append({"title": str(i), "done_when": ""})
    return out


MARKS = {"todo": "", "waiting": "ждёт тебя", "done": "готово"}


def tasks_of(layout):
    items = layout.get("tasks")
    if items is not None:
        return [t for t in items if isinstance(t, dict)]
    columns = layout.get("columns") or {}
    out = []
    for col in ("waiting", "todo", "done"):
        for card in columns.get(col) or []:
            out.append({**card, "column": col, "mark": MARKS[col]})
    return out


# --- блоки -----------------------------------------------------------------------------------------

def head(layout):
    project = layout.get("project") or "Проект без имени"
    phrase = layout.get("phrase")
    what = f'<p class="what">{esc(phrase)}</p>' if phrase else ""
    updated = short_date(layout.get("updated"))
    meta = f'<p class="meta">обновлено {esc(updated)}</p>' if updated else ""
    return f"  <div><h1>{esc(project)}</h1>{what}</div>\n  {meta}"


def step(cls, title, note="", when="", inner="", eyebrow=""):
    """Один шаг пути: рельс (точка и линия вниз к предыдущему шагу) и текст. title — уже экранирован."""
    eyebrow_html = f'<p class="rd-eyebrow">{esc(eyebrow)}</p>' if eyebrow else ""
    when_html = f'<span class="rd-when">{esc(when)}</span>' if when else ""
    note_html = f'<p class="rd-note">{esc(note)}</p>' if note else ""
    return (f'      <li class="rd-step {cls}"><div class="rd-rail"><span class="rd-dot"></span>'
            f'<span class="rd-line"></span></div><div class="rd-body">{eyebrow_html}'
            f'<div class="rd-top"><p class="rd-title">{title}</p>{when_html}</div>{note_html}{inner}</div></li>')


def task_html(task):
    col = task.get("column") or "todo"
    mark = task.get("mark")
    if mark is None:
        mark = MARKS.get(col, "")
    mark_cls = {"waiting": "rd-mark-wait", "done": "rd-mark-done"}.get(col, "")
    if col == "todo" and mark:
        mark_cls = "rd-mark-verify"  # «ждёт проверки» — карточка в «Выполнена» у проверяющего
    task_cls = {"waiting": "rd-task-wait", "done": "rd-task-done"}.get(col, "")
    mark_html = f'<span class="rd-mark {mark_cls}">{esc(mark)}</span>' if mark else ""
    note = task.get("note") or task.get("body") or ""
    note_html = f'<span class="rd-task-note">{esc(note)}</span>' if note else ""
    return (f'<li class="rd-task {task_cls}"><div class="rd-task-head">'
            f'<span class="rd-task-title">{esc(task.get("title") or "Без названия")}</span>{mark_html}</div>'
            f"{note_html}</li>")


def now_step(layout):
    cur = layout.get("current") or {}
    if not has_now(layout):
        return step("rd-now", "Текущего спринта нет", note="Новый спринт открывает владелец словом в чате.",
                    when="пауза", eyebrow="Сейчас")
    closing = cur.get("status") == "closing"
    badge = '<span class="rd-badge">ждёт закрытия</span>' if closing else ""
    title = f"{esc(cur.get('title') or 'Без названия')}{badge}"
    done_when = (cur.get("done_when") or "").strip()
    cond = f"Закрыт, когда {done_when}" if done_when else "Условие закрытия не записано."
    tasks = tasks_of(layout)
    done = sum(1 for t in tasks if t.get("column") == "done")
    total = len(tasks)
    count = f'<p class="rd-count">{done} из {total} {plural(total, "задачи", "задач", "задач")} готово</p>' \
        if total else ""
    body = (f'<ul class="rd-tasks">{"".join(task_html(t) for t in tasks)}</ul>' if tasks
            else '<p class="rd-tasks-empty">Задач в спринте пока нет.</p>')
    return step("rd-now", title, note=cond, when="" if closing else "в работе",
                inner=count + body, eyebrow="Сейчас")


def road(layout):
    """Путь сверху вниз: финиш, туман, следующие (дальний выше), «Сейчас», пройденные (новый выше), старт."""
    rows = []
    text, approved = finish_of(layout)
    if approved:
        rows.append(step("rd-goal", esc(f"Финиш: {text}" if text else "Финиш"), when="цель"))
    else:
        note = f"Предложено: {text}" if text else "Финиш задаёт владелец словом в чате."
        rows.append(step("rd-goal rd-draft", "Финиш не утверждён", note=note, when="не утверждён"))
    rows.append(step("rd-haze", "Туман", note="шаги появятся по ходу", when="—"))
    for nxt in reversed(upcoming_of(layout)):
        when_text = nxt.get("done_when")
        rows.append(step("rd-upcoming", esc(nxt.get("title")),
                         note=f"Закрыт, когда {when_text}" if when_text else "", when="дальше"))
    rows.append(now_step(layout))
    passed = passed_of(layout)
    # Порядок пройденных — хронологический; на пути новый выше. Сортируем по дате закрытия, если она есть.
    ordered = sorted(enumerate(passed), key=lambda p: (str(p[1].get("closed_at") or ""), p[0]))
    for _, it in reversed(ordered):
        rows.append(step("rd-passed", esc(it.get("title") or "Без названия"),
                         note=it.get("done_when") or "", when=short_date(it.get("closed_at"))))
    st = layout.get("start") or {}
    event = (st.get("event") or "").strip()
    rows.append(step("rd-start", esc(f"Старт: {event}" if event else "Старт"),
                     note="" if st.get("date") else "Дата старта не записана.",
                     when=short_date(st.get("date"))))
    return "\n".join(rows)


SEG_CLASS = {"passed": "bx-seg-passed", "current": "bx-seg-now", "closing": "bx-seg-closing",
             "next": "bx-seg-upcoming", "fog": "bx-seg-haze"}


def to_finish(layout):
    passed = len(passed_of(layout))
    ahead = len(upcoming_of(layout)) + (1 if has_now(layout) else 0)
    verb = "пройден" if plural(passed, 1, 2, 3) == 1 else "пройдено"
    ahead_text = f"впереди известно ещё {ahead}" if ahead else "впереди — только туман"
    score = (f'    <div class="bx-score"><span class="bx-score-num">{passed}</span>'
             f'<span class="bx-score-text">{plural(passed, "спринт", "спринта", "спринтов")} {verb}, '
             f"{esc(ahead_text)}</span></div>")
    parts = []
    for seg in segments(layout):
        cls = SEG_CLASS.get(seg.get("kind") or "next", "bx-seg-upcoming")
        parts.append(f'<span class="bx-seg {cls}" title="{esc(seg.get("title"))}"></span>')
    if not any(s.get("kind") == "fog" for s in segments(layout)):
        parts.append('<span class="bx-seg bx-seg-haze" title="туман"></span>')
    _, approved = finish_of(layout)
    parts.append('<span class="bx-flag"></span>' if approved else '<span class="bx-flag bx-flag-draft"></span>')
    strip = ('    <div class="bx-strip" role="img" aria-label="Полоса пути: зелёные отрезки пройдены, синий — '
             'текущий спринт, серые — следующие, пунктир — туман, флажок — финиш">' + "".join(parts) + "</div>")
    counter = layout.get("counter") or {}
    count_line = ""
    if counter.get("m"):
        # подпись решения 3 спеки: прогресс спринтами, не процентами
        count_line = (f'    <p class="bx-hint bx-counter">спринт {int(counter.get("n") or 0)} из известных '
                      f'{int(counter["m"])} · дальше — по ходу</p>')
    hint = ('    <p class="bx-hint">Пунктир — участок, где шаги ещё не видны. Он укорачивается, '
            "когда появляются новые спринты.</p>")
    return "\n".join(x for x in (score, count_line, strip, hint) if x)


def need(layout):
    items = layout.get("need") or []
    if not items:
        return '    <p class="empty">Сейчас от тебя ничего не нужно.</p>'
    rows = []
    for it in items:
        why = it.get("why")
        tail = f'<span class="bx-need-why">{esc(why)}</span>' if why else ""
        rows.append(f"<li><b>{esc(it.get('title'))}</b>{tail}</li>")
    return '    <ol class="bx-need-list">' + "".join(rows) + "</ol>"


def fog(layout):
    lines = [l for l in (layout.get("fog") or []) if l]
    if not lines:
        return '    <p class="empty">За туманом пока ничего не видно.</p>'
    return '    <ul class="bx-haze-list">' + "".join(f"<li>{esc(l)}</li>" for l in lines) + "</ul>"


def foot(layout):
    updated = short_date(layout.get("updated"))
    when = f" Собрано {esc(updated)}." if updated else ""
    return ("Данные: канбан-доска проекта. Страница только показывает; менять — на локальной доске "
            f"и командами.{when}")


def render(layout, template=None):
    page = template if template is not None else TEMPLATE.read_text(encoding="utf-8")
    if page.count(TITLE_STUB) != 1:
        raise ValueError("в шаблоне нет заголовка-заготовки или он не один")
    page = page.replace(TITLE_STUB, f"<title>Путь {esc(layout.get('project') or 'проекта')}</title>", 1)
    fill = {"HEAD": head, "ROAD": road, "TOFINISH": to_finish, "NEED": need, "FOG": fog, "FOOT": foot}
    for name in BLOCKS:
        page = put(page, name, fill[name](layout))
    return page


# --- проверочный образец (демо-проект) ---------------------------------------------------------------

_TASKS = [
    {"id": "c43", "title": "Ключ сервиса рассылки", "note": "без ключей очередь не публикует",
     "column": "waiting", "mark": "ждёт тебя"},
    {"id": "c41", "title": "Аккаунты каналов", "note": "куда публиковать первую страницу",
     "column": "todo", "mark": ""},
    {"id": "c42", "title": "Первый прогон", "note": "страница прошла очередь и опубликована",
     "column": "todo", "mark": ""},
]

SAMPLE = {
    "project": "demo",
    "phrase": "Публикация страниц сайта в каналы через очередь.",
    "updated": "2026-09-24",
    "start": {"date": "2026-09-19", "event": "спека и план утверждены"},
    "finish": {"text": "легаси выключен", "approved": True},
    "bar": [
        {"kind": "passed", "title": "Волна 1"},
        {"kind": "passed", "title": "Волна 2"},
        {"kind": "passed", "title": "Волна 3"},
        {"kind": "current", "title": "Ключи и первый прогон"},
        {"kind": "next", "title": "Волна 4"},
        {"kind": "fog", "title": "туман"},
        {"kind": "finish", "title": "легаси выключен"},
    ],
    "counter": {"n": 4, "m": 5},
    "current": {"title": "Ключи и первый прогон",
                "done_when": "первая страница прошла очередь и опубликована в канал.",
                "status": "current"},
    "columns": {
        "todo": [t for t in _TASKS if t["column"] == "todo"],
        "waiting": [t for t in _TASKS if t["column"] == "waiting"],
        "done": [],
    },
    "tasks": _TASKS,
    # Текущий спринт («Ключи и первый прогон») ещё идёт — предложения открыть «Волна 4» здесь нет
    # (progress.need: пока current/closing спринт есть, boardlib.open_sprint всё равно откажет).
    "need": [
        {"title": "Ключ сервиса рассылки", "why": "без ключей очередь не публикует"},
    ],
    "past": [
        {"title": "Волна 1", "closed_at": "2026-09-19"},
        {"title": "Волна 2", "closed_at": "2026-09-20"},
        {"title": "Волна 3", "closed_at": "2026-09-23"},
    ],
    "passed": [
        {"title": "Волна 1", "done_when": "ядро: текст, публикация, клиент модели, модуль ссылок",
         "closed_at": "2026-09-19"},
        {"title": "Волна 2", "done_when": "очередь и блоки: воркеры, сторож очереди, экраны блоков",
         "closed_at": "2026-09-20"},
        {"title": "Волна 3", "done_when": "экраны и импорт: очередь, расход, каналы, пользователи",
         "closed_at": "2026-09-23"},
    ],
    "next": "Волна 4",
    "upcoming": [{"title": "Волна 4", "done_when": "состав уточнится после первого прогона"}],
    "fog": [
        "перенос остальных сайтов на очередь",
        "расписание публикаций по каналам",
        "выключение легаси-скриптов",
    ],
    "warnings": [],
}


def main(argv):
    if len(argv) != 2:
        print("использование: python board/progress_page.py <куда-записать.html>", file=sys.stderr)
        return 2
    out = Path(argv[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(SAMPLE), encoding="utf-8")
    print(f"Страница собрана: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
