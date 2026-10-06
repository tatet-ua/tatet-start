"""server.py — локальная программа канбан-доски: страница, состояние, приём изменений.

Слушает ТОЛЬКО 127.0.0.1 (адрес не настраивается ни аргументом, ни переменной окружения).
Все изменения данных идут через boardlib, роль ВСЕГДА «владелец» (страница — это владелец):
роль из тела запроса никогда не читается, даже если она там передана.

Запуск
------
    python server.py [--port 8790]      или      board serve [--port 8790]

Порт: --port → переменная BOARD_PORT → 8790. Каталог данных — как у boardlib (BOARD_DIR →
$TATET_WIKI/boards → settings.json Claude Code → раскладка project/llm-wiki/boards; не найден —
сообщение в stderr и код 1). Каталог страницы — page/ рядом с этим файлом.

API для страницы
-----------------
См. отчёт задачи 1.8 (раздел «Описание API для автора страницы»): каждый адрес, тело запроса,
тело ответа, коды ошибок, пример.

Для тестов
----------
make_server(port=None, boards_dir=None, page_dir=None) → объект ThreadingHTTPServer, ещё не
запущен (не вызывает serve_forever); порт 0 — ОС выбирает свободный, реальный порт после bind —
server.server_address[1]. main() — обычный запуск (CLI, сигналы, второй экземпляр).

Второй экземпляр (порт занят) пишет строку в журнал и завершается кодом 0, без трассировки.
Программа рассчитана на запуск через pythonw.exe без консоли: sys.stdout/sys.stderr могут быть
None — print в модуле один (нет каталога данных, только при живом stderr), log_message переопределён, необработанные исключения идут
в журнал (board.log в каталоге данных).
"""
from __future__ import annotations

import argparse
import http.server
import json
import logging
import os
import re
import signal
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:  # чтобы `import boardlib` работал независимо от того, как запущен файл
    sys.path.insert(0, str(_HERE))

import boardlib  # noqa: E402

# ---------------------------------------------------------------- константы

DEFAULT_PORT = 8790
MAX_BODY_BYTES = 256 * 1024  # 256 КБ
LOG_MAX_BYTES = 1_000_000  # 1 МБ — после этого board.log -> board.log.1

STATIC_FILES = {
    "/": "index.html",
    "/board.css": "board.css",
    "/board.js": "board.js",
}
CONTENT_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "board.css": "text/css; charset=utf-8",
    "board.js": "application/javascript; charset=utf-8",
}
MISSING_PAGE_BODY = (
    "<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
    "<title>Доска</title></head><body><p>страница доски ещё не собрана</p></body></html>"
).encode("utf-8")

# Тело настоящего 500 (не ValidationError/ForbiddenError/NotFoundError/ConflictError/LockTimeoutError
# boardlib — у тех готовые русские фразы без путей, они идут в ответ как есть). Причина текста-заглушки:
# исходное исключение (в т.ч. голый BoardError типа «файл проекта не читается») может нести абсолютный
# путь или текст ОС — это не для браузера. Подробности с трассировкой уходят только в board.log.
INTERNAL_ERROR_MESSAGE = "внутренняя ошибка программы доски, подробности в журнале"

CARD_ACTIONS = ("move", "comment", "edit", "reorder", "delete", "can-move")
CARD_ACTION_RE = re.compile(r"^/api/cards/([^/]+)/(" + "|".join(CARD_ACTIONS) + r")$")
CAN_MOVE_GET_RE = re.compile(r"^/api/cards/([^/]+)/can-move$")

_ERROR_STATUS = (
    (boardlib.ValidationError, 400),
    (boardlib.ForbiddenError, 403),
    (boardlib.NotFoundError, 404),
    (boardlib.ConflictError, 409),
    (boardlib.LockTimeoutError, 503),
)


def _status_for_board_error(exc):
    for klass, status in _ERROR_STATUS:
        if isinstance(exc, klass):
            return status
    return 500  # прочий BoardError — не должен случаться, но не должен и падать без ответа


# ---------------------------------------------------------------- защита: функции, testable напрямую


