"""Проверки server.py: локальная программа доски — статика, состояние, защита, приём изменений.

Сервер поднимается на порту 0 (ОС выбирает свободный) в фоновом потоке текущего процесса,
каталог данных и каталог страницы — временные (tempfile), порт 8790 не занимается.
Запуск из папки board:

    PYTHONIOENCODING=utf-8 python -m unittest discover -s tests -p "test_server.py" -v
"""
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

BOARD = Path(__file__).resolve().parents[1]
if str(BOARD) not in sys.path:
    sys.path.insert(0, str(BOARD))

import boardlib as bl  # noqa: E402
import server  # noqa: E402

PROOF = "тесты прошли, проверено скриптом"
HOW = "открыть доску, нажать «все проекты» → карточка на месте"


# ---------------------------------------------------------------- вспомогательное


def _request(port, method, path, payload=None, raw_body=None, headers=None, host=None):
    """POST/GET через http.client с полным контролем заголовков (нужен произвольный Host)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        data = None
        hdrs = dict(headers or {})
        if raw_body is not None:
            data = raw_body if isinstance(raw_body, bytes) else str(raw_body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        elif payload is not None:
            data = json.dumps(payload).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        if data is not None:
            hdrs["Content-Length"] = str(len(data))
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host if host is not None else f"127.0.0.1:{port}")
        for key, value in hdrs.items():
            conn.putheader(key, value)
        conn.endheaders(data)
        resp = conn.getresponse()
        raw = resp.read()
        return resp, raw
    finally:
        conn.close()


class ServerCase(unittest.TestCase):
    """Один сервер на временных каталогах, поднятый в фоновом потоке, на каждый тест."""

    PROJECT = "demo"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.boards_dir = Path(tmp.name) / "boards"
        self.page_dir = Path(tmp.name) / "page"
        self.boards_dir.mkdir()
        self.page_dir.mkdir()
        (self.page_dir / "index.html").write_text("<!doctype html><p>доска</p>", encoding="utf-8")
        (self.page_dir / "board.css").write_text("body{color:#000}", encoding="utf-8")
        (self.page_dir / "board.js").write_text("console.log('board')", encoding="utf-8")

        bl.create_project(self.PROJECT, "тестовый проект для сервера", "/tmp/demo", boards_dir=str(self.boards_dir))

        self.srv = server.make_server(port=0, boards_dir=str(self.boards_dir), page_dir=str(self.page_dir))
        self.addCleanup(self._stop_server)
        self.thread = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.port = self.srv.server_address[1]

    def _stop_server(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=5)

    def req(self, method, path, **kwargs):
        return _request(self.port, method, path, **kwargs)

    def get_json(self, path, **kwargs):
        resp, raw = self.req("GET", path, **kwargs)
        return resp, json.loads(raw.decode("utf-8"))

    def post_json(self, path, payload, **kwargs):
        resp, raw = self.req("POST", path, payload=payload, **kwargs)
        body = json.loads(raw.decode("utf-8")) if raw else {}
        return resp, body

    def create_card(self, **fields):
        fields.setdefault("project", self.PROJECT)
        fields.setdefault("title", "карточка теста")
        resp, body = self.post_json("/api/cards", fields)
        self.assertEqual(resp.status, 200, body)
        return body["card"]

    def project_file_bytes(self):
        return bl.project_path(self.PROJECT, boards_dir=str(self.boards_dir)).read_bytes()


# ---------------------------------------------------------------- функции защиты напрямую (без сервера)


class PureFunctionTests(unittest.TestCase):
    def test_is_loopback_address(self):
        self.assertTrue(server.is_loopback_address("127.0.0.1"))
        self.assertTrue(server.is_loopback_address("::1"))
        self.assertFalse(server.is_loopback_address("10.0.0.5"))
        self.assertFalse(server.is_loopback_address("192.168.1.10"))
        self.assertFalse(server.is_loopback_address(""))

    def test_is_allowed_host(self):
        self.assertTrue(server.is_allowed_host("127.0.0.1:8790", 8790))
        self.assertTrue(server.is_allowed_host("LOCALHOST:8790", 8790))
        self.assertFalse(server.is_allowed_host("evil.example", 8790))
        self.assertFalse(server.is_allowed_host("127.0.0.1:9999", 8790))
        self.assertFalse(server.is_allowed_host("", 8790))
        self.assertFalse(server.is_allowed_host(None, 8790))

    def test_is_allowed_origin(self):
        self.assertTrue(server.is_allowed_origin(None, 8790))
        self.assertTrue(server.is_allowed_origin("", 8790))
        self.assertTrue(server.is_allowed_origin("http://127.0.0.1:8790", 8790))
        self.assertTrue(server.is_allowed_origin("HTTP://LOCALHOST:8790", 8790))
        self.assertFalse(server.is_allowed_origin("https://evil.example", 8790))
        self.assertFalse(server.is_allowed_origin("http://127.0.0.1:1234", 8790))

    def test_split_card_id(self):
        self.assertEqual(server.split_card_id("demo-12"), "demo")
        self.assertEqual(server.split_card_id("demo-chat-3"), "demo-chat")
        self.assertEqual(server.split_card_id("noHyphenAtAll"), "")

    def test_required_fields_for_move_review_needs_ask(self):
        card = {"ask": "", "proof": "", "accept_how": "", "rework": "", "acceptance": bl.OWNER}
        required = server.required_fields_for_move(card, bl.NEW, bl.REVIEW)
        self.assertIn("ask", required)

    def test_required_fields_for_move_done_needs_proof_and_accept_how(self):
        card = {"ask": "решено", "proof": "", "accept_how": "", "rework": "", "acceptance": bl.OWNER}
        required = server.required_fields_for_move(card, bl.IN_PROGRESS, bl.DONE)
        self.assertIn("proof", required)
        self.assertIn("accept_how", required)

    def test_required_fields_for_move_rework_always_needs_fresh_proof(self):
        card = {"ask": "", "proof": "старое доказательство", "accept_how": "смотри", "rework": "было", "acceptance": bl.OWNER}
        required = server.required_fields_for_move(card, bl.IN_PROGRESS, bl.DONE)
        self.assertIn("proof", required)  # прежнее не засчитывается

    def test_required_fields_for_move_rework_transition_needs_comment(self):
        card = {"ask": "", "proof": "x", "accept_how": "x", "rework": "", "acceptance": bl.OWNER}
        required = server.required_fields_for_move(card, bl.DONE, bl.IN_PROGRESS)
        self.assertIn("comment", required)

    def test_required_fields_for_move_return_from_accepted_needs_comment(self):
        # Спека 2026-09-29, пункт 4 / готово-когда 6
        card = {"ask": "", "proof": "x", "accept_how": HOW, "rework": "", "acceptance": bl.REVIEWER}
        self.assertIn("comment", server.required_fields_for_move(card, bl.ACCEPTED, bl.IN_PROGRESS))

    def test_required_fields_for_move_done_needs_accept_how_for_reviewer_too(self):
        # Готово-когда 1: критерий обязателен у любой приёмки; записанный без « → » спрашивается заново
        card = {"ask": "", "proof": "x", "accept_how": "", "rework": "", "acceptance": bl.REVIEWER}
        self.assertIn("accept_how", server.required_fields_for_move(card, bl.IN_PROGRESS, bl.DONE))
        card["accept_how"] = "посмотреть глазами"
        self.assertIn("accept_how", server.required_fields_for_move(card, bl.IN_PROGRESS, bl.DONE))
        card["accept_how"] = HOW
        self.assertNotIn("accept_how", server.required_fields_for_move(card, bl.IN_PROGRESS, bl.DONE))

    def test_optional_fields_for_move_accepted_offers_caveat(self):
        card = {"ask": "", "proof": "x", "accept_how": "x", "rework": "", "acceptance": bl.REVIEWER}
        required = server.required_fields_for_move(card, bl.DONE, bl.ACCEPTED)
        optional = server.optional_fields_for_move(card, bl.DONE, bl.ACCEPTED)
        self.assertNotIn("caveat", required)  # boardlib примет переход и без оговорки
        self.assertIn("caveat", optional)

    def test_optional_fields_for_move_empty_for_non_accepted(self):
        card = {"ask": "", "proof": "x", "accept_how": "x", "rework": "", "acceptance": bl.OWNER}
        self.assertEqual(server.optional_fields_for_move(card, bl.NEW, bl.IN_PROGRESS), [])


# ---------------------------------------------------------------- health, привязка, защита


class HealthAndBindingTests(ServerCase):
    def test_health_ok(self):
        resp, body = self.get_json("/health")
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, {"ok": True})
        self.assertEqual(resp.getheader("Content-Type"), "application/json; charset=utf-8")
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        for name in resp.getheaders():
            self.assertFalse(name[0].lower().startswith("access-control"))

    def test_server_bound_to_loopback(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")

    def test_host_header_wrong_rejected(self):
        resp, body = self.get_json("/health", host="evil.example")
        self.assertEqual(resp.status, 403)
        self.assertIn("error", body)

    def test_host_header_localhost_accepted(self):
        resp, body = self.get_json("/health", host=f"localhost:{self.port}")
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, {"ok": True})

    def test_options_rejected_without_cors(self):
        resp, raw = self.req("OPTIONS", "/api/cards")
        self.assertEqual(resp.status, 405)
        for name in resp.getheaders():
            self.assertFalse(name[0].lower().startswith("access-control"))

    def test_post_wrong_content_type_415(self):
        resp, body = self.post_json_raw("/api/cards", b"{}", content_type="text/plain")
        self.assertEqual(resp.status, 415)

    def post_json_raw(self, path, raw, content_type):
        resp, raw_resp = self.req("POST", path, raw_body=raw, headers={"Content-Type": content_type})
        body = json.loads(raw_resp.decode("utf-8")) if raw_resp else {}
        return resp, body

    def test_post_wrong_origin_403(self):
        resp, body = self.post_json(
            "/api/cards", {"project": self.PROJECT, "title": "x"},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(resp.status, 403)

    def test_post_matching_origin_accepted(self):
        resp, body = self.post_json(
            "/api/cards", {"project": self.PROJECT, "title": "своя карточка"},
            headers={"Origin": f"http://127.0.0.1:{self.port}"},
        )
        self.assertEqual(resp.status, 200, body)

    def test_body_too_large_413(self):
        payload = {"project": self.PROJECT, "title": "x", "body": "ф" * (300 * 1024)}
        resp, body = self.post_json("/api/cards", payload)
        self.assertEqual(resp.status, 413)

    def test_bad_json_400(self):
        resp, body = self.post_json_raw("/api/cards", "{не json", content_type="application/json")
        self.assertEqual(resp.status, 400)

    def test_unknown_api_route_404(self):
        resp, body = self.get_json("/api/nothing-here")
        self.assertEqual(resp.status, 404)
        self.assertIn("error", body)

    def test_host_header_wrong_rejected_on_state_no_board_data_leaked(self):
        resp, body = self.get_json("/api/state", host="evil.example")
        self.assertEqual(resp.status, 403)
        self.assertEqual(set(body.keys()), {"error"})
        self.assertNotIn("projects", body)
        self.assertNotIn("summary", body)
        self.assertNotIn("token", body)

    def test_server_header_hides_python_version(self):
        resp, _raw = self.req("GET", "/health")
        value = resp.getheader("Server") or ""
        self.assertIn("TatetBoard", value)
        self.assertNotIn("Python", value)

    def test_known_boardlib_error_keeps_original_message(self):
        # 400 от boardlib (заголовок карточки обязателен) — фраза остаётся оригинальной, не подменяется
        # общей фразой про внутреннюю ошибку (та зарезервирована только за 500).
        resp, body = self.post_json("/api/cards", {"project": self.PROJECT})
        self.assertEqual(resp.status, 400)
        joined = " ".join(body["error"])
        self.assertIn("заголовок", joined)
        self.assertNotIn(server.INTERNAL_ERROR_MESSAGE, joined)


# ---------------------------------------------------------------- статика


class StaticTests(ServerCase):
    def test_index_html_served(self):
        resp, raw = self.req("GET", "/")
        self.assertEqual(resp.status, 200)
        self.assertIn(b"\xd0\xb4\xd0\xbe\xd1\x81\xd0\xba\xd0\xb0", raw)  # "доска" в utf-8
        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
        self.assertTrue((resp.getheader("Content-Type") or "").startswith("text/html"))

    def test_traversal_dotdot_404(self):
        resp, raw = self.req("GET", "/../boardlib.py")
        self.assertEqual(resp.status, 404)
        self.assertIn("ещё не собрана".encode("utf-8"), raw)

    def test_traversal_nested_404(self):
        resp, raw = self.req("GET", "/board.css/../../x")
        self.assertEqual(resp.status, 404)

    def test_missing_page_message(self):
        resp, raw = self.req("GET", "/no-such-file")
        self.assertEqual(resp.status, 404)
        self.assertIn("ещё не собрана".encode("utf-8"), raw)


# ---------------------------------------------------------------- /api/state


class StateTests(ServerCase):
    def test_state_unchanged_then_full_after_write(self):
        resp, body = self.get_json("/api/state")
        self.assertEqual(resp.status, 200)
        token = body["token"]
        self.assertIn("projects", body)
        self.assertIn("statuses", body)
        self.assertIn("meta", body)

        resp2, body2 = self.get_json(f"/api/state?since={token}")
        self.assertEqual(resp2.status, 200)
        self.assertEqual(body2, {"token": token, "unchanged": True})

        self.create_card(title="новая карточка меняет токен")

        resp3, body3 = self.get_json(f"/api/state?since={token}")
        self.assertEqual(resp3.status, 200)
        self.assertNotIn("unchanged", body3)
        self.assertNotEqual(body3["token"], token)
        projects = {p["project"]: p for p in body3["projects"]}
        self.assertIn(self.PROJECT, projects)
        titles = [c["title"] for c in projects[self.PROJECT]["cards"]]
        self.assertIn("новая карточка меняет токен", titles)

    def test_state_statuses_order_and_titles(self):
        _resp, body = self.get_json("/api/state")
        keys = [s["key"] for s in body["statuses"]]
        self.assertEqual(keys, list(bl.COLUMNS) + list(bl.ARCHIVE))
        titles = {s["key"]: s["title"] for s in body["statuses"]}
        self.assertEqual(titles[bl.ACCEPTED], "Завершена")

    def test_state_meta_has_russian_titles(self):
        _resp, body = self.get_json("/api/state")
        self.assertEqual(body["meta"]["roles"][bl.OWNER], "владелец")
        self.assertEqual(body["meta"]["priorities"]["A"], "сейчас")


# ---------------------------------------------------------------- создание, правка, комментарий


class CardMutationTests(ServerCase):
    def test_create_card(self):
        card = self.create_card(title="первая карточка", priority="A", tag="urgent")
        self.assertTrue(card["id"].startswith(f"{self.PROJECT}-"))
        self.assertEqual(card["priority"], "A")
        self.assertEqual(card["tag"], "urgent")
        self.assertEqual(card["status"], bl.BACKLOG)

    def test_create_card_missing_title_400(self):
        resp, body = self.post_json("/api/cards", {"project": self.PROJECT})
        self.assertEqual(resp.status, 400)
        self.assertIn("error", body)

    def test_comment_card(self):
        card = self.create_card()
        resp, body = self.post_json(
            f"/api/cards/{card['id']}/comment", {"version": card["version"], "text": "первый комментарий"},
        )
        self.assertEqual(resp.status, 200, body)
        comments = body["card"]["comments"]
        self.assertEqual(comments[-1]["text"], "первый комментарий")
        self.assertEqual(comments[-1]["who"], bl.OWNER)

    def test_comment_missing_version_400(self):
        card = self.create_card()
        resp, body = self.post_json(f"/api/cards/{card['id']}/comment", {"text": "текст без версии"})
        self.assertEqual(resp.status, 400)

    def test_edit_card(self):
        card = self.create_card(title="старый заголовок")
        resp, body = self.post_json(
            f"/api/cards/{card['id']}/edit",
            {"version": card["version"], "title": "новый заголовок", "priority": "C"},
        )
        self.assertEqual(resp.status, 200, body)
        self.assertEqual(body["card"]["title"], "новый заголовок")
        self.assertEqual(body["card"]["priority"], "C")
        self.assertGreater(body["card"]["version"], card["version"])

    def test_reorder_card(self):
        card = self.create_card()
        resp, body = self.post_json(
            f"/api/cards/{card['id']}/reorder", {"version": card["version"], "order": 42, "priority": "A"},
        )
        self.assertEqual(resp.status, 200, body)
        self.assertEqual(body["card"]["order"], 42)
        self.assertEqual(body["card"]["priority"], "A")

    def test_delete_card(self):
        card = self.create_card()
        resp, body = self.post_json(f"/api/cards/{card['id']}/delete", {"version": card["version"]})
        self.assertEqual(resp.status, 200, body)
        remaining = bl.list_cards(self.PROJECT, boards_dir=str(self.boards_dir))
        self.assertFalse(any(c["id"] == card["id"] for c in remaining))


# ---------------------------------------------------------------- перенос: роль, версия, доработка


class MoveTests(ServerCase):
    def test_move_writes_owner_role_even_if_body_says_agent(self):
        card = self.create_card()
        resp, body = self.post_json(
            f"/api/cards/{card['id']}/move",
            {"to": "in_progress", "version": card["version"], "role": "agent"},
        )
        self.assertEqual(resp.status, 200, body)
        last_entry = body["card"]["history"][-1]
        self.assertEqual(last_entry["who"], bl.OWNER)

    def test_move_missing_version_400(self):
        card = self.create_card()
        resp, body = self.post_json(f"/api/cards/{card['id']}/move", {"to": "in_progress"})
        self.assertEqual(resp.status, 400)

    def test_move_stale_version_conflict_and_file_unchanged(self):
        card = self.create_card()
        before = self.project_file_bytes()
        resp, body = self.post_json(
            f"/api/cards/{card['id']}/move", {"to": "in_progress", "version": card["version"] + 5},
        )
        self.assertEqual(resp.status, 409, body)
        after = self.project_file_bytes()
        self.assertEqual(before, after)

    def test_move_rework_without_comment_400(self):
        card = bl.add_card(
            self.PROJECT, bl.OWNER, "карточка на приёмке", status=bl.DONE,
            proof=PROOF, accept_how=HOW, boards_dir=str(self.boards_dir),
        )
        resp, body = self.post_json(f"/api/cards/{card['id']}/move", {"to": "in_progress", "version": card["version"]})
        self.assertEqual(resp.status, 400, body)

    def test_move_rework_with_comment_sets_rework_field(self):
        card = bl.add_card(
            self.PROJECT, bl.OWNER, "карточка на приёмке 2", status=bl.DONE,
            proof=PROOF, accept_how=HOW, boards_dir=str(self.boards_dir),
        )
        resp, body = self.post_json(
            f"/api/cards/{card['id']}/move",
            {"to": "in_progress", "version": card["version"], "comment": "не то поле, переделай"},
        )
        self.assertEqual(resp.status, 200, body)
        self.assertEqual(body["card"]["rework"], "не то поле, переделай")

    def test_move_return_from_accepted_requires_comment(self):
        # Готово-когда 6: владелец возвращает из «Завершена» только с комментарием; без него 400,
        # в истории «владелец: Завершена → В работе».
        card = bl.add_card(
            self.PROJECT, bl.OWNER, "принятая карточка", status=bl.DONE,
            proof=PROOF, accept_how=HOW, boards_dir=str(self.boards_dir),
        )
        resp, body = self.post_json(f"/api/cards/{card['id']}/move", {"to": "accepted", "version": card["version"]})
        self.assertEqual(resp.status, 200, body)
        card = body["card"]
        before = self.project_file_bytes()
        resp, body = self.post_json(f"/api/cards/{card['id']}/move", {"to": "in_progress", "version": card["version"]})
        self.assertEqual(resp.status, 400, body)
        self.assertEqual(before, self.project_file_bytes())
        resp, body = self.get_json(f"/api/cards/{card['id']}/can-move?to=in_progress")
        self.assertIn("comment", body["required"])
        resp, body = self.post_json(
            f"/api/cards/{card['id']}/move",
            {"to": "in_progress", "version": card["version"], "comment": "проверь на телефоне"},
        )
        self.assertEqual(resp.status, 200, body)
        last = body["card"]["history"][-1]
        self.assertEqual((last["who"], last["from"], last["to"]), (bl.OWNER, bl.ACCEPTED, bl.IN_PROGRESS))
        self.assertEqual(body["card"]["rework"], "проверь на телефоне")

    def test_state_summary_has_verify_pending_and_meta_titles(self):
        # Пункт 13: сводка отдаёт счётчик «Ждёт проверки агентом», приёмка reviewer называется «проверяющий»
        bl.add_card(self.PROJECT, bl.OWNER, "ждёт агента", status=bl.DONE, proof=PROOF, accept_how=HOW,
                    boards_dir=str(self.boards_dir))
        _resp, body = self.get_json("/api/state")
        self.assertEqual(body["summary"]["verify_pending"], 1)
        self.assertEqual(body["summary"]["waiting"], [])
        self.assertEqual(body["meta"]["acceptance"][bl.REVIEWER], "проверяющий")

    def test_move_to_review_without_ask_400(self):
        card = self.create_card(status="in_progress")
        resp, body = self.post_json(f"/api/cards/{card['id']}/move", {"to": "review", "version": card["version"]})
        self.assertEqual(resp.status, 400, body)

    def test_move_to_review_with_ask_ok(self):
        card = self.create_card(status="in_progress")
        resp, body = self.post_json(
            f"/api/cards/{card['id']}/move",
            {"to": "review", "version": card["version"], "ask": "что решаем?"},
        )
        self.assertEqual(resp.status, 200, body)
        self.assertEqual(body["card"]["status"], bl.REVIEW)


# ---------------------------------------------------------------- can-move


class CanMoveTests(ServerCase):
    def test_can_move_get_allowed_without_required(self):
        card = self.create_card()
        resp, body = self.get_json(f"/api/cards/{card['id']}/can-move?to=in_progress")
        self.assertEqual(resp.status, 200, body)
        self.assertTrue(body["allowed"])
        self.assertEqual(body["required"], [])
        self.assertEqual(body["optional"], [])

    def test_can_move_get_reports_required_ask(self):
        card = self.create_card(status="in_progress")
        resp, body = self.get_json(f"/api/cards/{card['id']}/can-move?to=review")
        self.assertEqual(resp.status, 200, body)
        self.assertTrue(body["allowed"])
        self.assertIn("ask", body["required"])

    def test_can_move_post(self):
        card = self.create_card(status="in_progress")
        resp, body = self.post_json(f"/api/cards/{card['id']}/can-move", {"to": "review"})
        self.assertEqual(resp.status, 200, body)
        self.assertIn("ask", body["required"])

    def test_can_move_accepted_reports_caveat_as_optional_not_required(self):
        card = bl.add_card(
            self.PROJECT, bl.OWNER, "готова к приёмке", status=bl.DONE,
            proof=PROOF, accept_how=HOW, boards_dir=str(self.boards_dir),
        )
        resp, body = self.get_json(f"/api/cards/{card['id']}/can-move?to=accepted")
        self.assertEqual(resp.status, 200, body)
        self.assertTrue(body["allowed"])
        self.assertNotIn("caveat", body["required"])
        self.assertIn("caveat", body["optional"])

    def test_can_move_same_status_not_allowed(self):
        card = self.create_card()
        resp, body = self.get_json(f"/api/cards/{card['id']}/can-move?to=backlog")
        self.assertEqual(resp.status, 200, body)
        self.assertFalse(body["allowed"])
        self.assertTrue(body["reason"])


# ---------------------------------------------------------------- 500: текст-заглушка, без путей


class InternalErrorTests(ServerCase):
    def test_broken_project_file_returns_generic_message_without_path(self):
        project_file = bl.project_path(self.PROJECT, boards_dir=str(self.boards_dir))
        project_file.write_text("{это не json совсем", encoding="utf-8")
        resp, body = self.get_json(f"/api/cards/{self.PROJECT}-1/can-move?to=in_progress")
        self.assertEqual(resp.status, 500)
        self.assertEqual(body, {"error": [server.INTERNAL_ERROR_MESSAGE]})
        dumped = json.dumps(body)
        self.assertNotIn(str(project_file), dumped)
        self.assertNotIn(str(self.boards_dir), dumped)
        self.assertNotIn(":\\", dumped)  # ни одного похожего на путь Windows фрагмента


# ---------------------------------------------------------------- таймаут недосланного тела


class TimeoutTests(ServerCase):
    def setUp(self):
        self._orig_timeout = server.BoardRequestHandler.timeout
        server.BoardRequestHandler.timeout = 1  # без подмены пришлось бы ждать 30 с
        self.addCleanup(self._restore_timeout)
        super().setUp()

    def _restore_timeout(self):
        server.BoardRequestHandler.timeout = self._orig_timeout

    def test_incomplete_body_times_out_and_server_stays_up(self):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        request = (
            f"POST /api/cards HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.port}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: 100\r\n\r\n"
        ).encode("utf-8") + b"0123456789"  # только 10 из заявленных 100 байт тела
        try:
            sock.sendall(request)
            try:
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break  # сервер сам закрыл соединение, не дождавшись остатка тела
            except socket.timeout:
                self.fail("сервер не закрыл соединение с недосланным телом за отведённое время")
        finally:
            sock.close()

        # поток обработчика освободился — сервер отвечает на новый запрос как ни в чём не бывало
        resp, body = self.get_json("/health")
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, {"ok": True})


# ---------------------------------------------------------------- второй экземпляр (subprocess)


class SecondInstanceTests(unittest.TestCase):
    def test_port_busy_exits_zero_and_logs(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        boards_dir = Path(tmp.name) / "boards"
        boards_dir.mkdir()

        occupier = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(occupier.close)
        occupier.bind(("127.0.0.1", 0))
        occupier.listen(1)
        port = occupier.getsockname()[1]

        env = dict(os.environ)
        env["BOARD_DIR"] = str(boards_dir)
        env["PYTHONIOENCODING"] = "utf-8"
        server_py = BOARD / "server.py"

        result = subprocess.run(
            [sys.executable, str(server_py), "--port", str(port)],
            env=env, capture_output=True, timeout=20, text=True,
        )
        self.assertEqual(result.returncode, 0, f"stdout={result.stdout!r} stderr={result.stderr!r}")

        log_path = boards_dir / "board.log"
        self.assertTrue(log_path.exists())
        content = log_path.read_text(encoding="utf-8")
        self.assertIn("порт занят, второй экземпляр не запущен", content)


if __name__ == "__main__":
    unittest.main()
