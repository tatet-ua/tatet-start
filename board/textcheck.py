"""Проверка текста, который в карточку доски пишет агент: запрещённые шаблоны (хеши коммитов,
uuid, идентификаторы ресурсов, пути, имена файлов) и мягкие пределы длины полей карточки.

Регулярные выражения FORBIDDEN и DOMAIN_HEAD перенесены без изменений из прежнего сборщика
доски-артефакта (удалён 2026-09-24; тесты-образцы перенесены оттуда же).
Библиотека без побочных эффектов при импорте: ничего не печатает, ничего не читает и не пишет
на диск.
"""
from __future__ import annotations

import re

# Запрещённые в тексте агента шаблоны: (метка, регулярное выражение). Порядок сохранён как в
# оригинале — при первом совпадении в check_text дальнейшие шаблоны для этого текста не проверяются.
FORBIDDEN = [
    ("хеш коммита", re.compile(r"(?<![0-9A-Za-z])(?=[0-9a-f]*[a-f])(?=[0-9a-f]*[0-9])[0-9a-f]{7,40}(?![0-9A-Za-z])")),
    ("uuid", re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")),
    ("идентификатор ресурса", re.compile(r"(?<![0-9A-Za-z])(?=[a-z0-9]*[a-z])(?=[a-z0-9]*[0-9])[a-z0-9]{20,}(?![0-9A-Za-z])")),
    # Отличие от оригинала: (?<![A-Za-z]) перед буквой диска, иначе «https://…» ловится как диск «s:/»,
    # а поле accept_how обязано называть адрес страницы.
    ("путь к файлу", re.compile(r"(?:(?<![A-Za-z])[A-Za-z]:[\\/]|~/|\.{1,2}/)[^\s,;)]+")),
    ("путь к файлу", re.compile(r"[\w.-]+/[\w./-]*\.(?:md|ts|tsx|js|mjs|vue|json|py|html|css|prisma|yml|yaml|toml|env|sql)\b")),
    ("имя файла", re.compile(r"(?<![\w/.-])[\w-]+\.(?:md|ts|tsx|mjs|vue|json|py|prisma|yml|yaml|toml|sql)\b")),
]

# Метки FORBIDDEN, которые относятся к путям и именам файлов — их пропускает allow_paths=True
# (нужно полю resume: готовый запрос обязан называть папку проекта и файлы).
PATH_LABELS = {"путь к файлу", "имя файла"}

# «сайт.зона/путь/файл.js» — адрес в сети, а не путь в проекте; перенесено как есть.
DOMAIN_HEAD = re.compile(r"(?:[\w-]+\.)+[A-Za-z]{2,}/")

# Мягкие пределы длины полей карточки доски (спека доски проекта, таблица «Карточка»).
LIMITS: dict[str, int] = {
    "title": 120,
    "body": 1200,
    "ask": 400,
    "accept_how": 600,
    "proof": 400,
    "stopped_at": 320,
    "resume": 1500,
    "comment": 600,
    "caveat": 320,
    # Путь проекта (спека доски проекта 2026-09-24): спринты, финиш, старт, туман, цитата владельца.
    "sprint_title": 80,
    "done_when": 200,
    "sprint_proof": 400,
    "fog": 120,
    "finish": 120,
    "start_event": 80,
    "owner_said": 300,
    # Приёмка проверяющим (спека 2026-09-29): команда проверки как выполнялась.
    "verify_command": 600,
}


# Вывод проверяющего (поле verified.output, спека 2026-09-29, пункт 8): пути, хеши и идентификаторы
# разрешены — дословный вывод curl их содержит по определению; запрещены только секреты по образцам.
# Найден секрет — отказ целиком, вывод не записывается. Предел длины — обрезка с пометкой.
OUTPUT_LIMIT = 4000
OUTPUT_TRUNCATED_MARK = "(обрезано)"
SECRETS = [
    ("заголовок Authorization", re.compile(r"authorization:", re.I)),
    ("токен Bearer", re.compile(r"\bbearer ", re.I)),
    ("параметр token=", re.compile(r"token=", re.I)),
    ("параметр key=", re.compile(r"key=", re.I)),
    ("слово password", re.compile(r"password", re.I)),
    ("ключ вида sk-…", re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}")),
]


def check_output(value: str, field: str = "output") -> list[str]:
    """Проверка дословного вывода команды проверяющего: только секреты по образцам SECRETS.

    Пути, имена файлов, хеши, uuid и идентификаторы здесь разрешены. Возвращает список ошибок
    в формате "ОШИБКА  <field>: …"; пустой — вывод годен для записи.
    """
    s = "" if value is None else str(value)
    for label, rx in SECRETS:
        m = rx.search(s)
        if m:
            return [f"ОШИБКА  {field}: в выводе похоже на секрет ({label}: «{m.group(0)}»); вывод не записан"]
    return []


def trim_output(value: str, limit: int = OUTPUT_LIMIT) -> tuple[str, bool]:
    """Обрезка вывода по пределу с пометкой «(обрезано)». Возвращает (текст, обрезан ли)."""
    s = "" if value is None else str(value)
    if len(s) <= limit:
        return s, False
    return s[:limit].rstrip() + "\n" + OUTPUT_TRUNCATED_MARK, True


def check_text(value: str, field: str, limit: int | None = None, allow_paths: bool = False) -> list[str]:
    """Проверяет один текст. Возвращает список сообщений об ошибках (пустой — текст годен).

    Формат сообщения как в оригинальном сборщике доски: "ОШИБКА  <field>: <что не так>".
    allow_paths=True снимает запрет на пути и имена файлов (метки из PATH_LABELS), хеши и uuid
    остаются под запретом.
    """
    s = "" if value is None else str(value)
    errors: list[str] = []

    for label, rx in FORBIDDEN:
        if allow_paths and label in PATH_LABELS:
            continue
        m = next((x for x in rx.finditer(s) if not DOMAIN_HEAD.match(x.group(0))), None)
        if m:
            errors.append(f"ОШИБКА  {field}: {label} на доску не идёт: «{m.group(0)}»")
            break

    if limit is not None and len(s) > limit:
        errors.append(f"ОШИБКА  {field}: {len(s)} знаков, предел {limit}")

    return errors


def check_fields(fields: dict[str, str]) -> list[str]:
    """Прогоняет check_text по каждому непустому полю карточки с пределом из LIMITS.

    Поле resume проверяется с allow_paths=True (см. check_text).
    """
    errors: list[str] = []
    for field, value in fields.items():
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        errors.extend(
            check_text(
                value,
                field,
                limit=LIMITS.get(field),
                allow_paths=(field == "resume"),
            )
        )
    return errors