def is_loopback_address(host):
    """Адрес клиента — локальная машина. Запасной рубеж к тому, что сервер и так слушает 127.0.0.1."""
    return host in ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def is_allowed_host(host_header, port):
    """Заголовок Host обязан называть именно этот сервер и порт — защита от DNS rebinding."""
    if not host_header:
        return False
    value = host_header.strip().lower()
    return value in (f"127.0.0.1:{port}", f"localhost:{port}")


def is_allowed_origin(origin_header, port):
    """Origin, если он есть, обязан быть этим сервером. Пустой Origin (не браузер/не CORS) — годится."""
    if not origin_header:
        return True
    value = origin_header.strip().lower()
    return value in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")


def split_card_id(card_id):
    """<проект>-<номер> → имя проекта, деление по ПОСЛЕДНЕМУ дефису (проект может содержать дефисы).

    Если дефиса нет вовсе, возвращает пустую строку — она не пройдёт PROJECT_NAME в boardlib и
    вызов упадёт естественным ValidationError, без отдельной ветки ошибок здесь.
    """
    project, _sep, _number = card_id.rpartition("-")
    return project


def required_fields_for_move(card, frm, to):
    """Поля, БЕЗ которых переход в `to` boardlib откажет (ValidationError) — без записи.

    Дублирует часть правил boardlib._required_for_move (та функция приватная и рассчитана на
    все роли; здесь роль всегда «владелец», а обязательные поля берём прямо из спеки доски). См.
    отчёт, раздел «нужно в boardlib»: стоит вынести это в публичную функцию самой boardlib,
    чтобы board.py и server.py не поддерживали два одинаковых списка правил.
    """
    required = []
    if to == boardlib.REVIEW and not (card["ask"] or "").strip():
        required.append("ask")
    if to == boardlib.DONE:
        was_rework = bool((card["rework"] or "").strip())
        if was_rework or not (card["proof"] or "").strip():
            required.append("proof")
        # Критерий «что выполнить → что должно получиться» обязателен у любой приёмки
        # (спека 2026-09-29, пункт 1); записанный, но без разделителя — тоже спрашивается заново.
        if boardlib.accept_how_error(card["accept_how"]):
            required.append("accept_how")
    if frm in (boardlib.DONE, boardlib.ACCEPTED) and to == boardlib.IN_PROGRESS:
        required.append("comment")  # возврат из «Завершена» — тоже только с комментарием (пункт 4)
    return required


def optional_fields_for_move(card, frm, to):
    """Поля, которые переходу не нужны, но странице стоит их предложить владельцу.

    Сейчас только «caveat» — оговорка при приёмке: boardlib примет переход и без неё (это не
    ValidationError), но спека доски просит предлагать её именно при приёмке.
    """
    optional = []
    if to == boardlib.ACCEPTED:
        optional.append("caveat")
    return optional


# ---------------------------------------------------------------- журнал


