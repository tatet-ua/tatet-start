#!/usr/bin/env python3
"""board — команды агента для канбан-доски проектов.

    python board.py <команда> ...

Роль всегда «агент». Роль «проверяющий» (reviewer) есть только у команды `verify`: она принимает
карточку из «Выполнена» с командой проверки и файлом дословного вывода или возвращает её на
доработку; флага `--as-reviewer` у `move` нет.
Флага для роли «владелец» нет и быть не должно — ни явного, ни через переменную окружения.

Исключение — решения владельца о пути проекта, сказанные словом в чате: `finish approve`,
`sprint open`, `sprint close` и повторный `start`. Они идут ролью «владелец в чате» (owner_chat)
и требуют `--owner-said "…"` — дословную цитату владельца из этого чата. Без цитаты — код 1 и
«ОШИБКА  said: решает владелец: нужна его цитата из чата». Прав владельца на карточках эта роль
не даёт.

Все изменения данных идут через boardlib (правила, версия карточки, запись через временный
файл). Формат ошибок и стиль запуска — как в прежнем сборщике доски-артефакта
(удалён 2026-09-24): `sys.stdout.reconfigure(encoding="utf-8")`,
код выхода 0/1/2, строки "ОШИБКА  поле: что не так" в stderr, без трассировок.

Команды: list, show, add, move, comment, handoff, inbox, summary, import-pipeline,
sync-pipeline; приёмка — verify, verify-queue, migrate-acceptance; путь проекта — start, finish,
fog, sprint, progress; страница доски — serve (запускает server.py). Подробности каждой — `python board.py <команда> --help`.

Коды выхода: 0 — успех; 1 — отказ правил, ошибка проверки, конфликт версии, нет карточки или
проекта (сообщение boardlib выводится в stderr как есть); 2 — ошибка аргументов командной строки.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import re
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import boardlib  # noqa: E402
import textcheck  # noqa: E402


def _err(field, message):
    """Строка ошибки в формате boardlib/сборщика доски: два пробела после слова ОШИБКА."""
    return f"ОШИБКА  {field}: {message}"


# ---------------------------------------------------------------- проект и id карточки


def _project_from_id(card_id):
    """Проект по id карточки: делим по ПОСЛЕДНЕМУ дефису (имя проекта может содержать дефисы)."""
    if not isinstance(card_id, str):
        raise boardlib.ValidationError(_err("id", "id карточки — строка"))
    project, sep, number = card_id.rpartition("-")
    if not sep or not project or not number:
        raise boardlib.ValidationError(
            _err("id", f"«{card_id}» не похож на id карточки: нужен вид <проект>-<номер>")
        )
    return project


def resolve_project(explicit):
    """--project, иначе BOARD_PROJECT, иначе проект по текущей папке (path в файле проекта,
    вложенные папки считаются). Ничего не подошло — понятная ошибка, а не KeyError."""
    if explicit:
        return explicit
    env = os.environ.get("BOARD_PROJECT")
    if env:
        return env
    cwd = Path.cwd().resolve()
    best_name, best_len = None, -1
    for name in boardlib.list_projects():
        try:
            data = boardlib.read_project(name)
        except boardlib.BoardError:
            continue
        raw_path = (data.get("path") or "").strip()
        if not raw_path:
            continue
        try:
            proj_path = Path(raw_path).resolve()
        except OSError:
            continue
        try:
            cwd.relative_to(proj_path)
        except ValueError:
            continue
        length = len(str(proj_path))
        if length > best_len:
            best_len, best_name = length, name
    if best_name:
        return best_name
    raise boardlib.ValidationError(_err(
        "project",
        "не удалось определить проект: укажи --project, переменную BOARD_PROJECT "
        "или запусти команду из папки проекта",
    ))


# ---------------------------------------------------------------- ссылка на сессию

# Тот же класс символов, что у идентификатора в boardlib.SESSION_LINK
# (claude://claude.ai/epitaxy/[A-Za-z0-9_-]+). Мусорное значение переменной (пробелы, кавычки,
# "../x") проверяем этим же видом ДО основного действия команды: не идентификатор — сессию просто
# не пишем, а не превращаем уже сделанное действие в отказ.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_SESSION_WARNING_BAD_ID = (
    "ЗАМЕЧАНИЕ  session: переменная сессии не похожа на идентификатор, ссылка не записана"
)


def _session_payload(prev_session, title_arg):
    """(payload, warning) для записи ссылки на сессию; ровно одно из двух — None.

    payload — {link, title} для boardlib.handoff, когда CLAUDE_CODE_HOST_SESSION_ID задана и
    похожа на идентификатор. warning — готовая строка в stderr, когда переменная задана, но не
    похожа на идентификатор (сессия в этом случае не пишется совсем). Нет переменной — оба None,
    команда сессию не трогает и не падает.

    Модель ссылку не придумывает: id сессии берём только из окружения. Название — из аргумента
    --session-title (только у handoff), иначе прежнее — если ссылка та же, иначе пусто.
    """
    sid = os.environ.get("CLAUDE_CODE_HOST_SESSION_ID")
    if not sid:
        return None, None
    if not _SESSION_ID_RE.fullmatch(sid):
        return None, _SESSION_WARNING_BAD_ID
    link = f"claude://claude.ai/epitaxy/{sid}"
    prev = prev_session or {}
    if title_arg:
        title = title_arg
    elif prev.get("link") == link:
        title = prev.get("title") or ""
    else:
        title = ""
    return {"link": link, "title": title}, None


def _finalize_session(project, card, role, session_payload, session_warning):
    """add / move / comment: после УЖЕ УСПЕШНОГО основного действия — отдельная запись session.

    session_payload/session_warning считаны ДО основного действия (см. вызовы в cmd_add/cmd_move/
    cmd_comment) — так негодное значение переменной обнаруживается раньше, чем что-либо записано.
    Если переменная не похожа на идентификатор — только предупреждение, действие не трогаем. Любая
    другая неудача самой записи session (конфликт версии, текст не прошёл проверку) — тоже только
    предупреждение: основное действие уже на диске, откатывать и подавать как ошибку команды нечего.
    """
    if session_warning:
        print(session_warning, file=sys.stderr)
        return card
    if session_payload is None:
        return card
    try:
        return boardlib.handoff(project, card["id"], role, session=session_payload, version=card["version"])
    except boardlib.BoardError as exc:
        print("ЗАМЕЧАНИЕ  session: " + "; ".join(exc.messages), file=sys.stderr)
        return card
    except Exception as exc:  # тоже не должно превращать успех в код 1
        print(f"ЗАМЕЧАНИЕ  session: {type(exc).__name__}: {exc}", file=sys.stderr)
        return card


# ---------------------------------------------------------------- печать карточек


def _card_labels(card):
    labels = []
    if card["kind"] == "stage" and card.get("stage") is not None:
        labels.append(f"этап {card['stage']}")
    labels.append(card["priority"])
    if card.get("tag"):
        labels.append(boardlib.TAGS.get(card["tag"], card["tag"]))
    if str(card.get("rework") or "").strip():
        labels.append("на доработку")
    if str(card.get("caveat") or "").strip():
        labels.append("с оговоркой")
    if card.get("acceptance") == boardlib.OWNER:
        labels.append("принимаю сам")
    return labels


def _print_cards(cards):
    for c in cards:
        label_str = " ".join(f"[{l}]" for l in _card_labels(c))
        print(f"{c['id']}  {label_str}  {c['title']}")


def _print_board(project, status):
    if status:
        cards = boardlib.list_cards(project, status=status)
        if not cards:
            print(f"-- {boardlib.status_title(boardlib.parse_status(status))} -- пусто")
            return
        _print_cards(cards)
        return
    any_cards = False
    for col in boardlib.COLUMNS:
        cards = boardlib.list_cards(project, status=col)
        if not cards:
            continue
        any_cards = True
        print(f"-- {boardlib.status_title(col)} --")
        _print_cards(cards)
    if not any_cards:
        print("Карточек нет.")


def _print_card_full(card):
    kind_label = boardlib.KINDS.get(card["kind"], card["kind"])
    stage_part = f" · этап {card['stage']}" if card.get("stage") is not None else ""
    print(
        f"{card['id']}  [{boardlib.status_title(card['status'])}]{stage_part} · {kind_label} "
        f"· приоритет {card['priority']}"
    )
    print(card["title"])
    if card["body"]:
        print()
        print(card["body"])
    print()
    print(f"приёмка: {boardlib.acceptance_title(card['acceptance'])}")
    if card["ask"]:
        print(f"ask: {card['ask']}")
    if card["accept_how"]:
        print(f"accept_how: {card['accept_how']}")
    if card["proof"]:
        print(f"proof: {card['proof']}")
    if str(card.get("rework") or "").strip():
        print()
        print("=== НА ДОРАБОТКУ ===")
        print(card["rework"])
        print("====================")
    if card["caveat"]:
        print(f"оговорка: {card['caveat']}")
    verified = card.get("verified") or {}
    if verified:
        verdict = "совпало" if verified.get("verdict") == boardlib.VERIFY_OK else "не совпало"
        print()
        print("=== ПРОВЕРКА ===")
        print(f"кто: {boardlib.role_title(verified.get('who'))} · когда: {verified.get('at', '')} · вердикт: {verdict}")
        print(f"команда: {verified.get('command', '')}")
        print("вывод:")
        for line in str(verified.get("output") or "").splitlines():
            print(f"  {line}")
        print("================")
    if card["stopped_at"]:
        print(f"остановились на: {card['stopped_at']}")
    if card["resume"]:
        print(f"готовый запрос: {card['resume']}")
    session = card.get("session") or {}
    if session.get("link") or session.get("title"):
        print(f"сессия: {session.get('title') or ''} {session.get('link') or ''}".strip())
    if card["comments"]:
        print()
        print("Комментарии:")
        for c in card["comments"]:
            print(f"  [{c['at']}] {boardlib.role_title(c['who'])}: {c['text']}")
    if card["history"]:
        print()
        print("История:")
        for h in card["history"]:
            frm = boardlib.status_title(h["from"]) if h["from"] else "—"
            to = boardlib.status_title(h["to"])
            note = f" — {h['text']}" if h.get("text") else ""
            print(f"  [{h['at']}] {boardlib.role_title(h['who'])}: {frm} → {to}{note}")
    print()
    print(f"version: {card['version']}")


# ---------------------------------------------------------------- команды


def cmd_list(args):
    if args.all:
        projects = boardlib.list_projects()
        if not projects:
            print("На доске нет проектов.")
            return 0
        for name in projects:
            print(f"== {name} ==")
            _print_board(name, args.status)
        return 0
    project = resolve_project(args.project)
    _print_board(project, args.status)
    return 0


def cmd_show(args):
    project = _project_from_id(args.id)
    card = boardlib.get_card(project, args.id)
    _print_card_full(card)
    return 0


def cmd_add(args):
    project = resolve_project(args.project)
    # Вид переменной сессии проверяем ДО основного действия: новой карточки ещё нет, поэтому
    # прежней session взять неоткуда (title_arg у add тоже нет — только у handoff).
    session_payload, session_warning = _session_payload(None, None)
    # --sprint не указан — boardlib сам кладёт карточку агента в текущий спринт (если он есть).
    sprint = boardlib.UNSET if args.sprint is None else _sprint_arg(args.sprint)
    card = boardlib.add_card(
        project, boardlib.AGENT, title=args.title, body=args.body or "",
        kind=args.kind, stage=args.stage, status=args.status, priority=args.priority,
        tag=args.tag, acceptance=args.acceptance, sprint=sprint,
    )
    card = _finalize_session(project, card, boardlib.AGENT, session_payload, session_warning)
    sprint_part = f" · спринт {card['sprint']}" if card.get("sprint") else ""
    print(f"{card['id']}  создана · {boardlib.status_title(card['status'])}{sprint_part} · {card['title']}")
    return 0


def cmd_move(args):
    project = _project_from_id(args.id)
    role = boardlib.AGENT
    card = boardlib.get_card(project, args.id)
    # Вид переменной сессии проверяем ДО основного действия (move ещё не вызван).
    session_payload, session_warning = _session_payload(card.get("session"), None)
    result = boardlib.move_card(
        project, args.id, to=args.to, role=role, version=card["version"],
        ask=args.ask, proof=args.proof, accept_how=args.accept_how, comment=args.comment,
    )
    result = _finalize_session(project, result, role, session_payload, session_warning)
    print(
        f"{args.id}  {boardlib.status_title(card['status'])} → "
        f"{boardlib.status_title(result['status'])}"
    )
    return 0


def cmd_comment(args):
    project = _project_from_id(args.id)
    card = boardlib.get_card(project, args.id)
    # Вид переменной сессии проверяем ДО основного действия (comment ещё не вызван).
    session_payload, session_warning = _session_payload(card.get("session"), None)
    result = boardlib.comment_card(project, args.id, boardlib.AGENT, args.text, version=card["version"])
    result = _finalize_session(project, result, boardlib.AGENT, session_payload, session_warning)
    print(f"{args.id}  комментарий добавлен")
    return 0


def cmd_handoff(args):
    project = _project_from_id(args.id)
    card = boardlib.get_card(project, args.id)
    # У handoff запись session — часть единственного вызова boardlib.handoff (тот же, что пишет
    # stopped_at/resume): негодная переменная — просто не передаём session дальше, stopped_at и
    # resume при этом всё равно записываются.
    session_payload, session_warning = _session_payload(card.get("session"), args.session_title)
    if session_warning:
        print(session_warning, file=sys.stderr)
    boardlib.handoff(
        project, args.id, boardlib.AGENT, stopped_at=args.stopped, resume=args.resume,
        session=session_payload, version=card["version"],
    )
    print(f"{args.id}  handoff записан")
    return 0


# ---------------------------------------------------------------- приёмка проверяющим


def _read_output_file(path):
    """Дословный вывод из файла проверяющего. Нет файла или пуст — отказ кодом 1 (ValidationError)."""
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise boardlib.ValidationError(_err("output", f"файл вывода не читается: {exc.strerror or exc}")) from None
    if not text.strip():
        raise boardlib.ValidationError(_err("output", "файл вывода пуст: проверяющий записывает вывод дословно"))
    return text


def cmd_verify(args):
    project = _project_from_id(args.id)
    if args.ok:
        verdict = boardlib.VERIFY_OK
    elif args.fail:
        verdict = boardlib.VERIFY_FAIL
    else:
        verdict = boardlib.VERIFY_NEED_CRITERIA
    output = None
    if verdict != boardlib.VERIFY_NEED_CRITERIA:
        if not args.output_file:
            raise boardlib.ValidationError(_err("output", "нужен --output-file с дословным выводом команды проверки"))
        output = _read_output_file(args.output_file)
    card = boardlib.get_card(project, args.id)
    result = boardlib.verify_card(
        project, args.id, verdict, command=args.verify_command, output=output, comment=args.comment,
        version=card["version"],
    )
    what = {
        boardlib.VERIFY_OK: "принято проверяющим",
        boardlib.VERIFY_FAIL: "на доработку",
        boardlib.VERIFY_NEED_CRITERIA: "запрошен критерий",
    }[verdict]
    print(
        f"{args.id}  {boardlib.status_title(card['status'])} → {boardlib.status_title(result['status'])} · {what}"
    )
    return 0


def cmd_verify_queue(args):
    project = None if args.all else resolve_project(args.project)
    queue = boardlib.verify_queue(project)
    if not queue:
        print("Очередь проверки пуста: карточек в «Выполнена» с приёмкой «проверяющий» нет.")
        return 0
    current = None
    for item in queue:
        if args.all and item["project"] != current:
            current = item["project"]
            print(f"== {current} ==")
        labels = []
        if item["kind"] == "stage" and item.get("stage") is not None:
            labels.append(f"этап {item['stage']}")
        labels.append(item["priority"])
        print(f"{item['id']}  {' '.join(f'[{l}]' for l in labels)}  {item['title']}")
        if item["body"]:
            print(f"  текст: {item['body']}")
        if item["accept_how"]:
            note = "" if item["criteria_ok"] else "  (без разделителя « → » — нужен --need-criteria)"
            print(f"  критерий: {item['accept_how']}{note}")
        else:
            print("  критерий: не задан — нужен --need-criteria")
    print(f"В очереди: {len(queue)}.")
    return 0


def cmd_migrate_acceptance(args):
    projects = [args.project] if args.project else boardlib.list_projects()
    total = 0
    for project in projects:
        changed = boardlib.migrate_acceptance(project, dry_run=args.dry_run)
        if changed is None:
            when = boardlib.read_project(project)["acceptance_migrated"][:10]
            print(f"{project}: миграция уже выполнена {when}, повтор ничего не меняет")
            continue
        for item in changed:
            print(f"{item['id']}  [{boardlib.status_title(item['status'])}]  {item['title']}")
        total += len(changed)
    if not total:
        print("менять нечего")
        return 0
    if args.dry_run:
        print(f"Изменится: {total} (--dry-run, файлы не записаны).")
    else:
        print(f"Приёмка переведена на проверяющего: {total}. Владелец возвращает «принимаю сам» на странице.")
    return 0


# Решения владельца о пути проекта (спринты, финиш) идут сразу после возвратов и ответов: они
# меняют, над чем агент работает дальше.
_INBOX_KIND_ORDER = {
    "rework": 0, "answer": 1,
    "sprint_closed": 2, "sprint_opened": 3, "finish_approved": 4,
    "sprint_ready": 5, "sprint_added": 5, "finish_proposed": 5, "sprint_move": 5,
    "comment": 6, "new_card": 7, "accepted": 8, "archived": 9, "move": 10,
}
_INBOX_KIND_LABEL = {
    "rework": "на доработку", "answer": "ответ", "comment": "комментарий",
    "new_card": "новая карточка", "accepted": "принята", "archived": "в архив", "move": "перенесена",
    "sprint_opened": "спринт открыт", "sprint_closed": "спринт закрыт",
    "finish_approved": "финиш утверждён", "finish_proposed": "финиш предложен",
    "sprint_ready": "спринт ждёт закрытия", "sprint_added": "новый спринт", "sprint_move": "спринт",
}


def _format_inbox_event(event):
    label = _INBOX_KIND_LABEL.get(event["kind"], event["kind"])
    if event.get("card_id") is None:
        # событие пути проекта: у спринта — его id, у финиша — слово «финиш»; цитата владельца
        # показывается дословно, в кавычках
        who = event.get("sprint_id") or ("финиш" if event["kind"].startswith("finish") else "путь")
        said = str(event.get("said") or "").strip()
        if said:
            text = f" — владелец: «{said}»"
        else:
            text = f" — {event['text']}" if event.get("text") else ""
        return f"{who}  [{label}]  {event['title']}{text}"
    text = f" — {event['text']}" if event.get("text") else ""
    return f"{event['card_id']}  [{label}]  {event['title']}{text}"


def cmd_inbox(args):
    if args.all:
        projects = boardlib.list_projects()
    else:
        projects = [resolve_project(args.project)]

    any_events = False
    for project in projects:
        events = boardlib.read_inbox(project)
        if not events:
            continue
        any_events = True
        events.sort(key=lambda e: (_INBOX_KIND_ORDER.get(e["kind"], 99), e["seq"]))
        if args.all:
            print(f"== {project} ==")
        for event in events:
            print(_format_inbox_event(event))
        if not args.peek:
            boardlib.ack_inbox(project)
    if not any_events:
        print("Входящих нет: с прошлого раза владелец ничего не менял.")
    return 0


def cmd_summary(args):
    data = boardlib.summary()
    if data["broken"]:
        names = ", ".join(f"{b['project']} ({b['error']})" for b in data["broken"])
        print(f"Повреждены файлы проектов: {names}")

    print("Ждёт владельца:")
    if not data["waiting"]:
        print("  пусто")
    for c in data["waiting"]:
        print(f"  {c['id']}  [{boardlib.status_title(c['status'])}]  {c['title']}")
    print(f"Ждёт проверки агентом: {data.get('verify_pending', 0)}")

    print("Не закончено:")
    if not data["unfinished"]:
        print("  пусто")
    for c in data["unfinished"]:
        stale = "  ДОЛЬШЕ ДВУХ ДНЕЙ" if c["stale"] else ""
        stopped = f" — {c['stopped_at']}" if c["stopped_at"] else ""
        print(f"  {c['id']}  {c['idle_days']:.2f} дн.{stale}{stopped}")

    print("Сделано вчера:")
    if not data["done_yesterday"]:
        print("  пусто")
    for c in data["done_yesterday"]:
        print(f"  {c['id']}  → {boardlib.status_title(c['reached'])}  {c['title']}")

    if data["sessions_without_card"]:
        print("Сессии без карточки:")
        for s in data["sessions_without_card"]:
            print(f"  {s}")
    return 0


# ---------------------------------------------------------------- путь проекта: старт, финиш, туман, спринты


def _sprint_arg(value):
    """Значение --sprint: id спринта (s1, s2…) или none — «без спринта». Вид id проверяет boardlib."""
    text = (value or "").strip()
    if text.lower() in ("none", "нет", "-"):
        return None
    return text


# Решения владельца (finish approve, sprint open/close) всегда идут ролью «владелец в чате».
# Цитату проверяет boardlib: нет её — ValidationError «ОШИБКА  said: решает владелец: нужна его
# цитата из чата» (boardlib.NEED_OWNER_SAID), одинаково во всех командах.


def _said_note(said):
    return f" — владелец: «{said.strip()}»" if (said or "").strip() else ""


def cmd_start(args):
    project = resolve_project(args.project)
    # Первый старт задаёт агент. Старт уже задан — менять его может только владелец: такая
    # команда идёт ролью «владелец в чате» и без --owner-said получает отказ с NEED_OWNER_SAID.
    if args.owner_said is not None or boardlib.read_project(project)["start"] is not None:
        role = boardlib.OWNER_CHAT
    else:
        role = boardlib.AGENT
    start = boardlib.set_start(project, role, args.date, args.event, said=args.owner_said)
    who = "записал решение владельца: " if role == boardlib.OWNER_CHAT else ""
    print(f"Старт: {who}{start['date']} — {start['event']}{_said_note(args.owner_said)}")
    return 0


def cmd_finish(args):
    project = resolve_project(args.project)
    if args.action == "approve":
        finish = boardlib.approve_finish(
            project, boardlib.OWNER_CHAT, said=args.owner_said, text=args.text,
        )
        print(f"Финиш утверждён: записал решение владельца: {finish['text']}{_said_note(args.owner_said)}")
        return 0
    if args.owner_said is not None:
        print(_err("owner_said", "цитата владельца пишется только в «board finish approve»"), file=sys.stderr)
        return 2
    if not (args.text or "").strip():
        print(_err("finish", "укажи --text — финиш одной фразой"), file=sys.stderr)
        return 2
    finish = boardlib.propose_finish(project, boardlib.AGENT, args.text)
    print(f"Финиш предложен: {finish['text']} · утверждает владелец словом в чате")
    return 0


def cmd_fog_set(args):
    project = resolve_project(args.project)
    lines = boardlib.set_fog(project, boardlib.AGENT, args.line or [])
    if not lines:
        print("Туман очищен.")
        return 0
    print(f"Туман: {len(lines)} из {boardlib.FOG_MAX} строк")
    for line in lines:
        print(f"  {line}")
    return 0


def _sprint_line(sprint):
    status = boardlib.SPRINT_STATUSES.get(sprint["status"], sprint["status"])
    done_when = f" — закрыт, когда: {sprint['done_when']}" if sprint.get("done_when") else ""
    return f"{sprint['id']}  [{status}]  {sprint['title']}{done_when}"


def _path_history_line(entry, titles):
    frm = titles.get(entry.get("from"), entry.get("from")) if entry.get("from") else "—"
    to = titles.get(entry.get("to"), entry.get("to"))
    said = str(entry.get("said") or "").strip()
    text = str(entry.get("text") or "").strip()
    note = f" — владелец: «{said}»" if said else (f" — {text}" if text else "")
    return f"    [{entry.get('at', '')}] {boardlib.role_title(entry.get('who'))}: {frm} → {to}{note}"


def cmd_sprint_list(args):
    project = resolve_project(args.project)
    data = boardlib.read_project(project)
    start, finish = data["start"], data["finish"]
    if start is not None:
        print(f"Старт: {start['date']} — {start['event']}")
    if finish is not None and finish.get("text"):
        state = "утверждён" if finish["approved"] else "предложен, ждёт владельца"
        print(f"Финиш: {finish['text']} [{state}]")
        if args.history:
            finish_titles = {"proposed": "предложен", "approved": "утверждён"}
            for entry in finish.get("history") or []:
                print(_path_history_line(entry, finish_titles))
    if not data["sprints"]:
        print("Спринтов нет: страница пути покажет один спринт «Без названия».")
    for sprint in data["sprints"]:
        print(_sprint_line(sprint))
        if sprint.get("proof"):
            print(f"    доказательство: {sprint['proof']}")
        if args.history:
            for entry in sprint.get("history") or []:
                print(_path_history_line(entry, boardlib.SPRINT_STATUSES))
    if data["fog"]:
        print(f"Туман: {' · '.join(data['fog'])}")
    return 0


def cmd_sprint_add(args):
    project = resolve_project(args.project)
    sprint = boardlib.add_sprint(project, boardlib.AGENT, args.title, args.done_when)
    print(f"{_sprint_line(sprint)} · открывает владелец словом в чате")
    return 0


def cmd_sprint_ready(args):
    project = resolve_project(args.project)
    sprint = boardlib.ready_sprint(project, boardlib.AGENT, args.id, proof=args.proof)
    print(f"{_sprint_line(sprint)} · закрывает владелец словом в чате")
    return 0


def cmd_sprint_assign(args):
    project = _project_from_id(args.card)
    card = boardlib.get_card(project, args.card)
    role = boardlib.OWNER_CHAT if args.owner_said is not None else boardlib.AGENT
    result = boardlib.set_card_sprint(
        project, args.card, role, _sprint_arg(args.sprint), version=card["version"], said=args.owner_said,
    )
    target = result["sprint"] or "без спринта"
    print(f"{args.card}  спринт: {card.get('sprint') or 'без спринта'} → {target}{_said_note(args.owner_said)}")
    return 0


def cmd_sprint_open(args):
    project = resolve_project(args.project)
    sprint = boardlib.open_sprint(project, boardlib.OWNER_CHAT, args.id, said=args.owner_said)
    print(f"Спринт открыт: записал решение владельца: {_sprint_line(sprint)}{_said_note(args.owner_said)}")
    return 0


def cmd_sprint_close(args):
    project = resolve_project(args.project)
    sprint = boardlib.close_sprint(project, boardlib.OWNER_CHAT, args.id, said=args.owner_said)
    print(f"Спринт закрыт: записал решение владельца: {_sprint_line(sprint)}{_said_note(args.owner_said)}")
    return 0


# ---------------------------------------------------------------- страница пути


def cmd_progress(args):
    import progress  # noqa: E402  — ленивый импорт: остальные команды от страницы не зависят
    import progress_page  # noqa: E402

    project = resolve_project(args.project)
    if args.set_url is not None:
        url = boardlib.set_progress_url(project, args.set_url)
        print(f"Адрес страницы записан: {url or 'снят'}")
    data = boardlib.read_project(project)
    layout = progress.layout(data)
    errors = [w for w in layout["warnings"] if w.startswith("ОШИБКА")]
    if errors:
        for line in errors:
            print(line, file=sys.stderr)
        print("Страница не собрана: исправь данные командами board, прежний файл не тронут.", file=sys.stderr)
        return 1
    # Замечание про карточки без спринта теперь считает сам progress.check (см. progress._loose_cards_note) —
    # здесь только печатается вместе с прочими мягкими предупреждениями layout["warnings"].
    notes = [w for w in layout["warnings"] if not w.startswith("ОШИБКА")]

    html_text = progress_page.render(layout)  # ошибка шаблона — до записи, прежний файл цел
    out = Path(args.out) if args.out else boardlib.data_dir() / "progress" / f"{project}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_bytes(out, html_text.encode("utf-8"))

    for line in notes:
        print(line)
    counter = layout["counter"]
    print(
        f"Страница собрана: {out} · спринт {counter['n']} из {counter['m']} · "
        f"нужно от тебя: {len(layout['need'])} · замечаний: {len(notes)}"
    )
    if args.publish:
        target = data.get("progress_url") or "новый артефакт"
        print(f"Опубликовать: {out} → {target}")
    return 0


# ---------------------------------------------------------------- блок «Пайплайн»

HEADER_RE = re.compile(r"(?m)^##\s*(?:Пайплайн|Pipeline)\s*$")
NEXT_HEADER_RE = re.compile(r"(?m)^##\s+")
# Марка — любой одиночный символ в скобках: так строка с нераспознанной пометкой (например
# «- [q] 4 …») распознаётся как строка этапа и получает предупреждение, а не тихо пропускается
# (пометка проверяется отдельно, по KNOWN_MARKERS, сразу после матча).
STAGE_LINE_RE = re.compile(r"^- \[(.)\]\s*(\d+)\s+(.*)$")
KNOWN_MARKERS = " x~?!"
SYNC_MARK_RE = re.compile(r"(?m)^(- \[)([ x~?!])(\]\s*)(\d+)\b")
CAVEAT_RE = re.compile(r"\(((?:відхилення|отклонение|оговорка)\s*:\s*[^()]*)\)")

PIPELINE_PROOF = "перенесено из блока „Пайплайн“ проекта: отмечено как готовое до появления доски"
PIPELINE_ACCEPT_HOW = "сверить с блоком „Пайплайн“ проекта → пометка этапа в блоке совпадает с карточкой"


def _default_pipeline_file(project_dir):
    base = Path(project_dir)
    agents = base / "AGENTS.md"
    if agents.exists():
        return agents
    return base / "CLAUDE.md"


def _find_pipeline_block(text):
    """(текст блока, начало, конец) — от заголовка «## Пайплайн»/«## Pipeline» до следующего ## ."""
    m = HEADER_RE.search(text)
    if not m:
        raise boardlib.BoardError(_err("файл", "блока «## Пайплайн» / «## Pipeline» нет"))
    start = m.end()
    rest = text[start:]
    nxt = NEXT_HEADER_RE.search(rest)
    end = start + nxt.start() if nxt else len(text)
    return text[start:end], start, end


