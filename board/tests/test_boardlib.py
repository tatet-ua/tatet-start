"""Проверки boardlib: модель и таблица «Кто что может», обязательные поля, хранилище, сводка.

Все тесты работают во временных папках (tempfile); каталог boards/ репозитория не трогается.
Запуск из папки board:

    PYTHONIOENCODING=utf-8 python -m unittest discover -s tests -p "test_boardlib.py" -v
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

BOARD = Path(__file__).resolve().parents[1]
if str(BOARD) not in sys.path:
    sys.path.insert(0, str(BOARD))

import boardlib as bl  # noqa: E402

P = "demo"
OWNER, AGENT, REVIEWER, OWNER_CHAT = bl.OWNER, bl.AGENT, bl.REVIEWER, bl.OWNER_CHAT
SAID = "ок, финиш такой, спринт открывай"

PROOF = "тесты прошли, проверено хуком"
HOW = "открыть доску, нажать «все проекты» → карточка видна в колонке"
HOW_NO_SEP = "открыть доску и посмотреть"
COMMAND = "curl -sI https://example.com/health"
OUTPUT = "HTTP/2 200\nserver: nginx\ncontent-type: application/json\n"


def at(text):
    """Местное время из «ГГГГ-ММ-ДД ЧЧ:ММ» с часовым поясом машины."""
    return datetime.strptime(text, "%Y-%m-%d %H:%M").astimezone()


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = tmp.name
        bl.create_project(P, "проверочный проект", "X:/project/demo", boards_dir=self.d)

    # --- помощники -------------------------------------------------------
    def add(self, role=OWNER, title="Задача для проверки", **kw):
        return bl.add_card(P, role, title=title, boards_dir=self.d, **kw)

    def move(self, card_id, to, role, **kw):
        return bl.move_card(P, card_id, to, role, boards_dir=self.d, **kw)

    def to_progress(self, card_id, role=AGENT):
        self.move(card_id, "Новая", role)
        return self.move(card_id, "В работе", role)

    def to_done(self, card_id, role=AGENT):
        self.to_progress(card_id, role)
        return self.move(card_id, "Выполнена", role, proof=PROOF, accept_how=HOW)

    def file_bytes(self):
        return bl.project_path(P, self.d).read_bytes()

    def wipe_field(self, card_id, field):
        """Правка файла руками в обход модуля — так проверяются условия, которых модуль не создаёт."""
        file = bl.project_path(P, self.d)
        data = json.loads(file.read_text(encoding="utf-8"))
        for card in data["cards"]:
            if card["id"] == card_id:
                card[field] = ""
        file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ====================================================================== модель


class TestModel(Base):
    def test_columns_and_archive(self):
        self.assertEqual(bl.COLUMNS, ("backlog", "new", "in_progress", "review", "done", "accepted"))
        self.assertEqual(bl.ARCHIVE, ("postponed", "cancelled"))
        self.assertEqual(bl.STATUSES["review"], "На согласовании")

    def test_parse_status_key_and_russian(self):
        self.assertEqual(bl.parse_status("in_progress"), bl.IN_PROGRESS)
        self.assertEqual(bl.parse_status("в работе"), bl.IN_PROGRESS)
        self.assertEqual(bl.parse_status("В_работе"), bl.IN_PROGRESS)
        self.assertEqual(bl.parse_status("ЗАВЕРШЕНА"), bl.ACCEPTED)
        with self.assertRaises(bl.ValidationError):
            bl.parse_status("выполнено когда-нибудь")

    def test_parse_role_and_acceptance(self):
        self.assertEqual(bl.parse_role("владелец"), OWNER)
        self.assertEqual(bl.parse_role("Ревьюер"), REVIEWER)
        self.assertEqual(bl.parse_acceptance("владелец"), OWNER)
        self.assertEqual(bl.parse_tag("срочно"), "urgent")
        self.assertIsNone(bl.parse_tag(""))

    def test_stage_acceptance_defaults(self):
        # Спека 2026-09-29, пункт 7: все этапы принимает проверяющий; «принимаю сам» ставит владелец.
        for number in bl.STAGES:
            card = self.add(title=f"Этап {number}", kind="этап", stage=number)
            self.assertEqual(card["acceptance"], REVIEWER, f"этап {number}")

    def test_task_acceptance_is_reviewer(self):
        self.assertEqual(self.add()["acceptance"], REVIEWER)
        self.assertEqual(self.add(AGENT, title="Задача агента")["acceptance"], REVIEWER)

    def test_acceptance_titles_and_aliases(self):
        self.assertEqual(bl.acceptance_title(REVIEWER), "проверяющий")
        self.assertEqual(bl.role_title(REVIEWER), "проверяющий")
        self.assertEqual(bl.parse_acceptance("агент"), REVIEWER)
        self.assertEqual(bl.parse_acceptance("принимаю сам"), OWNER)
        self.assertEqual(bl.parse_acceptance("ревьюер"), REVIEWER)  # прежнее слово — синоним

    def test_accept_how_parts(self):
        self.assertEqual(bl.accept_how_parts("curl -sI https://a.b → 200, редирект с www на без www"),
                         ("curl -sI https://a.b", "200, редирект с www на без www"))
        for bad in ("", "без стрелки", " → пусто слева", "пусто справа → ", None, 5):
            with self.subTest(text=bad):
                self.assertIsNone(bl.accept_how_parts(bad))
        self.assertIsNone(bl.accept_how_error(HOW))
        self.assertIn("обязателен", bl.accept_how_error(""))
        self.assertIn("что выполнить → что должно получиться", bl.accept_how_error(HOW_NO_SEP))

    def test_stage_card_is_unique(self):
        self.add(title="Этап 4", kind="этап", stage=4)
        with self.assertRaises(bl.BoardError):
            self.add(title="Этап 4 снова", kind="этап", stage=4)
        self.assertIsNotNone(bl.find_stage_card(P, 4, boards_dir=self.d))
        self.assertIsNone(bl.find_stage_card(P, 7, boards_dir=self.d))


# ============================================ таблица «Кто что может», строка за строкой


class TestWhoCanWhat(Base):
    # строка 1: создать карточку, написать комментарий — владелец и агент
    def test_owner_creates_card(self):
        card = self.add(OWNER)
        self.assertEqual(card["status"], bl.BACKLOG)
        self.assertEqual(card["id"], "demo-1")
        self.assertEqual(card["history"][0]["who"], OWNER)

    def test_agent_creates_card(self):
        card = self.add(AGENT, title="Собрать страницу доски")
        self.assertEqual(card["history"][0]["who"], AGENT)

    def test_owner_comments(self):
        card = self.add()
        got = bl.comment_card(P, card["id"], OWNER, "посмотрел, годится", boards_dir=self.d)
        self.assertEqual(got["comments"][-1]["who"], OWNER)

    def test_agent_comments(self):
        card = self.add()
        got = bl.comment_card(P, card["id"], AGENT, "предлагаю отложить до следующей недели", boards_dir=self.d)
        self.assertEqual(got["comments"][-1]["who"], AGENT)

    # строка 2: Бэклог ↔ Новая, порядок и приоритет — владелец и агент
    def test_agent_moves_backlog_to_new_and_back(self):
        card = self.add()
        self.assertEqual(self.move(card["id"], "Новая", AGENT)["status"], bl.NEW)
        self.assertEqual(self.move(card["id"], "Бэклог", AGENT)["status"], bl.BACKLOG)

    def test_owner_moves_backlog_to_new(self):
        card = self.add()
        self.assertEqual(self.move(card["id"], "Новая", OWNER)["status"], bl.NEW)

    def test_both_roles_change_order_and_priority(self):
        card = self.add()
        got = bl.reorder_card(P, card["id"], AGENT, order=5, priority="A", boards_dir=self.d)
        self.assertEqual((got["order"], got["priority"]), (5, "A"))
        got = bl.reorder_card(P, card["id"], OWNER, order=1, priority="C", boards_dir=self.d)
        self.assertEqual((got["order"], got["priority"]), (1, "C"))

    # строка 3: → В работе, → На согласовании (с ask), → Выполнена (с proof)
    def test_agent_runs_the_work_lane(self):
        card = self.add()
        self.move(card["id"], "Новая", AGENT)
        self.assertEqual(self.move(card["id"], "В работе", AGENT)["status"], bl.IN_PROGRESS)
        got = self.move(card["id"], "На согласовании", AGENT, ask="какой домен берём")
        self.assertEqual((got["status"], got["ask"]), (bl.REVIEW, "какой домен берём"))
        got = self.move(card["id"], "В работе", AGENT)
        self.assertEqual(got["status"], bl.IN_PROGRESS)
        got = self.move(card["id"], "Выполнена", AGENT, proof=PROOF, accept_how=HOW)
        self.assertEqual((got["status"], got["proof"]), (bl.DONE, PROOF))

    def test_owner_answers_from_review(self):
        card = self.add()
        self.to_progress(card["id"])
        self.move(card["id"], "На согласовании", AGENT, ask="какой домен берём")
        got = self.move(card["id"], "В работе", OWNER, comment="берём короткий")
        self.assertEqual(got["status"], bl.IN_PROGRESS)

    # строка 4: Выполнена → Завершена
    def test_owner_accepts(self):
        card = self.add()
        self.to_done(card["id"])
        got = self.move(card["id"], "Завершена", OWNER)
        self.assertEqual(got["status"], bl.ACCEPTED)
        self.assertEqual(got["history"][-1]["who"], OWNER)

    def test_owner_accepts_with_caveat(self):
        card = self.add()
        self.to_done(card["id"])
        got = self.move(card["id"], "Завершена", OWNER, caveat="на телефоне колонки узкие")
        self.assertEqual(got["caveat"], "на телефоне колонки узкие")

    def test_reviewer_accepts_card_only_through_verify(self):
        # Спека 2026-09-29, пункт 10: путь проверяющего — только verify_card, move_card ролью
        # reviewer отказывает даже у карточки с приёмкой «проверяющий».
        card = self.add(title="Этап 1", kind="этап", stage=1)
        self.assertEqual(card["acceptance"], REVIEWER)
        self.to_done(card["id"])
        with self.assertRaises(bl.ForbiddenError) as ctx:
            self.move(card["id"], "Завершена", REVIEWER, proof=PROOF)
        self.assertIn("verify", str(ctx.exception))
        self.assertTrue(bl.can_move(bl.get_card(P, card["id"], boards_dir=self.d), "Завершена", REVIEWER)[0])
        got = bl.verify_card(P, card["id"], bl.VERIFY_OK, command=COMMAND, output=OUTPUT, boards_dir=self.d)
        self.assertEqual(got["status"], bl.ACCEPTED)
        self.assertEqual(got["history"][-1]["who"], REVIEWER)
        self.assertEqual(got["history"][-1]["text"], "принято проверяющим")
        self.assertEqual(bl.accepted_by(got), REVIEWER)

    def test_reviewer_cannot_accept_owner_card(self):
        card = self.add(acceptance="owner")
        self.to_done(card["id"])
        with self.assertRaises(bl.ForbiddenError) as ctx:
            bl.verify_card(P, card["id"], bl.VERIFY_OK, command=COMMAND, output=OUTPUT, boards_dir=self.d)
        self.assertIn("владелец", str(ctx.exception))
        self.assertFalse(bl.can_move(bl.get_card(P, card["id"], boards_dir=self.d), "Завершена", REVIEWER)[0])
        self.assertEqual(bl.get_card(P, card["id"], boards_dir=self.d)["status"], bl.DONE)

    def test_agent_cannot_accept(self):
        card = self.add()
        self.to_done(card["id"])
        with self.assertRaises(bl.ForbiddenError) as ctx:
            self.move(card["id"], "Завершена", AGENT)
        self.assertIn("владелец", str(ctx.exception))
        self.assertEqual(bl.get_card(P, card["id"], boards_dir=self.d)["status"], bl.DONE)

    def test_agent_cannot_create_card_in_accepted(self):
        with self.assertRaises(bl.ForbiddenError):
            self.add(AGENT, status="Завершена")

    def test_agent_cannot_reach_accepted_by_side_paths(self):
        card = self.add()
        self.to_progress(card["id"])
        with self.assertRaises(bl.ForbiddenError):
            self.move(card["id"], "Завершена", AGENT, proof=PROOF)  # В работе → Завершена
        self.move(card["id"], "Выполнена", AGENT, proof=PROOF, accept_how=HOW)
        self.move(card["id"], "Завершена", OWNER)
        with self.assertRaises(bl.ForbiddenError):  # и обратно из «Завершена» тоже нельзя
            self.move(card["id"], "В работе", AGENT)

    def test_agent_may_narrow_acceptance_to_owner_at_creation(self):
        # Спека 2026-09-29, пункт 7 и решение владельца: агент ставит «принимаю сам» (сужает права).
        self.assertEqual(self.add(AGENT, title="Письмо клиенту", acceptance="owner")["acceptance"], OWNER)
        self.assertEqual(self.add(AGENT, title="Обычная задача", acceptance="reviewer")["acceptance"], REVIEWER)
        stage = self.add(AGENT, title="Этап 4", kind="этап", stage=4, acceptance="принимаю сам")
        self.assertEqual(stage["acceptance"], OWNER)

    def test_agent_cannot_walk_around_owner_through_acceptance(self):
        """Обход: у карточки «принимаю сам» вернуть приёмку проверяющему и принять её verify_card."""
        card = self.add(OWNER, title="Дизайн главной", acceptance="owner")
        with self.assertRaises(bl.ForbiddenError):
            bl.edit_card(P, card["id"], AGENT, acceptance="reviewer", boards_dir=self.d)
        self.to_done(card["id"])
        with self.assertRaises(bl.ForbiddenError):
            bl.verify_card(P, card["id"], bl.VERIFY_OK, command=COMMAND, output=OUTPUT, boards_dir=self.d)
        got = bl.get_card(P, card["id"], boards_dir=self.d)
        self.assertEqual((got["status"], got["acceptance"]), (bl.DONE, OWNER))

    def test_agent_may_create_stage_card_but_not_repaint_it(self):
        """Этапы заводит любая роль (нужно импорту), но kind и stage потом не меняются никем."""
        card = self.add(AGENT, title="Этап 1", kind="этап", stage=1)
        self.assertEqual(card["acceptance"], REVIEWER)
        self.assertNotIn("kind", bl.edit_card.__code__.co_varnames[:bl.edit_card.__code__.co_argcount])
        self.assertNotIn("stage", bl.edit_card.__code__.co_varnames[:bl.edit_card.__code__.co_argcount])

    def test_agent_narrows_acceptance_but_cannot_lift_owner_mark(self):
        card = self.add()  # приёмка по умолчанию — проверяющий
        got = bl.edit_card(P, card["id"], AGENT, acceptance="owner", boards_dir=self.d)
        self.assertEqual(got["acceptance"], OWNER)  # сузить — можно
        with self.assertRaises(bl.ForbiddenError) as ctx:
            bl.edit_card(P, card["id"], AGENT, acceptance="reviewer", boards_dir=self.d)
        self.assertIn("владелец", str(ctx.exception))
        self.assertEqual(bl.get_card(P, card["id"], boards_dir=self.d)["acceptance"], OWNER)
        # владелец снимает пометку сам
        self.assertEqual(bl.edit_card(P, card["id"], OWNER, acceptance="reviewer", boards_dir=self.d)["acceptance"],
                         REVIEWER)

    # строка 5: Выполнена → В работе «на доработку»
    def test_owner_sends_back_to_rework(self):
        card = self.add()
        self.to_done(card["id"])
        got = self.move(card["id"], "В работе", OWNER, comment="колонки едут на узком экране")
        self.assertEqual(got["status"], bl.IN_PROGRESS)
        self.assertEqual(got["rework"], "колонки едут на узком экране")
        self.assertEqual(got["comments"][-1]["text"], "колонки едут на узком экране")
        self.assertEqual(got["history"][-1]["text"], "колонки едут на узком экране")

    def test_agent_cannot_send_back_to_rework(self):
        card = self.add()
        self.to_done(card["id"])
        with self.assertRaises(bl.ForbiddenError) as ctx:
            self.move(card["id"], "В работе", AGENT, comment="сам верну")
        self.assertIn("владелец", str(ctx.exception))

    # строка 6: → Отложена, → Отменена, возврат из архива
    def test_owner_postpones_and_cancels_from_any_column(self):
        one, two = self.add(title="Первая"), self.add(title="Вторая")
        self.assertEqual(self.move(one["id"], "Отложена", OWNER)["status"], bl.POSTPONED)
        self.to_progress(two["id"])
        self.assertEqual(self.move(two["id"], "Отменена", OWNER)["status"], bl.CANCELLED)

    def test_agent_cannot_postpone(self):
        card = self.add()
        with self.assertRaises(bl.ForbiddenError) as ctx:
            self.move(card["id"], "Отложена", AGENT)
        self.assertIn("комментарием", str(ctx.exception))

    def test_agent_cannot_cancel(self):
        card = self.add()
        with self.assertRaises(bl.ForbiddenError) as ctx:
            self.move(card["id"], "Отменена", AGENT)
        self.assertIn("комментарием", str(ctx.exception))

    def test_only_owner_returns_card_from_archive(self):
        card = self.add()
        self.move(card["id"], "Отложена", OWNER)
        with self.assertRaises(bl.ForbiddenError) as ctx:
            self.move(card["id"], "Новая", AGENT)
        self.assertIn("архив", str(ctx.exception))
        self.assertEqual(self.move(card["id"], "Новая", OWNER)["status"], bl.NEW)

    def test_agent_may_comment_archived_card(self):
        card = self.add()
        self.move(card["id"], "Отложена", OWNER)
        got = bl.comment_card(P, card["id"], AGENT, "предлагаю вернуть: стало нужно", boards_dir=self.d)
        self.assertEqual(got["comments"][-1]["who"], AGENT)

    # решения сверх спеки: владелец ходит свободно, агент — только по диаграмме
    def test_owner_may_jump_any_column(self):
        card = self.add()
        got = self.move(card["id"], "Выполнена", OWNER, proof="посмотрел глазами", accept_how=HOW)
        self.assertEqual(got["status"], bl.DONE)

    def test_agent_step_over_column_refused(self):
        card = self.add()
        with self.assertRaises(bl.ForbiddenError) as ctx:
            self.move(card["id"], "Выполнена", AGENT, proof=PROOF, accept_how=HOW)
        self.assertIn("по порядку", str(ctx.exception))

    def test_move_to_same_column_refused(self):
        card = self.add()
        with self.assertRaises(bl.ForbiddenError):
            self.move(card["id"], "Бэклог", OWNER)

    def test_agent_does_not_edit_card_waiting_for_acceptance(self):
        card = self.add()
        self.to_done(card["id"])
        with self.assertRaises(bl.ForbiddenError) as ctx:
            bl.edit_card(P, card["id"], AGENT, title="Другой заголовок", boards_dir=self.d)
        self.assertIn("ждёт приёмки", str(ctx.exception))
        with self.assertRaises(bl.ForbiddenError):
            bl.reorder_card(P, card["id"], AGENT, priority="A", boards_dir=self.d)
        # комментарий и handoff при этом можно
        bl.comment_card(P, card["id"], AGENT, "жду приёмки", boards_dir=self.d)
        bl.handoff(P, card["id"], AGENT, stopped_at="жду приёмки владельца", boards_dir=self.d)
        self.assertEqual(bl.get_card(P, card["id"], boards_dir=self.d)["title"], "Задача для проверки")
        # владельцу это не мешает
        self.assertEqual(
            bl.edit_card(P, card["id"], OWNER, title="Другой заголовок", boards_dir=self.d)["title"],
            "Другой заголовок",
        )

    def test_reviewer_cannot_accept_with_caveat(self):
        card = self.add(title="Этап 1", kind="этап", stage=1)
        self.to_done(card["id"])
        with self.assertRaises(bl.ForbiddenError):
            self.move(card["id"], "Завершена", REVIEWER, proof=PROOF, caveat="почти хорошо")
        self.assertEqual(bl.get_card(P, card["id"], boards_dir=self.d)["status"], bl.DONE)
        # у verify_card оговорки нет вовсе: приёмка с оговоркой — только у владельца
        self.assertNotIn("caveat", bl.verify_card.__code__.co_varnames[:bl.verify_card.__code__.co_argcount])

    def test_owner_returns_accepted_card_only_with_comment(self):
        # Спека 2026-09-29, пункт 4: «Завершена» → «В работе» владельцем — только с комментарием,
        # карточка получает метку «на доработку» и попадает в inbox как возврат.
        card = self.add()
        self.to_done(card["id"])
        self.move(card["id"], "Завершена", OWNER)
        with self.assertRaises(bl.ValidationError) as ctx:
            self.move(card["id"], "В работе", OWNER)
        self.assertIn("ОШИБКА  comment:", str(ctx.exception))
        self.assertEqual(bl.get_card(P, card["id"], boards_dir=self.d)["status"], bl.ACCEPTED)
        bl.ack_inbox(P, boards_dir=self.d)
        got = self.move(card["id"], "В работе", OWNER, comment="проверь ещё раз на телефоне")
        self.assertEqual((got["status"], got["rework"]), (bl.IN_PROGRESS, "проверь ещё раз на телефоне"))
        self.assertEqual(got["history"][-1]["who"], OWNER)
        self.assertEqual([e["kind"] for e in bl.read_inbox(P, boards_dir=self.d)], ["rework"])
        with self.assertRaises(bl.ValidationError):  # после возврата — свежий proof
            self.move(card["id"], "Выполнена", AGENT)


# ====================================================== обязательные поля (задача 1.3)


class TestRequiredFields(Base):
    def test_review_without_ask(self):
        card = self.add()
        self.to_progress(card["id"])
        with self.assertRaises(bl.ValidationError) as ctx:
            self.move(card["id"], "На согласовании", AGENT)
        self.assertTrue(str(ctx.exception).startswith("ОШИБКА  ask:"))
        self.assertEqual(bl.get_card(P, card["id"], boards_dir=self.d)["status"], bl.IN_PROGRESS)

    def test_done_without_proof(self):
        card = self.add()
        self.to_progress(card["id"])
        with self.assertRaises(bl.ValidationError) as ctx:
            self.move(card["id"], "Выполнена", AGENT, accept_how=HOW)
        self.assertIn("ОШИБКА  proof:", str(ctx.exception))

    def test_done_without_accept_how_refused_for_any_acceptance(self):
        # Спека 2026-09-29, пункт 1: критерий обязателен у любой приёмки.
        for acceptance in ("owner", "reviewer"):
            with self.subTest(acceptance=acceptance):
                card = self.add(title=f"Приёмка {acceptance}", acceptance=acceptance)
                self.to_progress(card["id"])
                with self.assertRaises(bl.ValidationError) as ctx:
                    self.move(card["id"], "Выполнена", AGENT, proof=PROOF)
                self.assertIn("ОШИБКА  accept_how:", str(ctx.exception))
                self.assertEqual(bl.get_card(P, card["id"], boards_dir=self.d)["status"], bl.IN_PROGRESS)

    def test_done_accept_how_needs_arrow_format(self):
        card = self.add(title="Этап 8", kind="этап", stage=8)
        self.to_progress(card["id"])
        with self.assertRaises(bl.ValidationError) as ctx:
            self.move(card["id"], "Выполнена", AGENT, proof=PROOF, accept_how=HOW_NO_SEP)
        self.assertIn("что выполнить → что должно получиться", str(ctx.exception))
        got = self.move(card["id"], "Выполнена", AGENT, proof=PROOF, accept_how=HOW)
        self.assertEqual((got["status"], got["accept_how"]), (bl.DONE, HOW))
        # и владелец на странице тоже: критерий без разделителя не проходит
        other = self.add(title="Владелец сразу в Выполнена")
        with self.assertRaises(bl.ValidationError):
            self.move(other["id"], "Выполнена", OWNER, proof="видел", accept_how="посмотреть")

    def test_rework_without_comment(self):
        card = self.add()
        self.to_done(card["id"])
        with self.assertRaises(bl.ValidationError) as ctx:
            self.move(card["id"], "В работе", OWNER)
        self.assertIn("ОШИБКА  comment:", str(ctx.exception))
        self.assertEqual(bl.get_card(P, card["id"], boards_dir=self.d)["status"], bl.DONE)

    def test_rework_cleared_when_done_again(self):
        card = self.add()
        self.to_done(card["id"])
        self.move(card["id"], "В работе", OWNER, comment="не хватает тёмной темы")
        self.assertTrue(bl.get_card(P, card["id"], boards_dir=self.d)["rework"])
        got = self.move(card["id"], "Выполнена", AGENT, proof=PROOF, accept_how=HOW)
        self.assertEqual(got["rework"], "")

    def test_fresh_proof_required_after_rework(self):
        card = self.add()
        self.to_done(card["id"])
        self.move(card["id"], "В работе", OWNER, comment="тёмная тема не переключается")
        with self.assertRaises(bl.ValidationError) as ctx:  # старое доказательство на карточке есть
            self.move(card["id"], "Выполнена", AGENT)
        self.assertIn("свежее доказательство", str(ctx.exception))
        got = self.move(card["id"], "Выполнена", AGENT, proof="переключатель темы проверен заново")
        self.assertEqual(got["proof"], "переключатель темы проверен заново")
        self.assertEqual(got["rework"], "")
        # доработки не было — прежнего доказательства хватает
        self.move(card["id"], "Завершена", OWNER)

    def test_text_fields_must_be_strings(self):
        class Sneaky:
            def __str__(self):
                return "текст в обход проверки"

        card = self.add()
        self.to_done(card["id"])
        with self.assertRaises(bl.ValidationError) as ctx:
            self.move(card["id"], "Завершена", OWNER, caveat=Sneaky())
        self.assertIn("ОШИБКА  caveat: ожидалась строка", str(ctx.exception))
        with self.assertRaises(bl.ValidationError) as ctx:
            self.add(title=Sneaky())
        self.assertIn("ОШИБКА  title: ожидалась строка", str(ctx.exception))
        with self.assertRaises(bl.ValidationError) as ctx:
            bl.comment_card(P, card["id"], OWNER, Sneaky(), boards_dir=self.d)
        self.assertIn("ОШИБКА  comment: ожидалась строка", str(ctx.exception))
        with self.assertRaises(bl.ValidationError) as ctx:
            bl.handoff(P, card["id"], AGENT, session={"title": Sneaky(), "link": ""}, boards_dir=self.d)
        self.assertIn("ОШИБКА  session.title: ожидалась строка", str(ctx.exception))
        self.assertEqual(bl.get_card(P, card["id"], boards_dir=self.d)["status"], bl.DONE)

    def test_session_link_must_look_like_a_session(self):
        card = self.add()
        with self.assertRaises(bl.ValidationError) as ctx:
            bl.handoff(P, card["id"], AGENT, session={"link": "https://example.com/сессия", "title": "Доска"},
                       boards_dir=self.d)
        self.assertIn("ОШИБКА  session.link:", str(ctx.exception))
        got = bl.handoff(P, card["id"], AGENT, boards_dir=self.d,
                         session={"link": "claude://claude.ai/epitaxy/abc-123_XY", "title": "Доска"})
        self.assertEqual(got["session"]["link"], "claude://claude.ai/epitaxy/abc-123_XY")

    def test_caveat_only_on_accept(self):
        card = self.add()
        self.to_progress(card["id"])
        with self.assertRaises(bl.ValidationError) as ctx:
            self.move(card["id"], "Выполнена", OWNER, proof=PROOF, accept_how=HOW, caveat="почти")
        self.assertIn("ОШИБКА  caveat:", str(ctx.exception))
        self.assertEqual(bl.get_card(P, card["id"], boards_dir=self.d)["status"], bl.IN_PROGRESS)

    def test_agent_text_goes_through_textcheck(self):
        import textcheck

        self.assertTrue(hasattr(textcheck, "check_fields"))  # работаем с настоящим модулем
        with self.assertRaises(bl.ValidationError) as ctx:
            self.add(AGENT, title="Починить сборку в render_page.py")
        self.assertIn("ОШИБКА  title:", str(ctx.exception))

    def test_owner_text_is_not_checked(self):
        card = self.add(OWNER, title="Починить сборку в render_page.py")
        self.assertIn("render_page.py", card["title"])


# ============================================ приёмка проверяющим (спека 2026-09-29, пункты 3, 4, 9)


class TestVerify(Base):
    def done_card(self, title="Деплой", acceptance="reviewer", accept_how=HOW):
        card = self.add(AGENT, title=title, acceptance=acceptance)
        self.to_progress(card["id"])
        return self.move(card["id"], "Выполнена", AGENT, proof=PROOF, accept_how=accept_how)

    def verify(self, card_id, verdict, **kw):
        return bl.verify_card(P, card_id, verdict, boards_dir=self.d, **kw)

    def test_ok_accepts_and_writes_verified(self):
        card = self.done_card()
        got = self.verify(card["id"], bl.VERIFY_OK, command=COMMAND, output=OUTPUT)
        self.assertEqual(got["status"], bl.ACCEPTED)
        v = got["verified"]
        self.assertEqual((v["who"], v["command"], v["output"], v["verdict"], v["truncated"]),
                         (REVIEWER, COMMAND, OUTPUT.strip(), bl.VERIFY_OK, False))
        self.assertTrue(v["at"])
        self.assertEqual(got["history"][-1]["text"], "принято проверяющим")
        self.assertEqual(got["proof"], PROOF)  # доказательство автора не тронуто
        raw = json.loads(self.file_bytes().decode("utf-8"))
        self.assertIn("verified", list(raw["cards"][0]))
        self.assertLess(list(bl.CARD_FIELDS).index("caveat"), list(bl.CARD_FIELDS).index("verified"))

    def test_ok_refusals_write_nothing(self):
        card = self.done_card()
        before = self.file_bytes()
        cases = {
            "без вывода": dict(command=COMMAND, output=None),
            "пустой вывод": dict(command=COMMAND, output="   \n"),
            "без команды": dict(command=None, output=OUTPUT),
            "секрет Authorization": dict(command=COMMAND, output="HTTP/2 200\nAuthorization: Basic abc\n"),
            "секрет Bearer": dict(command=COMMAND, output="ok\nx: Bearer eyJhbGci\n"),
            "секрет token=": dict(command=COMMAND, output="https://a.b/?token=123\n"),
            "секрет password": dict(command=COMMAND, output='{"password": "x"}\n'),
            "секрет sk-": dict(command=COMMAND, output="key sk-abcdefghijklmnop\n"),
        }
        for name, kw in cases.items():
            with self.subTest(case=name), self.assertRaises(bl.ValidationError):
                self.verify(card["id"], bl.VERIFY_OK, **kw)
        self.assertEqual(before, self.file_bytes())
        self.assertIsNone(bl.get_card(P, card["id"], boards_dir=self.d)["verified"])

    def test_paths_and_hashes_allowed_in_output_and_command(self):
        card = self.done_card()
        output = "GET /api/state → 200\nfile X:/project/x/y.json\ncommit a1b2c3d4e5f6a7b8\nid vjeuupeztqkwuvkoiglrmmm1\n"
        got = self.verify(card["id"], bl.VERIFY_OK, command="curl -s http://localhost:8790/api/state | head -c 400",
                          output=output)
        self.assertEqual(got["verified"]["output"], output.strip())

    def test_output_is_truncated_with_mark(self):
        import textcheck
        card = self.done_card()
        got = self.verify(card["id"], bl.VERIFY_OK, command=COMMAND, output="a" * (textcheck.OUTPUT_LIMIT + 500))
        v = got["verified"]
        self.assertTrue(v["truncated"])
        self.assertTrue(v["output"].endswith(textcheck.OUTPUT_TRUNCATED_MARK))
        self.assertLessEqual(len(v["output"]), textcheck.OUTPUT_LIMIT + len(textcheck.OUTPUT_TRUNCATED_MARK) + 1)

    def test_ok_refused_when_not_done_or_owner_card(self):
        card = self.add(AGENT, title="Ещё в работе")
        self.to_progress(card["id"])
        with self.assertRaises(bl.ValidationError) as ctx:
            self.verify(card["id"], bl.VERIFY_OK, command=COMMAND, output=OUTPUT)
        self.assertIn("Выполнена", str(ctx.exception))
        owner_card = self.done_card(title="Дизайн", acceptance="owner")
        with self.assertRaises(bl.ForbiddenError) as ctx:
            self.verify(owner_card["id"], bl.VERIFY_OK, command=COMMAND, output=OUTPUT)
        self.assertIn("владелец", str(ctx.exception))
        self.assertEqual(bl.get_card(P, owner_card["id"], boards_dir=self.d)["status"], bl.DONE)

    def test_ok_refused_without_valid_criteria(self):
        card = self.done_card()
        self.wipe_field(card["id"], "accept_how")  # критерий стёрли руками в файле
        with self.assertRaises(bl.ValidationError) as ctx:
            self.verify(card["id"], bl.VERIFY_OK, command=COMMAND, output=OUTPUT)
        self.assertIn("need-criteria", str(ctx.exception))
        with self.assertRaises(bl.ValidationError):
            self.verify(card["id"], bl.VERIFY_FAIL, command=COMMAND, output=OUTPUT, comment="не то")

    def test_fail_returns_to_work_with_rework_and_needs_fresh_proof(self):
        card = self.done_card()
        with self.assertRaises(bl.ValidationError) as ctx:
            self.verify(card["id"], bl.VERIFY_FAIL, command=COMMAND, output=OUTPUT)
        self.assertIn("ОШИБКА  comment:", str(ctx.exception))
        got = self.verify(card["id"], bl.VERIFY_FAIL, command=COMMAND, output="HTTP/2 502\n",
                          comment="ответ 502, а не 200")
        self.assertEqual((got["status"], got["rework"]), (bl.IN_PROGRESS, "ответ 502, а не 200"))
        self.assertEqual(got["verified"]["verdict"], bl.VERIFY_FAIL)
        self.assertEqual(got["comments"][-1]["who"], REVIEWER)
        self.assertEqual(got["history"][-1]["who"], REVIEWER)
        with self.assertRaises(bl.ValidationError) as ctx:  # свежий proof обязателен
            self.move(card["id"], "Выполнена", AGENT)
        self.assertIn("свежее доказательство", str(ctx.exception))
        again = self.move(card["id"], "Выполнена", AGENT, proof="поправлено, ответ 200")
        self.assertEqual((again["status"], again["rework"]), (bl.DONE, ""))
        # повторная проверка перезаписывает verified, прежняя остаётся в истории и комментариях
        accepted = self.verify(card["id"], bl.VERIFY_OK, command=COMMAND, output=OUTPUT)
        self.assertEqual(accepted["verified"]["verdict"], bl.VERIFY_OK)
        self.assertEqual(len([c for c in accepted["comments"] if c["who"] == REVIEWER]), 1)

    def test_need_criteria_returns_with_standard_comment(self):
        card = self.done_card()
        got = self.verify(card["id"], bl.VERIFY_NEED_CRITERIA)
        self.assertEqual(got["status"], bl.IN_PROGRESS)
        self.assertEqual(got["rework"], bl.NEED_CRITERIA_COMMENT)
        self.assertIsNone(got["verified"])  # команда и вывод не требуются и не пишутся
        other = self.done_card(title="Внешний вид")
        got = self.verify(other["id"], bl.VERIFY_NEED_CRITERIA, comment="критерий для владельца, не для команды")
        self.assertTrue(got["rework"].startswith(bl.NEED_CRITERIA_COMMENT))
        self.assertIn("критерий для владельца", got["rework"])

    def test_reviewer_comment_goes_through_textcheck(self):
        card = self.done_card()
        with self.assertRaises(bl.ValidationError):
            self.verify(card["id"], bl.VERIFY_FAIL, command=COMMAND, output=OUTPUT,
                        comment="смотри X:/project/x/log.txt")

    def test_verify_checks_version(self):
        card = self.done_card()
        with self.assertRaises(bl.ConflictError):
            self.verify(card["id"], bl.VERIFY_OK, command=COMMAND, output=OUTPUT, version=card["version"] + 1)

    def test_owner_returns_agent_accepted_card(self):
        card = self.done_card()
        self.verify(card["id"], bl.VERIFY_OK, command=COMMAND, output=OUTPUT)
        got = self.move(card["id"], "В работе", OWNER, comment="вывод не совпадает с критерием")
        self.assertEqual((got["status"], got["rework"]), (bl.IN_PROGRESS, "вывод не совпадает с критерием"))
        self.assertIsNotNone(got["verified"])  # запись проверки остаётся, её видно
        self.assertIsNone(bl.accepted_by(got))

    def test_verify_queue_hides_proof(self):
        bl.create_project("other", "второй", "X:/project/other", boards_dir=self.d)
        one = self.done_card(title="Первая")
        self.done_card(title="Принимаю сам", acceptance="owner")
        self.add(AGENT, title="Ещё не сделана")
        no_crit = self.done_card(title="Без критерия")
        self.wipe_field(no_crit["id"], "accept_how")
        queue = bl.verify_queue(P, boards_dir=self.d)
        self.assertEqual([q["id"] for q in queue], [one["id"], no_crit["id"]])
        self.assertEqual([q["criteria_ok"] for q in queue], [True, False])
        for item in queue:
            self.assertNotIn("proof", item)
            self.assertEqual(set(item) >= {"project", "id", "title", "body", "accept_how"}, True)
        self.assertEqual(bl.verify_queue(None, boards_dir=self.d), queue)  # все проекты — те же две
        self.assertEqual(bl.verify_queue("other", boards_dir=self.d), [])

    def make_old_file(self):
        """Файл проекта старого кода: без отметки миграции — так migrate_acceptance есть что делать."""
        file = bl.project_path(P, self.d)
        data = json.loads(file.read_text(encoding="utf-8"))
        data.pop("acceptance_migrated", None)
        file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def test_new_project_is_already_migrated(self):
        # Проект, созданный новым кодом, мигрировать не нужно: отметка стоит с создания файла
        self.assertTrue(bl.read_project(P, boards_dir=self.d)["acceptance_migrated"])
        self.assertLess(bl.PROJECT_FIELDS.index("acceptance_migrated"), bl.PROJECT_FIELDS.index("cards"))
        self.add(AGENT, title="Письмо клиенту", acceptance="owner")
        before = self.file_bytes()
        self.assertIsNone(bl.migrate_acceptance(P, boards_dir=self.d))
        self.assertIsNone(bl.migrate_acceptance(P, dry_run=True, boards_dir=self.d))
        self.assertEqual(before, self.file_bytes())

    def test_migrate_is_once_per_project(self):
        # Верификатор 2026-09-29: повтор не снимает «принимаю сам», поставленное после миграции
        old = self.add(OWNER, title="Старая, владелец", acceptance="owner")
        self.make_old_file()
        self.assertEqual([c["id"] for c in bl.migrate_acceptance(P, boards_dir=self.d)], [old["id"]])
        self.assertTrue(bl.read_project(P, boards_dir=self.d)["acceptance_migrated"])
        # владелец вернул «принимаю сам», агент завёл карточку с owner
        bl.edit_card(P, old["id"], OWNER, acceptance="owner", boards_dir=self.d)
        by_agent = self.add(AGENT, title="Дизайн", acceptance="owner")
        before = self.file_bytes()
        self.assertIsNone(bl.migrate_acceptance(P, boards_dir=self.d))
        self.assertIsNone(bl.migrate_acceptance(P, dry_run=True, boards_dir=self.d))  # и dry-run — «уже выполнена»
        self.assertEqual(before, self.file_bytes())
        for cid in (old["id"], by_agent["id"]):
            self.assertEqual(bl.get_card(P, cid, boards_dir=self.d)["acceptance"], OWNER)

    def test_migrate_marks_project_even_without_changes(self):
        self.add(OWNER, title="Уже проверяющий")
        self.make_old_file()
        self.assertEqual(bl.migrate_acceptance(P, dry_run=True, boards_dir=self.d), [])
        self.assertFalse(bl.read_project(P, boards_dir=self.d)["acceptance_migrated"])  # dry-run отметку не ставит
        self.assertEqual(bl.migrate_acceptance(P, boards_dir=self.d), [])
        self.assertTrue(bl.read_project(P, boards_dir=self.d)["acceptance_migrated"])
        self.assertIsNone(bl.migrate_acceptance(P, boards_dir=self.d))

    def test_migrate_acceptance(self):
        open_owner = self.add(OWNER, title="Открытая, владелец", acceptance="owner")
        done_owner = self.done_card(title="Выполнена, владелец", acceptance="owner")
        already = self.add(OWNER, title="Уже проверяющий")
        accepted = self.done_card(title="Принята владельцем", acceptance="owner")
        self.move(accepted["id"], "Завершена", OWNER)
        archived = self.add(OWNER, title="Отложена", acceptance="owner")
        self.move(archived["id"], "Отложена", OWNER)
        self.make_old_file()
        before = self.file_bytes()

        dry = bl.migrate_acceptance(P, dry_run=True, boards_dir=self.d)
        self.assertEqual([d["id"] for d in dry], [open_owner["id"], done_owner["id"]])
        self.assertEqual(before, self.file_bytes())  # dry-run ничего не пишет

        changed = bl.migrate_acceptance(P, boards_dir=self.d)
        self.assertEqual([c["id"] for c in changed], [open_owner["id"], done_owner["id"]])
        for cid in (open_owner["id"], done_owner["id"]):
            card = bl.get_card(P, cid, boards_dir=self.d)
            self.assertEqual(card["acceptance"], REVIEWER)
            self.assertEqual((card["history"][-1]["who"], card["history"][-1]["text"]), (AGENT, bl.MIGRATION_NOTE))
        self.assertEqual(bl.get_card(P, already["id"], boards_dir=self.d)["history"][-1]["text"], "")
        raw_before = json.loads(before.decode("utf-8"))["cards"]
        raw_after = json.loads(self.file_bytes().decode("utf-8"))["cards"]
        for old, new in zip(raw_before, raw_after):
            if old["id"] in (accepted["id"], archived["id"]):
                self.assertEqual(old, new)  # принятые и архив не изменились
        self.assertIsNone(bl.migrate_acceptance(P, boards_dir=self.d))  # повтор — уже выполнена


# ================================================================ хранилище (1.4)


class TestStorage(Base):
    def test_project_file_shape(self):
        data = json.loads(self.file_bytes().decode("utf-8"))
        self.assertEqual(list(data), list(bl.PROJECT_FIELDS))
        self.assertEqual(data["project"], P)
        self.assertEqual(data["path"], "X:/project/demo")

    def test_json_is_readable_in_diff(self):
        self.add(title="Задача с русским текстом")
        text = self.file_bytes().decode("utf-8")
        self.assertIn("Задача с русским текстом", text)  # ensure_ascii=False
        self.assertIn('\n  "cards"', text)  # indent=2
        self.assertEqual(list(json.loads(text)["cards"][0]), list(bl.CARD_FIELDS))

    def test_stale_version_rejected_and_file_untouched(self):
        card = self.add()
        bl.comment_card(P, card["id"], OWNER, "первая правка", boards_dir=self.d)
        before = self.file_bytes()
        with self.assertRaises(bl.ConflictError):
            self.move(card["id"], "Новая", OWNER, version=card["version"])
        self.assertEqual(before, self.file_bytes())

    def test_version_grows_on_every_write(self):
        card = self.add()
        first = bl.get_card(P, card["id"], boards_dir=self.d)["version"]
        bl.comment_card(P, card["id"], OWNER, "раз", boards_dir=self.d)
        second = bl.get_card(P, card["id"], boards_dir=self.d)["version"]
        self.move(card["id"], "Новая", OWNER, version=second)
        third = bl.get_card(P, card["id"], boards_dir=self.d)["version"]
        self.assertEqual([second, third], [first + 1, first + 2])

    def test_history_records_who_from_to_and_time(self):
        card = self.add()
        got = self.move(card["id"], "Новая", AGENT)
        last = got["history"][-1]
        self.assertEqual((last["who"], last["from"], last["to"]), (AGENT, bl.BACKLOG, bl.NEW))
        self.assertIsNotNone(bl._parse_dt(last["at"]).tzinfo)

    def test_broken_replace_keeps_old_file_and_no_tmp(self):
        card = self.add()
        before = self.file_bytes()
        with mock.patch("boardlib.os.replace", side_effect=OSError("диск занят")):
            with self.assertRaises(bl.BoardError):
                bl.comment_card(P, card["id"], OWNER, "не должно записаться", boards_dir=self.d)
        self.assertEqual(before, self.file_bytes())
        self.assertEqual(list(Path(self.d).glob("*.tmp")), [])

    def test_broken_serialization_keeps_old_file_and_no_tmp(self):
        card = self.add()
        before = self.file_bytes()
        with mock.patch("boardlib.json.dumps", side_effect=RuntimeError("сериализация упала")):
            with self.assertRaises(RuntimeError):
                bl.comment_card(P, card["id"], OWNER, "не должно записаться", boards_dir=self.d)
        self.assertEqual(before, self.file_bytes())
        self.assertEqual(list(Path(self.d).glob("*.tmp")), [])

    def test_number_is_not_reused(self):
        one, two, three = self.add(title="Раз"), self.add(title="Два"), self.add(title="Три")
        self.assertEqual([one["id"], two["id"], three["id"]], ["demo-1", "demo-2", "demo-3"])
        bl.delete_card(P, two["id"], OWNER, boards_dir=self.d)
        self.move(three["id"], "Отменена", OWNER)
        self.assertEqual(self.add(title="Четыре")["id"], "demo-4")

    def test_agent_cannot_delete(self):
        card = self.add()
        with self.assertRaises(bl.ForbiddenError):
            bl.delete_card(P, card["id"], AGENT, boards_dir=self.d)

    def test_missing_project_and_card(self):
        with self.assertRaises(bl.NotFoundError):
            bl.read_project("no-such-project", boards_dir=self.d)
        with self.assertRaises(bl.NotFoundError):
            bl.get_card(P, "demo-99", boards_dir=self.d)

    def test_project_name_whitelist(self):
        for name in ("C:побег", "Z:x", "nul", "CON", "com1", "проект:поток", "../побег",
                     "board/../../x", "", " ", "-начало", "ПРОЕКТ", "a" * 65, 7):
            with self.subTest(name=name):
                with self.assertRaises(bl.ValidationError):
                    bl.project_path(name, self.d)
        self.assertTrue(str(bl.project_path("demo-app", self.d)).endswith("demo-app.json"))
        with self.assertRaises(bl.ValidationError):
            bl.create_project("C:побег", "мимо каталога", "", boards_dir=self.d)
        self.assertEqual(bl.list_projects(self.d), [P])

    def test_numbers_from_arguments_are_checked(self):
        card = self.add()
        with self.assertRaises(bl.ValidationError) as ctx:
            bl.reorder_card(P, card["id"], OWNER, order="в начало", boards_dir=self.d)
        self.assertIn("ОШИБКА  order:", str(ctx.exception))
        with self.assertRaises(bl.ValidationError) as ctx:
            self.move(card["id"], "Новая", OWNER, version="первая")
        self.assertIn("ОШИБКА  version:", str(ctx.exception))
        with self.assertRaises(bl.ValidationError) as ctx:
            self.add(title="Этап", kind="этап", stage="первый")
        self.assertIn("ОШИБКА  stage:", str(ctx.exception))
        with self.assertRaises(bl.ValidationError) as ctx:
            self.add(title="Задача", priority="сверхсрочно")
        self.assertIn("ОШИБКА  priority:", str(ctx.exception))
        with self.assertRaises(bl.ValidationError):
            bl.find_stage_card(P, "первый", boards_dir=self.d)

    def test_state_token_changes_after_write(self):
        first = bl.state_token(self.d)
        self.add(title="Новая задача")
        second = bl.state_token(self.d)
        self.assertNotEqual(first, second)
        self.assertEqual(second, bl.state_token(self.d))

    def test_list_projects_and_create_twice(self):
        bl.create_project("demo-app", "вторая доска", "X:/project/demo-app", boards_dir=self.d)
        self.assertEqual(bl.list_projects(self.d), ["demo", "demo-app"])
        with self.assertRaises(bl.BoardError):
            bl.create_project(P, "снова", "", boards_dir=self.d)

    def test_data_dir_from_env(self):
        with mock.patch.dict(os.environ, {"BOARD_DIR": self.d}):
            self.assertEqual(str(bl.data_dir()), self.d)
        self.assertEqual(str(bl.data_dir(self.d)), self.d)  # аргумент сильнее переменной

    def test_data_dir_order(self):
        # Настоящий ~/.claude и настоящие диски не трогаются: CLAUDE_CONFIG_DIR — временная папка,
        # кандидаты раскладки подменены.
        root = Path(self.d)
        cfg = root / "cfg"
        cfg.mkdir()
        settings = cfg / "settings.json"
        layout = root / "disk" / "project" / "llm-wiki" / "boards"
        clean = {k: v for k, v in os.environ.items() if k not in ("BOARD_DIR", "TATET_WIKI", "CLAUDE_CONFIG_DIR")}
        base = dict(clean, CLAUDE_CONFIG_DIR=str(cfg))

        def run(extra=None):
            with mock.patch.dict(os.environ, dict(base, **(extra or {})), clear=True), \
                    mock.patch.object(bl, "_layout_candidates", return_value=[layout]):
                return bl.data_dir()

        # ничего нет: ни переменных, ни settings.json, ни папки раскладки — понятная ошибка, не кэш плагина
        with self.assertRaises(bl.BoardError) as ctx:
            run()
        message = "\n".join(ctx.exception.messages)
        self.assertIn("ПОМИЛКА  дані дошки", message)
        self.assertIn("setup.py", message)
        self.assertIn(str(settings), message)
        self.assertFalse(hasattr(bl, "_REPO_ROOT"))  # запасного корня «две папки вверх» больше нет

        # раскладка: первая существующая папка project/llm-wiki/boards
        layout.mkdir(parents=True)
        self.assertEqual(run(), layout)

        # settings.json: битый файл пропускается, env.TATET_WIKI → /boards, env.BOARD_DIR сильнее
        settings.write_text("{не json", encoding="utf-8")
        self.assertEqual(run(), layout)
        wiki_s = root / "wiki-s"
        settings.write_text(json.dumps({"env": {"TATET_WIKI": str(wiki_s)}}), encoding="utf-8")
        self.assertEqual(run(), wiki_s / "boards")
        board_s = root / "boards-s"
        settings.write_text(
            json.dumps({"env": {"TATET_WIKI": str(wiki_s), "BOARD_DIR": str(board_s)}}), encoding="utf-8",
        )
        self.assertEqual(run(), board_s)

        # переменные процесса сильнее settings.json; BOARD_DIR сильнее TATET_WIKI
        wiki = root / "wiki"
        self.assertEqual(run({"TATET_WIKI": str(wiki)}), wiki / "boards")
        self.assertEqual(run({"TATET_WIKI": str(wiki), "BOARD_DIR": self.d}), Path(self.d))

    def test_claude_settings_path_default_and_layout_shape(self):
        clean = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
        with mock.patch.dict(os.environ, clean, clear=True):
            self.assertEqual(bl._claude_settings_path(), Path.home() / ".claude" / "settings.json")
        with mock.patch.dict(os.environ, dict(clean, CLAUDE_CONFIG_DIR=self.d), clear=True):
            self.assertEqual(bl._claude_settings_path(), Path(self.d) / "settings.json")
        candidates = bl._layout_candidates()  # только список путей, на диск не смотрит
        self.assertTrue(candidates)
        for path in candidates:
            self.assertEqual(path.parts[-3:], ("project", "llm-wiki", "boards"))

    def test_handoff_without_status_change(self):
        card = self.add()
        self.to_progress(card["id"])
        got = bl.handoff(
            P, card["id"], AGENT,
            stopped_at="дописан разбор статусов, остались тесты",
            resume="Продолжи доску: board/boardlib.py, задача 1.5",
            session={"link": "claude://claude.ai/epitaxy/2990aadd-f4b9", "title": "Доска, волна 1"},
            boards_dir=self.d,
        )
        self.assertEqual(got["status"], bl.IN_PROGRESS)
        self.assertEqual(got["session"]["title"], "Доска, волна 1")
        self.assertIn("board/boardlib.py", got["resume"])  # resume пропускает пути (allow_paths)

    def test_two_processes_write_at_once(self):
        worker = Path(self.d) / "worker.py"
        worker.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(BOARD)!r})\n"
            "import boardlib as bl\n"
            "project, folder, count, mark = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]\n"
            "for i in range(count):\n"
            "    bl.add_card(project, 'agent', title=mark + ' ' + str(i), boards_dir=folder)\n",
            encoding="utf-8",
        )
        count = 15
        procs = [
            subprocess.Popen([sys.executable, str(worker), P, self.d, str(count), mark],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for mark in ("первый", "второй")
        ]
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err.decode("utf-8", "replace"))
        data = json.loads(self.file_bytes().decode("utf-8"))  # файл цел и читается
        ids = [c["id"] for c in data["cards"]]
        self.assertEqual(len(ids), 2 * count)
        self.assertEqual(len(set(ids)), 2 * count)
        self.assertEqual(data["counter"], 2 * count)
        self.assertEqual(sorted(ids), sorted(f"demo-{n}" for n in range(1, 2 * count + 1)))


# ==================================================================== inbox (пункт 5)


class TestInbox(Base):
    def test_rework_shown_once(self):
        card = self.add(AGENT, title="Собрать колонки доски")
        bl.ack_inbox(P, boards_dir=self.d)  # агент прочитал всё, что было
        self.to_done(card["id"])
        self.move(card["id"], "В работе", OWNER, comment="на узком экране колонки едут")
        events = bl.read_inbox(P, boards_dir=self.d)
        self.assertEqual([e["kind"] for e in events], ["rework"])
        self.assertEqual(events[0]["text"], "на узком экране колонки едут")
        self.assertEqual(bl.read_inbox(P, boards_dir=self.d), events)  # чтение не сдвигает курсор
        bl.ack_inbox(P, boards_dir=self.d)
        self.assertEqual(bl.read_inbox(P, boards_dir=self.d), [])

    def test_inbox_collects_owner_actions_only(self):
        card = self.add(AGENT, title="Собрать сводку")
        bl.ack_inbox(P, boards_dir=self.d)
        bl.comment_card(P, card["id"], AGENT, "начал", boards_dir=self.d)
        bl.comment_card(P, card["id"], OWNER, "посмотри ещё архив", boards_dir=self.d)
        own = self.add(OWNER, title="Проверить тёмную тему")
        self.to_progress(card["id"])
        self.move(card["id"], "На согласовании", AGENT, ask="брать ли архив в сводку")
        self.move(card["id"], "В работе", OWNER)
        kinds = [e["kind"] for e in bl.read_inbox(P, boards_dir=self.d)]
        self.assertEqual(kinds, ["comment", "new_card", "answer"])
        self.assertEqual(bl.read_inbox(P, boards_dir=self.d)[1]["card_id"], own["id"])

    def test_inbox_shows_acceptance_and_archive(self):
        card = self.add()
        self.to_done(card["id"])
        bl.ack_inbox(P, boards_dir=self.d)
        self.move(card["id"], "Завершена", OWNER, caveat="узкий экран потом")
        other = self.add(title="Отложить до осени")
        self.move(other["id"], "Отложена", OWNER)
        kinds = [e["kind"] for e in bl.read_inbox(P, boards_dir=self.d)]
        self.assertEqual(kinds, ["accepted", "new_card", "archived"])


# ============================================ путь проекта: модель и права (план 2026-09-24, A1–A6)


class TestProjectPathModel(Base):
    """A1: поля файла, умолчания, порядок ключей, старый файл без новых полей."""

    def test_new_fields_have_defaults_and_order(self):
        data = bl.read_project(P, boards_dir=self.d)
        self.assertEqual((data["start"], data["finish"], data["sprints"], data["fog"], data["progress_url"]),
                         (None, None, [], [], ""))
        raw = json.loads(self.file_bytes().decode("utf-8"))
        self.assertEqual(list(raw), list(bl.PROJECT_FIELDS))
        self.assertLess(bl.PROJECT_FIELDS.index("start"), bl.PROJECT_FIELDS.index("cards"))
        self.assertEqual(bl.PROJECT_FIELDS[-1], "cards")
        self.assertEqual(bl.CARD_FIELDS[bl.CARD_FIELDS.index("order") + 1], "sprint")

    def test_old_file_without_path_fields_reads_and_rewrites(self):
        file = bl.project_path(P, self.d)
        raw = json.loads(file.read_text(encoding="utf-8"))
        for key in ("start", "finish", "sprints", "fog", "progress_url"):
            raw.pop(key)
        raw["cards"] = [{"id": "demo-1", "title": "Старая карточка"}]  # карточка без поля sprint
        file.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        data = bl.read_project(P, boards_dir=self.d)
        self.assertEqual(data["sprints"], [])
        self.assertIsNone(data["cards"][0]["sprint"])
        bl.set_fog(P, AGENT, ["виджет для телефона"], boards_dir=self.d)  # любая запись переписывает ключи
        raw = json.loads(file.read_text(encoding="utf-8"))
        self.assertEqual(list(raw), list(bl.PROJECT_FIELDS))
        self.assertEqual(list(raw["cards"][0]), list(bl.CARD_FIELDS))

    def test_sprint_constants(self):
        self.assertEqual((bl.SPRINT_NEXT, bl.SPRINT_CURRENT, bl.SPRINT_CLOSING, bl.SPRINT_PASSED),
                         ("next", "current", "closing", "passed"))
        self.assertEqual(bl.FOG_MAX, 5)
        self.assertEqual(bl.parse_role("владелец в чате"), OWNER_CHAT)
        self.assertEqual(bl.role_title(OWNER_CHAT), "владелец в чате")

    def test_sprint_file_shape(self):
        bl.add_sprint(P, AGENT, "Волна 1", "ядро на проде", boards_dir=self.d)
        raw = json.loads(self.file_bytes().decode("utf-8"))
        self.assertEqual(list(raw["sprints"][0]), list(bl.SPRINT_FIELDS))
        self.assertEqual(raw["sprints"][0]["id"], "s1")
        entry = raw["sprints"][0]["history"][0]
        self.assertEqual((entry["who"], entry["from"], entry["to"], entry["said"]), (AGENT, None, "next", ""))


class TestStartAndFinish(Base):
    """A3: старт и финиш; роль owner_chat с обязательной цитатой."""

    def test_agent_sets_start_once(self):
        got = bl.set_start(P, AGENT, "2026-09-19", "спека утверждена", boards_dir=self.d)
        self.assertEqual(got, {"date": "2026-09-19", "event": "спека утверждена"})
        with self.assertRaises(bl.ForbiddenError) as ctx:
            bl.set_start(P, AGENT, "2026-09-20", "передумал", boards_dir=self.d)
        self.assertIn("владелец", str(ctx.exception))
        self.assertEqual(bl.read_project(P, boards_dir=self.d)["start"]["date"], "2026-09-19")

    def test_owner_chat_changes_start_only_with_quote(self):
        bl.set_start(P, AGENT, "2026-09-19", "спека утверждена", boards_dir=self.d)
        with self.assertRaises(bl.ValidationError) as ctx:
            bl.set_start(P, OWNER_CHAT, "2026-09-10", "первый коммит", boards_dir=self.d)
        self.assertIn(bl.NEED_OWNER_SAID, str(ctx.exception))
        got = bl.set_start(P, OWNER_CHAT, "2026-09-10", "первый коммит", said="старт считай с первого коммита",
                           boards_dir=self.d)
        self.assertEqual(got["date"], "2026-09-10")
        # владелец на странице — без цитаты
        self.assertEqual(bl.set_start(P, OWNER, "2026-09-11", "ещё раз", boards_dir=self.d)["date"], "2026-09-11")

    def test_start_date_is_checked(self):
        for bad in ("19.09.2026", "2026-13-01", "", None):
            with self.subTest(date=bad), self.assertRaises(bl.ValidationError):
                bl.set_start(P, AGENT, bad, "спека утверждена", boards_dir=self.d)
        with self.assertRaises(bl.ValidationError) as ctx:
            bl.set_start(P, AGENT, "2026-09-19", "", boards_dir=self.d)
        self.assertIn("ОШИБКА  start_event:", str(ctx.exception))
        self.assertIsNone(bl.read_project(P, boards_dir=self.d)["start"])

    def test_agent_proposes_but_cannot_approve_finish(self):
        got = bl.propose_finish(P, AGENT, "легаси выключен", boards_dir=self.d)
        self.assertEqual((got["text"], got["approved"], got["approved_at"]), ("легаси выключен", False, None))
        with self.assertRaises(bl.ForbiddenError) as ctx:
            bl.approve_finish(P, AGENT, said="ок", boards_dir=self.d)  # даже с подставленной цитатой
        self.assertIn("владелец", str(ctx.exception))
        with self.assertRaises(bl.ForbiddenError):
            bl.approve_finish(P, REVIEWER, boards_dir=self.d)
        self.assertFalse(bl.read_project(P, boards_dir=self.d)["finish"]["approved"])

    def test_owner_chat_without_quote_refused(self):
        bl.propose_finish(P, AGENT, "легаси выключен", boards_dir=self.d)
        for said in (None, "", "   "):
            with self.subTest(said=said), self.assertRaises(bl.ValidationError) as ctx:
                bl.approve_finish(P, OWNER_CHAT, said=said, boards_dir=self.d)
            self.assertIn(bl.NEED_OWNER_SAID, str(ctx.exception))
        self.assertFalse(bl.read_project(P, boards_dir=self.d)["finish"]["approved"])

    def test_owner_chat_approves_with_quote(self):
        bl.propose_finish(P, AGENT, "легаси выключен", boards_dir=self.d)
        got = bl.approve_finish(P, OWNER_CHAT, said=SAID, boards_dir=self.d)
        self.assertTrue(got["approved"])
        self.assertTrue(got["approved_at"])
        last = got["history"][-1]
        self.assertEqual((last["who"], last["to"], last["said"]), (OWNER_CHAT, "approved", SAID))
        # агент не меняет утверждённый финиш
        with self.assertRaises(bl.ForbiddenError) as ctx:
            bl.propose_finish(P, AGENT, "легаси удалён", boards_dir=self.d)
        self.assertIn("утверждён", str(ctx.exception))
        self.assertEqual(bl.read_project(P, boards_dir=self.d)["finish"]["text"], "легаси выключен")
        # повторное утверждение без новой формулировки — нечего утверждать
        with self.assertRaises(bl.ValidationError):
            bl.approve_finish(P, OWNER_CHAT, said="ещё раз ок", boards_dir=self.d)
        # новая формулировка словом владельца
        got = bl.approve_finish(P, OWNER_CHAT, said="финиш — легаси удалён", text="легаси удалён", boards_dir=self.d)
        self.assertEqual((got["text"], got["approved"]), ("легаси удалён", True))

    def test_owner_on_page_approves_without_quote(self):
        bl.propose_finish(P, AGENT, "легаси выключен", boards_dir=self.d)
        got = bl.approve_finish(P, OWNER, boards_dir=self.d)
        self.assertEqual((got["approved"], got["history"][-1]["who"]), (True, OWNER))

    def test_approve_without_proposal_needs_text(self):
        with self.assertRaises(bl.ValidationError):
            bl.approve_finish(P, OWNER_CHAT, said=SAID, boards_dir=self.d)
        got = bl.approve_finish(P, OWNER_CHAT, said=SAID, text="легаси выключен", boards_dir=self.d)
        self.assertEqual((got["text"], got["approved"]), ("легаси выключен", True))

    def test_quote_goes_through_textcheck(self):
        bl.propose_finish(P, AGENT, "легаси выключен", boards_dir=self.d)
        with self.assertRaises(bl.ValidationError) as ctx:
            bl.approve_finish(P, OWNER_CHAT, said="о" * 301, boards_dir=self.d)
        self.assertIn("ОШИБКА  owner_said:", str(ctx.exception))
        with self.assertRaises(bl.ValidationError) as ctx:
            bl.approve_finish(P, OWNER_CHAT, said="ок, коммит a1b2c3d4e5f6a7b8 годится", boards_dir=self.d)
        self.assertIn("хеш коммита", str(ctx.exception))
        with self.assertRaises(bl.ValidationError):  # объект с __str__ — не строка
            bl.approve_finish(P, OWNER_CHAT, said=object(), boards_dir=self.d)

    def test_agent_text_of_finish_and_start_is_checked(self):
        with self.assertRaises(bl.ValidationError) as ctx:
            bl.propose_finish(P, AGENT, "выключен X:/project/legacy", boards_dir=self.d)
        self.assertIn("ОШИБКА  finish:", str(ctx.exception))
        with self.assertRaises(bl.ValidationError) as ctx:
            bl.propose_finish(P, AGENT, "ф" * 121, boards_dir=self.d)
        self.assertIn("предел 120", str(ctx.exception))
        with self.assertRaises(bl.ValidationError):
            bl.set_start(P, AGENT, "2026-09-19", "с" * 81, boards_dir=self.d)


class TestSprints(Base):
    """A4: спринты — статусы, история с цитатой, права ролей."""

    def sprint(self, title="Волна 1", done_when="ядро на проде", role=AGENT):
        return bl.add_sprint(P, role, title, done_when, boards_dir=self.d)

    def test_add_sprint_numbers_in_order(self):
        one, two = self.sprint("Волна 1"), self.sprint("Волна 2", role=OWNER_CHAT)
        self.assertEqual((one["id"], two["id"], one["status"]), ("s1", "s2", bl.SPRINT_NEXT))
        self.assertEqual([s["id"] for s in bl.list_sprints(P, boards_dir=self.d)], ["s1", "s2"])
        self.assertEqual(bl.get_sprint(P, "s2", boards_dir=self.d)["title"], "Волна 2")
        self.assertIsNone(bl.current_sprint(P, boards_dir=self.d))
        with self.assertRaises(bl.NotFoundError):
            bl.get_sprint(P, "s3", boards_dir=self.d)
        with self.assertRaises(bl.ValidationError):
            bl.get_sprint(P, "sprint-1", boards_dir=self.d)

    def test_add_sprint_requires_title_and_done_when(self):
        with self.assertRaises(bl.ValidationError) as ctx:
            self.sprint(done_when="")
        self.assertIn("ОШИБКА  done_when:", str(ctx.exception))
        with self.assertRaises(bl.ValidationError) as ctx:
            self.sprint(title="Починить render_page.py")
        self.assertIn("ОШИБКА  sprint_title:", str(ctx.exception))
        with self.assertRaises(bl.ValidationError):
            self.sprint(title="в" * 81)
        self.assertEqual(bl.list_sprints(P, boards_dir=self.d), [])

    def test_agent_cannot_open_sprint(self):
        self.sprint()
        with self.assertRaises(bl.ForbiddenError) as ctx:
            bl.open_sprint(P, AGENT, "s1", boards_dir=self.d)
        self.assertIn("владелец", str(ctx.exception))
        with self.assertRaises(bl.ForbiddenError):  # подставленная цитата агенту не помогает
            bl.open_sprint(P, AGENT, "s1", said=SAID, boards_dir=self.d)
        self.assertEqual(bl.get_sprint(P, "s1", boards_dir=self.d)["status"], bl.SPRINT_NEXT)

    def test_owner_chat_opens_only_with_quote(self):
        self.sprint()
        with self.assertRaises(bl.ValidationError) as ctx:
            bl.open_sprint(P, OWNER_CHAT, "s1", boards_dir=self.d)
        self.assertIn(bl.NEED_OWNER_SAID, str(ctx.exception))
        got = bl.open_sprint(P, OWNER_CHAT, "s1", said="открывай волну 1", boards_dir=self.d)
        self.assertEqual(got["status"], bl.SPRINT_CURRENT)
        self.assertTrue(got["opened_at"])
        last = got["history"][-1]
        self.assertEqual((last["who"], last["from"], last["to"], last["said"]),
                         (OWNER_CHAT, "next", "current", "открывай волну 1"))
        self.assertEqual(bl.current_sprint(P, boards_dir=self.d)["id"], "s1")

    def test_second_current_sprint_refused(self):
        self.sprint("Волна 1"), self.sprint("Волна 2")
        bl.open_sprint(P, OWNER_CHAT, "s1", said=SAID, boards_dir=self.d)
        with self.assertRaises(bl.ValidationError) as ctx:
            bl.open_sprint(P, OWNER_CHAT, "s2", said=SAID, boards_dir=self.d)
        self.assertIn("текущий спринт уже есть", str(ctx.exception))
        bl.ready_sprint(P, AGENT, "s1", proof=PROOF, boards_dir=self.d)  # «ждёт закрытия» — тоже текущий
        with self.assertRaises(bl.ValidationError):
            bl.open_sprint(P, OWNER, "s2", boards_dir=self.d)
        with self.assertRaises(bl.ValidationError):  # открыть можно только «следующий»
            bl.open_sprint(P, OWNER, "s1", boards_dir=self.d)
        self.assertEqual(bl.get_sprint(P, "s2", boards_dir=self.d)["status"], bl.SPRINT_NEXT)

    def test_ready_requires_proof_for_agent(self):
        self.sprint()
        bl.open_sprint(P, OWNER_CHAT, "s1", said=SAID, boards_dir=self.d)
        with self.assertRaises(bl.ValidationError) as ctx:
            bl.ready_sprint(P, AGENT, "s1", boards_dir=self.d)
        self.assertIn("ОШИБКА  sprint_proof:", str(ctx.exception))
        with self.assertRaises(bl.ValidationError):
            bl.ready_sprint(P, AGENT, "s1", proof="см. коммит a1b2c3d4e5f6a7b8", boards_dir=self.d)
        self.assertEqual(bl.get_sprint(P, "s1", boards_dir=self.d)["status"], bl.SPRINT_CURRENT)
        got = bl.ready_sprint(P, AGENT, "s1", proof=PROOF, boards_dir=self.d)
        self.assertEqual((got["status"], got["proof"], got["history"][-1]["text"]), (bl.SPRINT_CLOSING, PROOF, PROOF))
        with self.assertRaises(bl.ValidationError):  # второй раз — уже не текущий
            bl.ready_sprint(P, AGENT, "s1", proof=PROOF, boards_dir=self.d)

    def test_ready_only_from_current(self):
        self.sprint()
        with self.assertRaises(bl.ValidationError) as ctx:
            bl.ready_sprint(P, AGENT, "s1", proof=PROOF, boards_dir=self.d)
        self.assertIn("только текущий", str(ctx.exception))

    def test_agent_cannot_close_sprint(self):
        self.sprint()
        bl.open_sprint(P, OWNER_CHAT, "s1", said=SAID, boards_dir=self.d)
        bl.ready_sprint(P, AGENT, "s1", proof=PROOF, boards_dir=self.d)
        with self.assertRaises(bl.ForbiddenError) as ctx:
            bl.close_sprint(P, AGENT, "s1", boards_dir=self.d)
        self.assertIn("владелец", str(ctx.exception))
        with self.assertRaises(bl.ForbiddenError):
            bl.close_sprint(P, REVIEWER, "s1", said=SAID, boards_dir=self.d)
        with self.assertRaises(bl.ValidationError) as ctx:
            bl.close_sprint(P, OWNER_CHAT, "s1", boards_dir=self.d)
        self.assertIn(bl.NEED_OWNER_SAID, str(ctx.exception))
        self.assertEqual(bl.get_sprint(P, "s1", boards_dir=self.d)["status"], bl.SPRINT_CLOSING)

    def test_owner_closes_current_directly(self):
        self.sprint()
        bl.open_sprint(P, OWNER, "s1", boards_dir=self.d)
        got = bl.close_sprint(P, OWNER, "s1", boards_dir=self.d)
        self.assertEqual((got["status"], got["history"][-1]["from"]), (bl.SPRINT_PASSED, "current"))
        self.assertTrue(got["closed_at"])
        with self.assertRaises(bl.ValidationError):  # пройденный не закрывается второй раз
            bl.close_sprint(P, OWNER, "s1", boards_dir=self.d)
        self.sprint("Волна 2")
        with self.assertRaises(bl.ValidationError):  # «следующий» закрыть нельзя
            bl.close_sprint(P, OWNER, "s2", boards_dir=self.d)

    def test_full_cycle_agent_and_owner_chat(self):
        one = self.sprint("Волна 1", "ядро на проде")
        two = self.sprint("Волна 2", "виджеты на проде")
        bl.open_sprint(P, OWNER_CHAT, one["id"], said="открывай первую", boards_dir=self.d)
        bl.ready_sprint(P, AGENT, one["id"], proof=PROOF, boards_dir=self.d)
        closed = bl.close_sprint(P, OWNER_CHAT, one["id"], said="закрывай, принято", boards_dir=self.d)
        self.assertEqual(closed["status"], bl.SPRINT_PASSED)
        opened = bl.open_sprint(P, OWNER_CHAT, two["id"], said="и вторую открывай", boards_dir=self.d)
        self.assertEqual(opened["status"], bl.SPRINT_CURRENT)
        self.assertEqual([s["status"] for s in bl.list_sprints(P, boards_dir=self.d)], ["passed", "current"])
        history = bl.get_sprint(P, one["id"], boards_dir=self.d)["history"]
        self.assertEqual([(h["who"], h["to"], h["said"]) for h in history], [
            (AGENT, "next", ""), (OWNER_CHAT, "current", "открывай первую"),
            (AGENT, "closing", ""), (OWNER_CHAT, "passed", "закрывай, принято"),
        ])
        seqs = [h["seq"] for s in bl.list_sprints(P, boards_dir=self.d) for h in s["history"]]
        self.assertEqual(len(seqs), len(set(seqs)))  # общий счётчик событий
        self.assertEqual(bl.read_project(P, boards_dir=self.d)["event_counter"], max(seqs))

    def test_sprints_are_not_deleted(self):
        self.assertFalse(hasattr(bl, "delete_sprint"))


class TestFogAndCardSprint(Base):
    """A5: туман и привязка карточек к спринту."""

    def test_fog_up_to_five_lines(self):
        lines = [f"видно {i}" for i in range(1, 6)]
        self.assertEqual(bl.set_fog(P, AGENT, lines, boards_dir=self.d), lines)
        with self.assertRaises(bl.ValidationError) as ctx:
            bl.set_fog(P, AGENT, lines + ["шестая"], boards_dir=self.d)
        self.assertIn("предел 5", str(ctx.exception))
        self.assertEqual(bl.read_project(P, boards_dir=self.d)["fog"], lines)
        self.assertEqual(bl.set_fog(P, AGENT, [], boards_dir=self.d), [])

    def test_fog_lines_are_checked(self):
        with self.assertRaises(bl.ValidationError):
            bl.set_fog(P, AGENT, "одна строка, а не список", boards_dir=self.d)
        with self.assertRaises(bl.ValidationError):
            bl.set_fog(P, AGENT, ["видно", ""], boards_dir=self.d)
        with self.assertRaises(bl.ValidationError) as ctx:
            bl.set_fog(P, AGENT, ["т" * 121], boards_dir=self.d)
        self.assertIn("ОШИБКА  fog:", str(ctx.exception))
        with self.assertRaises(bl.ValidationError):
            bl.set_fog(P, AGENT, ["см. X:/project/demo"], boards_dir=self.d)
        self.assertEqual(bl.set_fog(P, OWNER, ["см. X:/project/demo"], boards_dir=self.d), ["см. X:/project/demo"])

    def test_agent_card_goes_to_current_sprint_by_default(self):
        self.assertIsNone(self.add(AGENT, title="До спринтов")["sprint"])
        bl.add_sprint(P, AGENT, "Волна 1", "ядро на проде", boards_dir=self.d)
        bl.open_sprint(P, OWNER_CHAT, "s1", said=SAID, boards_dir=self.d)
        self.assertEqual(self.add(AGENT, title="В текущий")["sprint"], "s1")
        self.assertIsNone(self.add(OWNER, title="Владелец без спринта")["sprint"])
        self.assertIsNone(self.add(AGENT, title="Явно без спринта", sprint=None)["sprint"])
        bl.add_sprint(P, AGENT, "Волна 2", "виджеты", boards_dir=self.d)
        self.assertEqual(self.add(AGENT, title="В следующий", sprint="s2")["sprint"], "s2")
        with self.assertRaises(bl.NotFoundError):
            self.add(AGENT, title="В несуществующий", sprint="s9")

    def test_set_card_sprint_checks_version_and_sprint(self):
        card = self.add(AGENT, title="Задача")
        bl.add_sprint(P, AGENT, "Волна 1", "ядро на проде", boards_dir=self.d)
        got = bl.set_card_sprint(P, card["id"], AGENT, "s1", version=card["version"], boards_dir=self.d)
        self.assertEqual((got["sprint"], got["version"]), ("s1", card["version"] + 1))
        with self.assertRaises(bl.ConflictError):
            bl.set_card_sprint(P, card["id"], AGENT, None, version=card["version"], boards_dir=self.d)
        with self.assertRaises(bl.NotFoundError):
            bl.set_card_sprint(P, card["id"], AGENT, "s7", boards_dir=self.d)
        self.assertIsNone(bl.set_card_sprint(P, card["id"], AGENT, None, boards_dir=self.d)["sprint"])

    def test_agent_cannot_change_sprint_of_accepted_card(self):
        bl.add_sprint(P, AGENT, "Волна 1", "ядро на проде", boards_dir=self.d)
        card = self.add(AGENT, title="Сделано")
        self.to_done(card["id"])
        with self.assertRaises(bl.ForbiddenError) as ctx:
            bl.set_card_sprint(P, card["id"], AGENT, "s1", boards_dir=self.d)
        self.assertIn("ждёт приёмки", str(ctx.exception))
        self.move(card["id"], "Завершена", OWNER)
        with self.assertRaises(bl.ForbiddenError):
            bl.set_card_sprint(P, card["id"], REVIEWER, "s1", boards_dir=self.d)
        with self.assertRaises(bl.ValidationError) as ctx:  # владелец в чате — с цитатой
            bl.set_card_sprint(P, card["id"], OWNER_CHAT, "s1", boards_dir=self.d)
        self.assertIn(bl.NEED_OWNER_SAID, str(ctx.exception))
        self.assertIsNone(bl.get_card(P, card["id"], boards_dir=self.d)["sprint"])
        got = bl.set_card_sprint(P, card["id"], OWNER_CHAT, "s1", said="это была волна 1", boards_dir=self.d)
        self.assertEqual(got["sprint"], "s1")
        self.assertIsNone(bl.set_card_sprint(P, card["id"], OWNER, None, boards_dir=self.d)["sprint"])

    def test_owner_chat_has_no_owner_powers_on_cards(self):
        """Владелец в чате — не владелец страницы: карточки он двигает как агент."""
        card = self.add(AGENT, title="Сделано")
        self.to_done(card["id"])
        with self.assertRaises(bl.ForbiddenError):
            self.move(card["id"], "Завершена", OWNER_CHAT)
        with self.assertRaises(bl.ForbiddenError):
            bl.delete_card(P, card["id"], OWNER_CHAT, boards_dir=self.d)
        with self.assertRaises(bl.ForbiddenError):
            bl.edit_card(P, card["id"], OWNER_CHAT, title="Другой", boards_dir=self.d)
        marked = self.add(OWNER, title="Принимаю сам", acceptance="owner")
        with self.assertRaises(bl.ForbiddenError):  # пометку владельца не снимает
            bl.edit_card(P, marked["id"], OWNER_CHAT, acceptance="reviewer", boards_dir=self.d)
        with self.assertRaises(bl.ValidationError):  # и текст его проходит textcheck
            self.add(OWNER_CHAT, title="Починить render_page.py")

    def test_progress_url(self):
        self.assertEqual(bl.set_progress_url(P, "https://claude.ai/artifact/abc", boards_dir=self.d),
                         "https://claude.ai/artifact/abc")
        self.assertEqual(bl.read_project(P, boards_dir=self.d)["progress_url"], "https://claude.ai/artifact/abc")
        with self.assertRaises(bl.ValidationError):
            bl.set_progress_url(P, "claude.ai/artifact/abc", boards_dir=self.d)
        self.assertEqual(bl.set_progress_url(P, "", boards_dir=self.d), "")


class TestPathInbox(Base):
    """A6: события спринтов и финиша в read_inbox."""

    def test_finish_and_sprint_events_with_said(self):
        bl.add_sprint(P, AGENT, "Волна 1", "ядро на проде", boards_dir=self.d)
        bl.propose_finish(P, AGENT, "легаси выключен", boards_dir=self.d)
        bl.ack_inbox(P, boards_dir=self.d)
        self.assertEqual(bl.read_inbox(P, boards_dir=self.d), [])  # предложения агента — не события владельца
        bl.approve_finish(P, OWNER_CHAT, said="финиш годится", boards_dir=self.d)
        bl.open_sprint(P, OWNER_CHAT, "s1", said="открывай", boards_dir=self.d)
        bl.ready_sprint(P, AGENT, "s1", proof=PROOF, boards_dir=self.d)
        bl.close_sprint(P, OWNER_CHAT, "s1", said="закрывай", boards_dir=self.d)
        events = bl.read_inbox(P, boards_dir=self.d)
        self.assertEqual([e["kind"] for e in events], ["finish_approved", "sprint_opened", "sprint_closed"])
        self.assertEqual([e["said"] for e in events], ["финиш годится", "открывай", "закрывай"])
        self.assertEqual((events[0]["title"], events[0]["card_id"], events[0]["text"]),
                         ("легаси выключен", None, "финиш годится"))
        self.assertEqual((events[1]["sprint_id"], events[1]["title"], events[1]["status"]), ("s1", "Волна 1", "passed"))
        bl.ack_inbox(P, boards_dir=self.d)
        self.assertEqual(bl.read_inbox(P, boards_dir=self.d), [])

    def test_path_events_sorted_with_card_events(self):
        card = self.add(AGENT, title="Задача")
        bl.add_sprint(P, AGENT, "Волна 1", "ядро на проде", boards_dir=self.d)
        bl.ack_inbox(P, boards_dir=self.d)
        bl.open_sprint(P, OWNER, "s1", boards_dir=self.d)
        bl.comment_card(P, card["id"], OWNER, "посмотрел", boards_dir=self.d)
        events = bl.read_inbox(P, boards_dir=self.d)
        self.assertEqual([e["kind"] for e in events], ["sprint_opened", "comment"])
        self.assertEqual(events[0]["said"], "")


# =================================================================== сводка (1.5)


class TestSummary(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = tmp.name
        self.now = at("2026-09-20 09:00")

    def build(self, moment, fn):
        with mock.patch.object(bl, "_now", return_value=moment):
            return fn()

    def test_summary_three_lists(self):
        bl.create_project(P, "доска", "X:/project/demo", boards_dir=self.d)
        bl.create_project("demo-app", "вторая", "X:/project/demo-app", boards_dir=self.d)

        # ждёт владельца: «На согласовании» и «Выполнена» с приёмкой владельца
        waiting_review = bl.add_card(P, OWNER, title="Спросить про домен", boards_dir=self.d)
        bl.move_card(P, waiting_review["id"], "Новая", AGENT, boards_dir=self.d)
        bl.move_card(P, waiting_review["id"], "В работе", AGENT, boards_dir=self.d)
        bl.move_card(P, waiting_review["id"], "На согласовании", AGENT, ask="какой домен", boards_dir=self.d)

        waiting_done = bl.add_card(P, OWNER, title="Показать страницу", acceptance="owner", boards_dir=self.d)
        bl.move_card(P, waiting_done["id"], "Новая", AGENT, boards_dir=self.d)
        bl.move_card(P, waiting_done["id"], "В работе", AGENT, boards_dir=self.d)
        bl.move_card(P, waiting_done["id"], "Выполнена", AGENT, proof=PROOF, accept_how=HOW, boards_dir=self.d)

        # «Выполнена» с приёмкой проверяющего — не в «Ждёт тебя», а счётчиком verify_pending
        # (спека 2026-09-29, пункт 13)
        stage = bl.add_card("demo-app", OWNER, title="Этап 1", kind="этап", stage=1, boards_dir=self.d)
        bl.move_card("demo-app", stage["id"], "Новая", AGENT, boards_dir=self.d)
        bl.move_card("demo-app", stage["id"], "В работе", AGENT, boards_dir=self.d)
        bl.move_card("demo-app", stage["id"], "Выполнена", AGENT, proof=PROOF, accept_how=HOW, boards_dir=self.d)

        out = bl.summary(self.d, now=self.now)
        waiting_ids = [c["id"] for c in out["waiting"]]
        self.assertEqual(sorted(waiting_ids), sorted([waiting_review["id"], waiting_done["id"]]))
        self.assertEqual(out["verify_pending"], 1)
        self.assertEqual(out["sessions_without_card"], [])

    def test_unfinished_marks_two_days(self):
        bl.create_project(P, "доска", "X:/project/demo", boards_dir=self.d)
        fresh = self.build(at("2026-09-19 20:00"), lambda: self._in_progress("Свежая"))
        old = self.build(at("2026-09-16 10:00"), lambda: self._in_progress("Залежалась"))

        out = bl.summary(self.d, now=self.now)
        by_id = {c["id"]: c for c in out["unfinished"]}
        self.assertFalse(by_id[fresh]["stale"])
        self.assertTrue(by_id[old]["stale"])
        self.assertGreater(by_id[old]["idle_days"], 2)
        self.assertEqual(out["unfinished"][0]["id"], old)  # самая старая первой

    def test_done_yesterday_on_the_day_border(self):
        bl.create_project(P, "доска", "X:/project/demo", boards_dir=self.d)
        late = self.build(at("2026-09-19 23:59"), lambda: self._done("Вчера поздно"))
        early = self.build(at("2026-09-20 00:01"), lambda: self._done("Сегодня рано"))
        before = self.build(at("2026-09-18 23:59"), lambda: self._done("Позавчера"))

        out = bl.summary(self.d, now=self.now)
        ids = [c["id"] for c in out["done_yesterday"]]
        self.assertIn(late, ids)
        self.assertNotIn(early, ids)
        self.assertNotIn(before, ids)
        self.assertEqual(out["done_yesterday"][0]["reached"], bl.DONE)

    def test_accepted_yesterday_counts_once(self):
        """Карточка, ставшая вчера и «Выполнена», и «Завершена», — одна строка с последним статусом."""
        bl.create_project(P, "доска", "X:/project/demo", boards_dir=self.d)
        card = self.build(at("2026-09-19 12:00"), lambda: self._done("Принята вчера"))
        self.build(
            at("2026-09-19 18:00"),
            lambda: bl.move_card(P, card, "Завершена", OWNER, boards_dir=self.d),
        )
        out = bl.summary(self.d, now=self.now)
        rows = [c for c in out["done_yesterday"] if c["id"] == card]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reached"], bl.ACCEPTED)

    def test_summary_reports_broken_project_file(self):
        bl.create_project(P, "доска", "X:/project/demo", boards_dir=self.d)
        bl.create_project("demo-app", "вторая", "X:/project/demo-app", boards_dir=self.d)
        good = self.build(at("2026-09-19 12:00"), lambda: self._in_progress("Живая карточка"))
        (Path(self.d) / "demo-app.json").write_text("{это не JSON", encoding="utf-8")

        out = bl.summary(self.d, now=self.now)
        self.assertEqual([b["project"] for b in out["broken"]], ["demo-app"])
        self.assertIn("demo-app.json", out["broken"][0]["error"])
        self.assertEqual([c["id"] for c in out["unfinished"]], [good])  # остальные проекты собраны

    def test_summary_on_empty_board(self):
        out = bl.summary(self.d, now=self.now)
        self.assertEqual(
            (out["waiting"], out["unfinished"], out["done_yesterday"],
             out["sessions_without_card"], out["broken"], out["verify_pending"]),
            ([], [], [], [], [], 0),
        )

    # --- помощники -------------------------------------------------------
    def _in_progress(self, title):
        card = bl.add_card(P, OWNER, title=title, boards_dir=self.d)
        bl.move_card(P, card["id"], "Новая", AGENT, boards_dir=self.d)
        bl.move_card(P, card["id"], "В работе", AGENT, boards_dir=self.d)
        return card["id"]

    def _done(self, title):
        card_id = self._in_progress(title)
        bl.move_card(P, card_id, "Выполнена", AGENT, proof=PROOF, accept_how=HOW, boards_dir=self.d)
        return card_id


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