def _make_logger(boards_dir):
    """Отдельный логгер на каждый вызов (не через logging.getLogger — тесты поднимают много
    серверов на разные временные каталоги в одном процессе, общий логгер по имени их бы перепутал).
    """
    boards_dir = Path(boards_dir)
    boards_dir.mkdir(parents=True, exist_ok=True)
    log_path = boards_dir / "board.log"
    logger = logging.Logger(f"board-server@{log_path}", level=logging.INFO)
    handler = RotatingFileHandler(str(log_path), maxBytes=LOG_MAX_BYTES, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def _close_logger(logger):
    for handler in list(logger.handlers):
        try:
            handler.close()
        finally:
            logger.removeHandler(handler)


# ---------------------------------------------------------------- HTTP-сервер


class BoardHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    # Без SO_REUSEADDR: на Windows этот флаг разрешает привязаться к порту, который уже слушает
    # другой процесс (в отличие от POSIX, где он лишь снимает TIME_WAIT) — второй экземпляр обязан
    # получить настоящий отказ бинда, а не тихо встать рядом с первым.
    allow_reuse_address = False

    def handle_error(self, request, client_address):
        """Socketserver зовёт это на необработанное исключение вокруг запроса (например, обрыв
        соединения при записи ответа). По умолчанию печатает traceback в sys.stderr — под
        pythonw.exe его нет, печать в None уронит поток. Пишем в журнал вместо этого."""
        logger = getattr(self, "board_logger", None)
        if logger is not None:
            logger.error("необработанная ошибка соединения от %s", client_address, exc_info=True)

    def server_close(self):
        super().server_close()
        logger = getattr(self, "board_logger", None)
        if logger is not None:
            _close_logger(logger)


class _BodyError(Exception):
    """Тело POST-запроса нельзя разобрать: неверный размер, кодировка или JSON."""

    def __init__(self, status, message, protection=False):
        super().__init__(message)
        self.status = status
        self.message = message
        self.protection = protection


class BoardRequestHandler(http.server.BaseHTTPRequestHandler):
    # Ни версии Python, ни номера сборки в заголовке Server — незачем подсказывать это чужому скрипту.
    server_version = "TatetBoard"
    sys_version = ""
    # Без таймаута соединение с недосланным телом (Content-Length больше факта) держит поток
    # обработчика бесконечно: StreamRequestHandler.setup() зовёт socket.settimeout(self.timeout),
    # если он не None — после стольки секунд бездействия чтение бросит TimeoutError, а do_POST
    # это ловит как любую другую ошибку (см. `except Exception` в do_GET/do_POST/do_OPTIONS).
    timeout = 30

    # -------------------------------------------------- журнал вместо печати в консоль

    def log_message(self, format, *args):  # noqa: A002 - имя метода задано базовым классом
        """Обычные запросы (в первую очередь пятисекундный опрос) НЕ пишем в журнал —
        их не менее 17 тысяч в сутки. Нужные события логируют явные вызовы ниже."""
        return

    # -------------------------------------------------- общие мелочи

    @property
    def _boards_dir(self):
        return self.server.board_dir

    @property
    def _port(self):
        return self.server.board_port

    @property
    def _logger(self):
        return self.server.board_logger

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except OSError:
            pass  # клиент уже отключился — отвечать некому

    def _send_error(self, status, messages):
        self._send_json(status, {"error": list(messages)})

    def _log_protection(self, status, message):
        self._logger.warning(
            "отказ защиты: %s %s -> %s (клиент %s): %s",
            self.command, self.path, status, self.client_address[0], message,
        )

    def _log_5xx(self, status, exc):
        self._logger.error(
            "ответ %s на %s %s: %s", status, self.command, self.path, exc, exc_info=True,
        )

    def _reject(self, status, message, protection=False):
        if protection:
            self._log_protection(status, message)
        self._send_error(status, [message])

    def _call(self, fn):
        """Выполнить обращение к boardlib; при BoardError отправить ответ и вернуть (None, False).

        LockTimeoutError (503) и голый BoardError (500, не должен случаться, но не должен и падать
        без ответа) попадают в журнал с трассировкой. У 503 готовая безопасная русская фраза — идёт
        в ответ как есть. У 500 текст исключения (в т.ч. когда это ошибка ОС из boardlib._save,
        несущая абсолютный путь) в тело НЕ идёт — только константа INTERNAL_ERROR_MESSAGE.
        400/403/404/409 — готовые фразы boardlib без путей, отдаются как есть.
        """
        try:
            return fn(), True
        except boardlib.BoardError as exc:
            status = _status_for_board_error(exc)
            if status >= 500:
                self._log_5xx(status, exc)
            messages = [INTERNAL_ERROR_MESSAGE] if status == 500 else exc.messages
            self._send_error(status, messages)
            return None, False

    def _send_card_result(self, card):
        self._send_json(200, {"card": card, "token": boardlib.state_token(self._boards_dir)})

    # -------------------------------------------------- защита

    def _guard_common(self):
        client_host = self.client_address[0]
        if not is_loopback_address(client_host):
            self._reject(403, f"адрес клиента «{client_host}» не 127.0.0.1", protection=True)
            return False
        host_header = self.headers.get("Host", "")
        if not is_allowed_host(host_header, self._port):
            self._reject(403, f"заголовок Host «{host_header}» не годится", protection=True)
            return False
        return True

    def _guard_post(self):
        content_type = self.headers.get("Content-Type", "")
        main_type = content_type.split(";", 1)[0].strip().lower()
        if main_type != "application/json":
            self._reject(
                415, f"Content-Type «{content_type}» не годится: нужен application/json", protection=True,
            )
            return False
        origin = self.headers.get("Origin")
        if not is_allowed_origin(origin, self._port):
            self._reject(403, f"Origin «{origin}» не годится", protection=True)
            return False
        return True

    def _read_json_body(self):
        length_header = self.headers.get("Content-Length")
        try:
            length = int(length_header) if length_header is not None else 0
        except ValueError:
            raise _BodyError(400, f"заголовок Content-Length «{length_header}» не число")
        if length < 0:
            raise _BodyError(400, "заголовок Content-Length отрицательный")
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            raise _BodyError(413, f"тело запроса больше {MAX_BODY_BYTES} байт", protection=True)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _BodyError(400, f"тело запроса не UTF-8: {exc}")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise _BodyError(400, f"тело запроса не JSON: {exc}")
        if not isinstance(data, dict):
            raise _BodyError(400, "тело запроса должно быть JSON-объектом")
        return data

    def _require_version(self, payload):
        version = payload.get("version")
        if version is None:
            self._send_error(400, ["ОШИБКА  version: обязательна для изменения карточки"])
            return None, False
        return version, True

    # -------------------------------------------------- статика

    def _serve_static(self, path):
        filename = STATIC_FILES.get(path)
        if filename is None:
            return self._send_missing_page()
        file_path = self.server.board_page_dir / filename
        try:
            data = file_path.read_bytes()
        except OSError:
            return self._send_missing_page()
        try:
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPES.get(filename, "application/octet-stream"))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass

    def _send_missing_page(self):
        try:
            self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(MISSING_PAGE_BODY)))
            self.end_headers()
            self.wfile.write(MISSING_PAGE_BODY)
        except OSError:
            pass

    # -------------------------------------------------- маршруты чтения

    def _handle_health(self):
        self._send_json(200, {"ok": True})

    def _handle_state(self, query):
        boards_dir = self._boards_dir
        token = boardlib.state_token(boards_dir)
        since = (query.get("since") or [None])[0]
        if since is not None and since == token:
            self._send_json(200, {"token": token, "unchanged": True})
            return
        projects = []
        for name in boardlib.list_projects(boards_dir):
            try:
                info = boardlib.read_project(name, boards_dir)
                cards = boardlib.list_cards(name, boards_dir=boards_dir)
            except boardlib.BoardError:
                continue  # файл проекта не читается — summary() отдельно покажет его в broken
            projects.append({
                "project": info["project"], "phrase": info["phrase"], "path": info["path"],
                "cards": cards,
            })
        summary = boardlib.summary(boards_dir=boards_dir)
        statuses = [
            {"key": key, "title": boardlib.STATUSES[key]}
            for key in (*boardlib.COLUMNS, *boardlib.ARCHIVE)
        ]
        meta = {
            "roles": dict(boardlib.ROLES),
            "priorities": dict(boardlib.PRIORITIES),
            "tags": dict(boardlib.TAGS),
            "acceptance": dict(boardlib.ACCEPTANCE),
            "kinds": dict(boardlib.KINDS),
        }
        self._send_json(200, {
            "token": token, "projects": projects, "summary": summary,
            "statuses": statuses, "meta": meta,
        })

    def _handle_can_move(self, card_id, to_value):
        project = split_card_id(card_id)
        card, ok = self._call(lambda: boardlib.get_card(project, card_id, boards_dir=self._boards_dir))
        if not ok:
            return
        try:
            to = boardlib.parse_status(to_value)
        except boardlib.ValidationError as exc:
            self._send_error(400, exc.messages)
            return
        allowed, reason = boardlib.can_move(card, to, boardlib.OWNER)
        if allowed:
            required = required_fields_for_move(card, card["status"], to)
            optional = optional_fields_for_move(card, card["status"], to)
        else:
            required, optional = [], []
        self._send_json(200, {"allowed": allowed, "reason": reason, "required": required, "optional": optional})

    # -------------------------------------------------- маршруты записи (POST)

    def _create_card(self, payload):
        project = payload.get("project")
        card, ok = self._call(lambda: boardlib.add_card(
            project, boardlib.OWNER, payload.get("title"),
            body=payload.get("body") or "",
            kind=payload.get("kind", "task"),
            stage=payload.get("stage"),
            status=payload.get("status", boardlib.BACKLOG),
            priority=payload.get("priority", "B"),
            tag=payload.get("tag"),
            acceptance=payload.get("acceptance"),
            ask=payload.get("ask") or "",
            proof=payload.get("proof") or "",
            accept_how=payload.get("accept_how") or "",
            boards_dir=self._boards_dir,
        ))
        if not ok:
            return
        self._send_card_result(card)

    def _move_card(self, card_id, payload):
        version, ok = self._require_version(payload)
        if not ok:
            return
        project = split_card_id(card_id)
        card, ok = self._call(lambda: boardlib.move_card(
            project, card_id, payload.get("to"), boardlib.OWNER,
            version=version,
            ask=payload.get("ask"), proof=payload.get("proof"), accept_how=payload.get("accept_how"),
            comment=payload.get("comment"), caveat=payload.get("caveat"),
            boards_dir=self._boards_dir,
        ))
        if not ok:
            return
        self._send_card_result(card)

    def _comment_card(self, card_id, payload):
        version, ok = self._require_version(payload)
        if not ok:
            return
        project = split_card_id(card_id)
        card, ok = self._call(lambda: boardlib.comment_card(
            project, card_id, boardlib.OWNER, payload.get("text"),
            version=version, boards_dir=self._boards_dir,
        ))
        if not ok:
            return
        self._send_card_result(card)

    def _edit_card(self, card_id, payload):
        version, ok = self._require_version(payload)
        if not ok:
            return
        project = split_card_id(card_id)
        tag = payload["tag"] if "tag" in payload else boardlib.UNSET
        card, ok = self._call(lambda: boardlib.edit_card(
            project, card_id, boardlib.OWNER,
            title=payload.get("title"), body=payload.get("body"), priority=payload.get("priority"),
            tag=tag, acceptance=payload.get("acceptance"),
            version=version, boards_dir=self._boards_dir,
        ))
        if not ok:
            return
        self._send_card_result(card)

    def _reorder_card(self, card_id, payload):
        version, ok = self._require_version(payload)
        if not ok:
            return
        project = split_card_id(card_id)
        card, ok = self._call(lambda: boardlib.reorder_card(
            project, card_id, boardlib.OWNER,
            order=payload.get("order"), priority=payload.get("priority"),
            version=version, boards_dir=self._boards_dir,
        ))
        if not ok:
            return
        self._send_card_result(card)

    def _delete_card(self, card_id, payload):
        version, ok = self._require_version(payload)
        if not ok:
            return
        project = split_card_id(card_id)
        card, ok = self._call(lambda: boardlib.delete_card(
            project, card_id, boardlib.OWNER, version=version, boards_dir=self._boards_dir,
        ))
        if not ok:
            return
        self._send_card_result(card)

    def _can_move_post(self, card_id, payload):
        self._handle_can_move(card_id, payload.get("to"))

    _POST_ACTIONS = {
        "move": _move_card,
        "comment": _comment_card,
        "edit": _edit_card,
        "reorder": _reorder_card,
        "delete": _delete_card,
        "can-move": _can_move_post,
    }

    def _route_post(self, path, payload):
        if path == "/api/cards":
            return self._create_card(payload)
        match = CARD_ACTION_RE.match(path)
        if match:
            card_id = unquote(match.group(1))
            action = match.group(2)
            return self._POST_ACTIONS[action](self, card_id, payload)
        self._send_error(404, [f"адреса «{path}» нет"])

    # -------------------------------------------------- точки входа http.server

    def do_GET(self):
        try:
            if not self._guard_common():
                return
            parsed = urlsplit(self.path)
            path = parsed.path
            if path == "/health":
                return self._handle_health()
            if path == "/api/state":
                return self._handle_state(parse_qs(parsed.query))
            match = CAN_MOVE_GET_RE.match(path)
            if match:
                query = parse_qs(parsed.query)
                to_value = (query.get("to") or [None])[0]
                return self._handle_can_move(unquote(match.group(1)), to_value)
            if path.startswith("/api/"):
                return self._send_error(404, [f"адреса «{path}» нет"])
            return self._serve_static(path)
        except Exception as exc:  # noqa: BLE001 - последняя сетка перед голым traceback у клиента
            self._log_5xx(500, exc)
            self._reject(500, INTERNAL_ERROR_MESSAGE)

    def do_POST(self):
        try:
            if not self._guard_common():
                return
            if not self._guard_post():
                return
            try:
                payload = self._read_json_body()
            except _BodyError as exc:
                self._reject(exc.status, exc.message, protection=exc.protection)
                return
            parsed = urlsplit(self.path)
            self._route_post(parsed.path, payload)
        except Exception as exc:  # noqa: BLE001
            self._log_5xx(500, exc)
            self._reject(500, INTERNAL_ERROR_MESSAGE)

    def do_OPTIONS(self):
        try:
            if not self._guard_common():
                return
            # Заголовки CORS не отдаются никогда; преflight в любом виде отклоняется.
            self._reject(405, "OPTIONS не поддерживается", protection=True)
        except Exception as exc:  # noqa: BLE001
            self._log_5xx(500, exc)
            self._reject(500, INTERNAL_ERROR_MESSAGE)