def _atomic_write_bytes(path, data):
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f"{path.stem}.", suffix=".tmp")
    done = False
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, str(path))
        done = True
    finally:
        if not done:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)


def cmd_import_pipeline(args):
    project = args.project
    existing = project in boardlib.list_projects()
    if not existing:
        if not args.path or not args.phrase:
            raise boardlib.ValidationError(_err(
                "project",
                f"проекта «{project}» на доске нет: укажи --path и --phrase, чтобы его создать",
            ))
        boardlib.create_project(project, args.phrase, args.path)
        project_dir = args.path
    else:
        data = boardlib.read_project(project)
        project_dir = args.path or data.get("path") or ""
        if not project_dir:
            raise boardlib.ValidationError(_err("path", "у проекта на доске не записан путь; укажи --path"))

    file_path = Path(args.from_file) if args.from_file else _default_pipeline_file(project_dir)
    if not file_path.exists():
        raise boardlib.BoardError(_err("файл", f"{file_path} не найден"))
    text = file_path.read_text(encoding="utf-8-sig")
    block, block_start, _end = _find_pipeline_block(text)
    first_line_no = text[:block_start].count("\n") + 1  # строка заголовка «## Пайплайн»

    created = 0
    skipped = 0
    for offset, line in enumerate(block.splitlines()):
        line_no = first_line_no + offset
        m = STAGE_LINE_RE.match(line)
        if not m:
            continue
        marker, num_str, rest = m.groups()
        if marker not in KNOWN_MARKERS:
            print(
                f"ЗАМЕЧАНИЕ  строка {line_no}: пометка «[{marker}]» не распознана "
                f"(допустимы [ ], [x], [~], [?], [!]), строка пропущена"
            )
            continue
        stage_num = int(num_str)
        if stage_num not in boardlib.STAGES:
            print(f"ЗАМЕЧАНИЕ  этап {stage_num}: номер вне списка этапов пайплайна, строка пропущена")
            continue
        if boardlib.find_stage_card(project, stage_num) is not None:
            print(f"этап {stage_num} уже на доске")
            skipped += 1
            continue

        if ":" in rest:
            title_raw, body_raw = rest.split(":", 1)
        else:
            title_raw, body_raw = rest, ""
        title = title_raw.strip()
        body = body_raw.strip()

        body_errors = textcheck.check_fields({"body": body})
        if body_errors:
            print(f"ЗАМЕЧАНИЕ  этап {stage_num}: текст не прошёл проверку, тело заменено общей фразой")
            body = "текст этапа — в блоке «Пайплайн» проекта"

        caveat_comment = None
        if marker == "~":
            cm = CAVEAT_RE.search(body_raw)
            if cm:
                caveat_comment = f"предлагаемая оговорка: {cm.group(1)}"
            else:
                print(f"ЗАМЕЧАНИЕ  этап {stage_num}: нет текста оговорки в скобках, комментарий не добавлен")

        # sprint=None явно: карточки-этапы импорта — из блока «Пайплайн» проекта, не из текущей
        # работы сессии, поэтому в текущий спринт (умолчание add_card для роли агента) не попадают.
        if marker == " ":
            card = boardlib.add_card(
                project, boardlib.AGENT, title=title, body=body, kind="этап", stage=stage_num,
                status=boardlib.BACKLOG, sprint=None,
            )
        elif marker == "!":
            card = boardlib.add_card(
                project, boardlib.AGENT, title=title, body=body, kind="этап", stage=stage_num,
                status=boardlib.IN_PROGRESS, sprint=None,
            )
        else:  # "x", "~", "?" — все три садятся в «Выполнена» с одним и тем же доказательством;
            # в «Завершена» их доводит проверяющий (board verify), импорт сам не принимает
            card = boardlib.add_card(
                project, boardlib.AGENT, title=title, body=body, kind="этап", stage=stage_num,
                status=boardlib.DONE, proof=PIPELINE_PROOF, accept_how=PIPELINE_ACCEPT_HOW, sprint=None,
            )

        if caveat_comment is not None:
            comment_errors = textcheck.check_fields({"comment": caveat_comment})
            if comment_errors:
                print(
                    f"ЗАМЕЧАНИЕ  этап {stage_num}: предложенная оговорка не прошла проверку, "
                    "комментарий не добавлен"
                )
            else:
                card = boardlib.comment_card(
                    project, card["id"], boardlib.AGENT, caveat_comment, version=card["version"]
                )

        created += 1
        print(f"{card['id']}  этап {stage_num} · {boardlib.status_title(card['status'])} · {title}")

    print(f"Импорт завершён: создано карточек {created}, пропущено (уже на доске) {skipped}.")
    return 0