# ---------------------------------------------------------------- сборка сервера


def _resolve_port(port):
    if port is not None:
        return int(port)
    env = os.environ.get("BOARD_PORT")
    if env:
        return int(env)
    return DEFAULT_PORT


def _default_page_dir():
    return _HERE / "page"


def make_server(port=None, boards_dir=None, page_dir=None):
    """Собрать (но не запустить) ThreadingHTTPServer на 127.0.0.1.

    port=0 — ОС выбирает свободный порт; фактический порт — server.server_address[1] (используют
    и тесты, и сам сервер для проверки заголовков Host/Origin — так им не нужно знать порт заранее).
    Поднимает OSError, если запрошенный порт занят (например, второй экземпляр) — main() это ловит.
    """
    requested_port = _resolve_port(port)
    resolved_boards_dir = boardlib.data_dir(boards_dir)
    resolved_page_dir = Path(page_dir) if page_dir is not None else _default_page_dir()

    server = BoardHTTPServer(("127.0.0.1", requested_port), BoardRequestHandler)
    server.board_dir = resolved_boards_dir
    server.board_page_dir = resolved_page_dir
    server.board_port = server.server_address[1]
    server.board_logger = _make_logger(resolved_boards_dir)
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="board-server", description="Локальная канбан-доска: страница, состояние, изменения.",
    )
    parser.add_argument("--port", type=int, default=None, help="порт (по умолчанию 8790)")
    args = parser.parse_args(argv)

    try:
        server = make_server(port=args.port)
    except boardlib.BoardError as exc:
        # Каталог данных не найден (boardlib.data_dir): журналу писать некуда — сообщение в stderr,
        # если консоль есть (под pythonw её нет), и код 1.
        if sys.stderr is not None:
            for message in exc.messages:
                print(message, file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        port = _resolve_port(args.port)
        boards_dir = boardlib.data_dir(None)
        logger = _make_logger(boards_dir)
        logger.warning("порт занят, второй экземпляр не запущен (порт %s, %s)", port, exc)
        _close_logger(logger)
        sys.exit(0)

    logger = server.board_logger
    logger.info(
        "запуск: 127.0.0.1:%s, данные %s, страница %s",
        server.board_port, server.board_dir, server.board_page_dir,
    )

    def _shutdown(signum, _frame):
        logger.info("остановка по сигналу %s", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    try:
        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)
    except (ValueError, AttributeError):  # pragma: no cover - не в главном потоке или нет сигнала на ОС
        pass

    try:
        server.serve_forever()
    except Exception:  # noqa: BLE001 - под pythonw эта трассировка иначе улетела бы в никуда
        logger.error("необработанное исключение верхнего уровня", exc_info=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - запасная сетка: даже сбой до логгера не должен трассировкой в консоль
        try:
            logging.getLogger("board-server-fallback").error("сбой запуска", exc_info=True)
        except Exception:
            pass
        sys.exit(1)