def _desired_marker(card):
    status = card["status"]
    if status == boardlib.ACCEPTED:
        return "~" if str(card.get("caveat") or "").strip() else "x"
    if status == boardlib.DONE:
        return "?"
    if status == boardlib.IN_PROGRESS and str(card.get("rework") or "").strip():
        return "!"
    return " "


def cmd_sync_pipeline(args):
    project = args.project
    data = boardlib.read_project(project)
    file_path = Path(args.file) if args.file else _default_pipeline_file(data.get("path") or "")
    if not file_path.exists():
        raise boardlib.BoardError(_err("файл", f"{file_path} не найден"))

    raw = file_path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    content = raw.decode("utf-8-sig")
    _block, block_start, block_end = _find_pipeline_block(content)
    block = content[block_start:block_end]

    changes = []
    missing = []

    def repl(m):
        prefix, marker, mid, num_str = m.group(1), m.group(2), m.group(3), m.group(4)
        num = int(num_str)
        card = boardlib.find_stage_card(project, num)
        if card is None:
            missing.append(num)
            return m.group(0)
        desired = _desired_marker(card)
        if desired != marker:
            changes.append((num, marker, desired))
        return f"{prefix}{desired}{mid}{num_str}"

    new_block = SYNC_MARK_RE.sub(repl, block)
    new_content = content[:block_start] + new_block + content[block_end:]

    for num in missing:
        print(f"ЗАМЕЧАНИЕ  этап {num}: карточки на доске нет, строка не тронута")
    for num, prev, new in changes:
        print(f"этап {num}: [{prev}] → [{new}]")

    if args.dry_run:
        if changes:
            print(f"Изменений: {len(changes)} (--dry-run, файл не записан).")
        else:
            print("Изменений нет (--dry-run, файл не записан).")
        return 0

    new_bytes = new_content.encode("utf-8")
    if bom:
        new_bytes = b"\xef\xbb\xbf" + new_bytes
    _atomic_write_bytes(file_path, new_bytes)
    if changes:
        print(f"Синхронизировано: {len(changes)} этапов изменено в {file_path}.")
    else:
        print("Изменений нет.")
    return 0


# ---------------------------------------------------------------- serve


def cmd_serve(args):
    """Запустить программу доски (страница и API) — тот же код, что `python server.py`.

    Нужна, чтобы одна обёртка `board` покрывала и команды агента, и сервер: путь к плагину меняется
    при обновлении, а обёртку переписывает установщик. Работает до остановки (Ctrl+C или сигнал);
    занятый порт — строка в журнале и код 0, как у server.py.
    """
    import server  # noqa: PLC0415 - сервер нужен только этой команде

    server.main(["--port", str(args.port)] if args.port is not None else [])
    return 0


# ---------------------------------------------------------------- argparse


def build_parser():
    parser = argparse.ArgumentParser(
        prog="board", description="Канбан-доска проектов — команды агента (роль всегда «агент»)."
    )
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="карточки по колонкам")
    p_list.add_argument("--project", help="имя проекта; без него — по текущей папке или BOARD_PROJECT")
    p_list.add_argument("--status", help="только одна колонка")
    p_list.add_argument("--all", action="store_true", help="все проекты")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="карточка целиком")
    p_show.add_argument("id", help="id карточки, <проект>-<номер>")
    p_show.set_defaults(func=cmd_show)

    p_add = sub.add_parser("add", help="создать карточку")
    p_add.add_argument("--title", required=True, help="заголовок в одну строку")
    p_add.add_argument("--body", help="текст: что сделать и зачем")
    p_add.add_argument("--project", help="без него — по текущей папке или BOARD_PROJECT")
    p_add.add_argument("--kind", default="задача", help="задача (по умолчанию) или этап")
    p_add.add_argument("--stage", type=int, help="номер этапа (только для kind=этап)")
    p_add.add_argument("--status", default="backlog", help="колонка; по умолчанию Бэклог")
    p_add.add_argument("--priority", default="B", help="A сейчас, B следом (по умолчанию), C потом")
    p_add.add_argument("--tag", help="блокирует остальное / срочно")
    p_add.add_argument(
        "--acceptance",
        help="кто принимает: owner («принимаю сам» — дизайн, письмо клиенту, оплата); по умолчанию проверяющий",
    )
    p_add.add_argument(
        "--sprint", help="спринт карточки (s1, s2…) или none; без него — текущий спринт, если он есть",
    )
    p_add.set_defaults(func=cmd_add)

    p_move = sub.add_parser("move", help="сменить статус карточки")
    p_move.add_argument("id", help="id карточки")
    p_move.add_argument("--to", required=True, help="целевая колонка")
    p_move.add_argument("--ask", help="что решить владельцу (обязательно в «На согласовании»)")
    p_move.add_argument("--proof", help="чем доказано и кем проверено (обязательно в «Выполнена»)")
    p_move.add_argument(
        "--accept-how", dest="accept_how",
        help="критерий проверки «что выполнить → что должно получиться» (обязателен в «Выполнена»)",
    )
    p_move.add_argument("--comment", help="комментарий к переносу")
    p_move.set_defaults(func=cmd_move)

    p_verify = sub.add_parser("verify", help="приёмка проверяющим: принять, вернуть или запросить критерий")
    p_verify.add_argument("id", help="id карточки в «Выполнена» с приёмкой «проверяющий»")
    verdict = p_verify.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--ok", action="store_true", help="критерий совпал → «Завершена»")
    verdict.add_argument("--fail", action="store_true", help="не совпало → «В работе», нужен --comment")
    verdict.add_argument(
        "--need-criteria", dest="need_criteria", action="store_true",
        help="критерия нет или без разделителя → «В работе» с запросом критерия",
    )
    # dest не «command»: это имя занято выбором подкоманды (sub.add_subparsers(dest="command")).
    p_verify.add_argument(
        "--command", dest="verify_command",
        help="команда или действие, как выполнялась проверка (для --ok и --fail)",
    )
    p_verify.add_argument(
        "--output-file", dest="output_file",
        help="файл с дословным выводом команды (для --ok и --fail); пустой файл — отказ",
    )
    p_verify.add_argument("--comment", help="что не совпало (обязателен у --fail; у --need-criteria — пояснение)")
    p_verify.set_defaults(func=cmd_verify)

    p_vq = sub.add_parser("verify-queue", help="очередь проверяющего: «Выполнена» с приёмкой «проверяющий», без proof")
    p_vq.add_argument("--project", help="имя проекта; без него — по текущей папке или BOARD_PROJECT")
    p_vq.add_argument("--all", action="store_true", help="все проекты доски")
    p_vq.set_defaults(func=cmd_verify_queue)

    p_mig = sub.add_parser("migrate-acceptance", help="одноразово: приёмка владелец → проверяющий у непринятых карточек")
    p_mig.add_argument("--project", help="имя проекта; без него — все проекты доски")
    p_mig.add_argument("--dry-run", dest="dry_run", action="store_true", help="показать список, ничего не менять")
    p_mig.set_defaults(func=cmd_migrate_acceptance)

    p_comment = sub.add_parser("comment", help="комментарий в ленту карточки")
    p_comment.add_argument("id", help="id карточки")
    p_comment.add_argument("--text", required=True, help="текст комментария")
    p_comment.set_defaults(func=cmd_comment)

    p_handoff = sub.add_parser("handoff", help="на чём остановились и готовый запрос")
    p_handoff.add_argument("id", help="id карточки")
    p_handoff.add_argument("--stopped", required=True, help="на чём остановились")
    p_handoff.add_argument("--resume", required=True, help="готовый запрос для новой сессии")
    p_handoff.add_argument("--session-title", dest="session_title", help="название сессии")
    p_handoff.set_defaults(func=cmd_handoff)

    p_inbox = sub.add_parser("inbox", help="что владелец изменил с прошлого чтения")
    p_inbox.add_argument("--project", help="имя проекта; без него — по текущей папке или BOARD_PROJECT")
    p_inbox.add_argument("--all", action="store_true", help="все проекты")
    p_inbox.add_argument("--peek", action="store_true", help="не сдвигать курсор")
    p_inbox.set_defaults(func=cmd_inbox)

    p_summary = sub.add_parser("summary", help="сводка «все проекты»")
    p_summary.set_defaults(func=cmd_summary)

    p_import = sub.add_parser("import-pipeline", help="семь карточек-этапов из блока «Пайплайн»")
    p_import.add_argument("--project", required=True, help="имя проекта")
    p_import.add_argument("--from", dest="from_file", help="файл проекта; по умолчанию AGENTS.md/CLAUDE.md")
    p_import.add_argument("--path", help="папка проекта (нужна, если проекта на доске ещё нет)")
    p_import.add_argument("--phrase", help="фраза о проекте (нужна, если проекта на доске ещё нет)")
    p_import.set_defaults(func=cmd_import_pipeline)

    p_sync = sub.add_parser("sync-pipeline", help="пометки блока «Пайплайн» из доски")
    p_sync.add_argument("--project", required=True, help="имя проекта")
    p_sync.add_argument("--file", help="файл проекта; по умолчанию AGENTS.md/CLAUDE.md")
    p_sync.add_argument("--dry-run", action="store_true", help="показать изменения, не писать файл")
    p_sync.set_defaults(func=cmd_sync_pipeline)

    # ---- путь проекта. --owner-said — дословная цитата владельца из этого чата: только после его
    # реплики, никогда не сочиняется. Без неё решения владельца получают отказ (код 1).
    owner_said_help = "дословная цитата владельца из этого чата (решение владельца)"

    p_start = sub.add_parser("start", help="старт проекта: дата и событие")
    p_start.add_argument("--project", help="без него — по текущей папке или BOARD_PROJECT")
    p_start.add_argument("--date", required=True, help="ГГГГ-ММ-ДД")
    p_start.add_argument("--event", required=True, help="событие старта, например «спека утверждена»")
    p_start.add_argument(
        "--owner-said", dest="owner_said",
        help=owner_said_help + "; обязательна, если старт уже задан",
    )
    p_start.set_defaults(func=cmd_start)

    p_finish = sub.add_parser("finish", help="финиш: предложить (--text) или утвердить (approve)")
    p_finish.add_argument(
        "action", nargs="?", choices=["approve"],
        help="approve — записать решение владельца утвердить финиш (нужен --owner-said)",
    )
    p_finish.add_argument("--project", help="без него — по текущей папке или BOARD_PROJECT")
    p_finish.add_argument("--text", help="финиш одной фразой; у approve — новая формулировка словом владельца")
    p_finish.add_argument("--owner-said", dest="owner_said", help=owner_said_help)
    p_finish.set_defaults(func=cmd_finish)

    p_fog = sub.add_parser("fog", help="что видно в тумане за последним спринтом")
    fog_sub = p_fog.add_subparsers(dest="fog_command", required=True)
    p_fog_set = fog_sub.add_parser("set", help="записать туман целиком (до пяти строк; без --line — очистить)")
    p_fog_set.add_argument("--project", help="без него — по текущей папке или BOARD_PROJECT")
    p_fog_set.add_argument("--line", action="append", help="строка тумана; повторить для каждой")
    p_fog_set.set_defaults(func=cmd_fog_set)

    p_sprint = sub.add_parser("sprint", help="спринты проекта")
    sprint_sub = p_sprint.add_subparsers(dest="sprint_command", required=True)

    p_s_list = sprint_sub.add_parser("list", help="старт, финиш, спринты, туман")
    p_s_list.add_argument("--project", help="без него — по текущей папке или BOARD_PROJECT")
    p_s_list.add_argument("--history", action="store_true", help="с историей переходов и цитатами владельца")
    p_s_list.set_defaults(func=cmd_sprint_list)

    p_s_add = sprint_sub.add_parser("add", help="предложить следующий спринт")
    p_s_add.add_argument("--project", help="без него — по текущей папке или BOARD_PROJECT")
    p_s_add.add_argument("--title", required=True, help="название спринта")
    p_s_add.add_argument("--done-when", dest="done_when", required=True, help="закрыт, когда …")
    p_s_add.set_defaults(func=cmd_sprint_add)

    p_s_ready = sprint_sub.add_parser("ready", help="текущий спринт готов — ждёт закрытия владельцем")
    p_s_ready.add_argument("id", help="id спринта, s1, s2…")
    p_s_ready.add_argument("--project", help="без него — по текущей папке или BOARD_PROJECT")
    p_s_ready.add_argument("--proof", required=True, help="чем доказано, что условие спринта выполнено")
    p_s_ready.set_defaults(func=cmd_sprint_ready)

    p_s_assign = sprint_sub.add_parser("assign", help="привязать карточку к спринту")
    p_s_assign.add_argument("card", help="id карточки")
    p_s_assign.add_argument("--sprint", required=True, help="id спринта или none — отвязать")
    p_s_assign.add_argument(
        "--owner-said", dest="owner_said",
        help=owner_said_help + "; нужна только у карточки в «Выполнена»/«Завершена»/архиве",
    )
    p_s_assign.set_defaults(func=cmd_sprint_assign)

    p_s_open = sprint_sub.add_parser("open", help="записать решение владельца открыть спринт")
    p_s_open.add_argument("id", help="id спринта")
    p_s_open.add_argument("--project", help="без него — по текущей папке или BOARD_PROJECT")
    p_s_open.add_argument("--owner-said", dest="owner_said", help=owner_said_help)
    p_s_open.set_defaults(func=cmd_sprint_open)

    p_s_close = sprint_sub.add_parser("close", help="записать решение владельца закрыть спринт")
    p_s_close.add_argument("id", help="id спринта")
    p_s_close.add_argument("--project", help="без него — по текущей папке или BOARD_PROJECT")
    p_s_close.add_argument("--owner-said", dest="owner_said", help=owner_said_help)
    p_s_close.set_defaults(func=cmd_sprint_close)

    p_progress = sub.add_parser("progress", help="собрать страницу пути boards/progress/<проект>.html")
    p_progress.add_argument("--project", help="без него — по текущей папке или BOARD_PROJECT")
    p_progress.add_argument("--out", help="куда записать страницу; по умолчанию boards/progress/<проект>.html")
    p_progress.add_argument("--set-url", dest="set_url", help="адрес опубликованной страницы (http/https)")
    p_progress.add_argument("--publish", action="store_true", help="напечатать, что и куда опубликовать")
    p_progress.set_defaults(func=cmd_progress)

    p_serve = sub.add_parser(
        "serve", help="запустить страницу доски на 127.0.0.1 (то же, что python server.py)",
        description="Страница доски на http://127.0.0.1:<порт>; работает до Ctrl+C. Занятый порт — "
        "строка в журнале board.log и код 0; нет каталога данных — сообщение и код 1.",
    )
    p_serve.add_argument(
        "--port", type=int, default=None, help="порт; без него — BOARD_PORT, иначе 8790",
    )
    p_serve.set_defaults(func=cmd_serve)

    return parser


def main(argv=None):
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8")
    with contextlib.suppress(Exception):
        sys.stderr.reconfigure(encoding="utf-8")

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2

    try:
        return args.func(args) or 0
    except boardlib.BoardError as exc:
        for message in exc.messages:
            print(message, file=sys.stderr)
        return 1
    except Exception as exc:  # трассировку пользователю не показываем
        print(_err("board", f"{type(exc).__name__}: {exc}"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
