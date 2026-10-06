"""Проверки progress.py: чистая логика доски-прогресс-бара проекта (план, часть B, B1-B6).

Модуль без ввода-вывода, все данные — словари в памяти. Запуск из папки board:

    PYTHONIOENCODING=utf-8 python -m unittest discover -s tests -p "test_progress.py" -v
"""
import sys
import unittest
from datetime import date
from pathlib import Path

BOARD = Path(__file__).resolve().parents[1]
if str(BOARD) not in sys.path:
    sys.path.insert(0, str(BOARD))

import boardlib as bl  # noqa: E402
import progress  # noqa: E402

TODAY = date(2026, 9, 24)


def card(id_, status, sprint=None, priority="B", order=1, title=None, acceptance=None, history=None):
    """Минимальная карточка доски — только поля, которые читает progress.py."""
    data = {
        "id": id_,
        "title": title or f"Карточка {id_}",
        "status": status,
        "sprint": sprint,
        "priority": priority,
        "order": order,
    }
    if acceptance is not None:
        data["acceptance"] = acceptance
    if history is not None:
        data["history"] = history
    return data


def sprint(id_, title, status, done_when="", closed_at=""):
    return {
        "id": id_,
        "title": title,
        "done_when": done_when,
        "status": status,
        "closed_at": closed_at,
    }


def project(**over):
    data = {
        "project": "demo",
        "phrase": "перенос постинга",
        "start": {"date": "2026-09-19", "event": "спека утверждена"},
        "finish": {"text": "легаси выключен", "approved": False},
        "sprints": [],
        "fog": [],
        "cards": [],
    }
    data.update(over)
    return data


# ---------------------------------------------------------------- B1: column_of


class ColumnOfTestCase(unittest.TestCase):
    def test_todo_statuses(self):
        for status in (bl.BACKLOG, bl.NEW, bl.IN_PROGRESS):
            with self.subTest(status=status):
                self.assertEqual(progress.column_of(card("c1", status)), "todo")

    def test_waiting_statuses(self):
        for status in (bl.REVIEW, bl.DONE):
            with self.subTest(status=status):
                self.assertEqual(progress.column_of(card("c1", status)), "waiting")

    def test_done_status(self):
        self.assertEqual(progress.column_of(card("c1", bl.ACCEPTED)), "done")

    def test_archive_hidden(self):
        for status in (bl.POSTPONED, bl.CANCELLED):
            with self.subTest(status=status):
                self.assertIsNone(progress.column_of(card("c1", status)))

    def test_unknown_status_hidden(self):
        self.assertIsNone(progress.column_of(card("c1", "something-else")))

    def test_done_with_reviewer_acceptance_is_todo_waiting_for_check(self):
        # Спека 2026-09-29, пункт 14 / готово-когда 8
        reviewer_card = card("c1", bl.DONE, acceptance=bl.REVIEWER)
        owner_card = card("c2", bl.DONE, acceptance=bl.OWNER)
        self.assertEqual(progress.column_of(reviewer_card), "todo")
        self.assertEqual(progress.mark_of(reviewer_card), "ждёт проверки")
        self.assertEqual(progress.column_of(owner_card), "waiting")
        self.assertEqual(progress.mark_of(owner_card), "ждёт тебя")

    def test_accepted_by_reviewer_marked_agent(self):
        by_agent = card("c1", bl.ACCEPTED, history=[{"who": bl.REVIEWER, "to": bl.ACCEPTED, "at": "2026-09-29T10:00:00"}])
        by_owner = card("c2", bl.ACCEPTED, history=[{"who": bl.OWNER, "to": bl.ACCEPTED, "at": "2026-09-29T10:00:00"}])
        self.assertEqual(progress.mark_of(by_agent), "принял агент")
        self.assertEqual(progress.mark_of(by_owner), "готово")


# ---------------------------------------------------------------- B2: cards_of_sprint


class CardsOfSprintTestCase(unittest.TestCase):
    def test_attached_cards_included_regardless_of_current(self):
        cards = [card("c1", bl.NEW, sprint="s1")]
        self.assertEqual(progress.cards_of_sprint(cards, "s1", is_current=False), cards)

    def test_unattached_open_card_goes_to_current_only(self):
        cards = [card("c1", bl.NEW, sprint=None)]
        self.assertEqual(progress.cards_of_sprint(cards, "s1", is_current=True), cards)
        self.assertEqual(progress.cards_of_sprint(cards, "s1", is_current=False), [])

    def test_unattached_accepted_card_hidden_even_if_current(self):
        cards = [card("c1", bl.ACCEPTED, sprint=None)]
        self.assertEqual(progress.cards_of_sprint(cards, "s1", is_current=True), [])

    def test_archive_hidden(self):
        cards = [card("c1", bl.POSTPONED, sprint="s1")]
        self.assertEqual(progress.cards_of_sprint(cards, "s1", is_current=True), [])

    def test_other_sprint_card_not_included(self):
        cards = [card("c1", bl.NEW, sprint="s2")]
        self.assertEqual(progress.cards_of_sprint(cards, "s1", is_current=True), [])

    def test_virtual_sprint_none_collects_unattached_open_cards(self):
        cards = [
            card("c1", bl.NEW, sprint=None),
            card("c2", bl.ACCEPTED, sprint=None),
        ]
        self.assertEqual(progress.cards_of_sprint(cards, None, is_current=True), [cards[0]])


# ---------------------------------------------------------------- B3: counter


class CounterTestCase(unittest.TestCase):
    def test_no_sprints_is_one_of_one(self):
        self.assertEqual(progress.counter([]), {"n": 1, "m": 1})

    def test_three_passed_one_current_one_next(self):
        sprints = [
            sprint("s1", "Волна 1", progress.PASSED),
            sprint("s2", "Волна 2", progress.PASSED),
            sprint("s3", "Волна 3", progress.PASSED),
            sprint("s4", "Ключи", progress.CURRENT),
            sprint("s5", "Волна 4", progress.NEXT),
        ]
        self.assertEqual(progress.counter(sprints), {"n": 4, "m": 5})

    def test_closing_counts_as_current(self):
        sprints = [
            sprint("s1", "Волна 1", progress.PASSED),
            sprint("s2", "Ключи", progress.CLOSING),
        ]
        self.assertEqual(progress.counter(sprints), {"n": 2, "m": 2})

    def test_only_next_sprints_has_zero_current(self):
        sprints = [sprint("s1", "Волна 1", progress.NEXT)]
        self.assertEqual(progress.counter(sprints), {"n": 0, "m": 1})


# ---------------------------------------------------------------- B4: bar


class BarTestCase(unittest.TestCase):
    def test_no_sprints_virtual_segment_plus_fog_and_unapproved_finish(self):
        data = project()
        segments = progress.bar(data)
        self.assertEqual(segments[0], {"kind": progress.CURRENT, "title": progress.UNNAMED_SPRINT})
        self.assertEqual(segments[-2], {"kind": "fog", "title": "туман"})
        self.assertEqual(segments[-1], {"kind": "finish", "title": "финиш не утверждён"})

    def test_sprints_in_order_then_fog_then_approved_finish(self):
        data = project(
            sprints=[
                sprint("s1", "Волна 1", progress.PASSED),
                sprint("s2", "Ключи", progress.CURRENT),
                sprint("s3", "Волна 4", progress.NEXT),
            ],
            finish={"text": "легаси выключен", "approved": True},
        )
        segments = progress.bar(data)
        self.assertEqual(
            [s["kind"] for s in segments],
            [progress.PASSED, progress.CURRENT, progress.NEXT, "fog", "finish"],
        )
        self.assertEqual(segments[-1], {"kind": "finish", "title": "легаси выключен"})

    def test_fog_always_present(self):
        data = project(finish={"text": "", "approved": True})
        kinds = [s["kind"] for s in progress.bar(data)]
        self.assertIn("fog", kinds)


# ---------------------------------------------------------------- B5: need


class NeedTestCase(unittest.TestCase):
    def test_closing_sprint_first(self):
        sprints = [sprint("s1", "Ключи", progress.CLOSING)]
        data = project(sprints=sprints, finish={"text": "", "approved": True})
        items = progress.need(data, sprints, sprints[0], [])
        self.assertEqual(items[0], {"title": "Ключи", "why": "спринт ждёт закрытия"})

    def test_unapproved_finish_listed(self):
        data = project(finish={"text": "легаси выключен", "approved": False})
        items = progress.need(data, [], None, [])
        self.assertEqual(items[0], {"title": "легаси выключен", "why": "финиш ждёт подтверждения"})

    def test_no_finish_text_not_listed(self):
        data = project(finish={"text": "", "approved": False})
        items = progress.need(data, [], None, [])
        self.assertEqual(items, [])

    def test_next_sprint_listed(self):
        sprints = [sprint("s2", "Волна 4", progress.NEXT)]
        data = project(sprints=sprints, finish={"text": "", "approved": True})
        items = progress.need(data, sprints, None, [])
        self.assertEqual(items[0], {"title": "Волна 4", "why": "новый спринт ждёт открытия"})

    def test_next_sprint_not_listed_while_current_is_open(self):
        # B5/задача 1: пока спринт current или closing уже идёт, открытие следующего всё равно
        # отказал бы boardlib.open_sprint («сначала закрой его») — предлагать это владельцу рано.
        sprints = [sprint("s1", "Ключи", progress.CURRENT), sprint("s2", "Волна 4", progress.NEXT)]
        data = project(sprints=sprints, finish={"text": "", "approved": True})
        items = progress.need(data, sprints, sprints[0], [])
        self.assertEqual(items, [])

    def test_waiting_cards_sorted_by_priority(self):
        data = project(finish={"text": "", "approved": True})
        waiting = [
            card("c1", bl.REVIEW, priority="C", title="Потом"),
            card("c2", bl.DONE, priority="A", title="Сейчас"),
            card("c3", bl.REVIEW, priority="B", title="Следом"),
        ]
        items = progress.need(data, [], None, waiting)
        self.assertEqual([i["title"] for i in items], ["Сейчас", "Следом", "Потом"])

    def test_waiting_card_ask_shown_instead_of_column_title(self):
        # B5/задача 2: у карточки в «На согласовании» есть ask (пишет boardlib.move_card при
        # `board move … --ask`) — на странице он точнее, чем название колонки.
        data = project(finish={"text": "", "approved": True})
        waiting = [{**card("c1", bl.REVIEW, title="Ключ шлюза"), "ask": "какой ключ брать: тестовый или боевой"}]
        items = progress.need(data, [], None, waiting)
        self.assertEqual(items, [{"title": "Ключ шлюза", "why": "какой ключ брать: тестовый или боевой"}])

    def test_waiting_card_without_ask_falls_back_to_status_title(self):
        data = project(finish={"text": "", "approved": True})
        waiting = [card("c1", bl.DONE, title="Готова")]  # done: ask не заполняется
        items = progress.need(data, [], None, waiting)
        self.assertEqual(items, [{"title": "Готова", "why": "Выполнена"}])

    def test_full_order(self):
        sprints = [sprint("s1", "Ключи", progress.CLOSING), sprint("s2", "Волна 4", progress.NEXT)]
        data = project(sprints=sprints, finish={"text": "легаси выключен", "approved": False})
        waiting = [card("c1", bl.REVIEW, priority="A", title="Карточка")]
        items = progress.need(data, sprints, sprints[0], waiting)
        # current_sprint — «closing» (не None): текущий спринт ещё идёт, «новый спринт ждёт
        # открытия» в этом состоянии не предлагается (задача 1).
        self.assertEqual(
            [i["why"] for i in items],
            ["спринт ждёт закрытия", "финиш ждёт подтверждения", "На согласовании"],
        )


# ---------------------------------------------------------------- B6: check


class CheckTestCase(unittest.TestCase):
    def test_no_sprints_no_warnings(self):
        self.assertEqual(progress.check(project()), [])

    def test_two_current_sprints_is_error(self):
        data = project(sprints=[
            sprint("s1", "Волна 1", progress.CURRENT),
            sprint("s2", "Волна 2", progress.CLOSING),
        ])
        warnings = progress.check(data)
        self.assertTrue(any("ОШИБКА" in w and "sprints" in w for w in warnings))

    def test_unknown_sprint_id_on_card_is_error(self):
        data = project(
            sprints=[sprint("s1", "Волна 1", progress.CURRENT)],
            cards=[card("c1", bl.NEW, sprint="s99")],
        )
        warnings = progress.check(data)
        self.assertTrue(any("ОШИБКА" in w and "неизвестный спринт" in w for w in warnings))

    def test_fog_over_limit_is_error(self):
        data = project(
            sprints=[sprint("s1", "Волна 1", progress.CURRENT)],
            fog=[f"строка {i}" for i in range(progress.FOG_MAX + 1)],
        )
        warnings = progress.check(data)
        self.assertTrue(any("ОШИБКА" in w and "fog" in w for w in warnings))

    def test_only_next_sprints_is_soft_warning(self):
        data = project(sprints=[sprint("s1", "Волна 1", progress.NEXT)])
        warnings = progress.check(data)
        self.assertTrue(any("ЗАМЕЧАНИЕ" in w for w in warnings))

    def test_no_sprints_fog_over_limit_is_still_error(self):
        # Задача 3: предел тумана проверяется и у проекта без спринтов, не только у спринтового.
        data = project(fog=[f"строка {i}" for i in range(progress.FOG_MAX + 1)])
        warnings = progress.check(data)
        self.assertTrue(any("ОШИБКА" in w and "fog" in w for w in warnings))

    def test_no_sprints_card_with_sprint_id_is_error(self):
        # Задача 3: без спринтов известных id нет вовсе — любой sprint у карточки чужой.
        data = project(cards=[card("c1", bl.NEW, sprint="s1")])
        warnings = progress.check(data)
        self.assertTrue(any("ОШИБКА" in w and "неизвестный спринт" in w for w in warnings))

    def test_loose_cards_note_moved_from_board_py(self):
        # Задача 3: замечание «карточки без спринта» теперь считает сам progress.check, не board.py.
        data = project(
            sprints=[sprint("s1", "Волна 1", progress.CURRENT)],
            cards=[card("c1", bl.NEW, sprint=None)],
        )
        warnings = progress.check(data)
        self.assertTrue(any("ЗАМЕЧАНИЕ" in w and "cards" in w and "без спринта" in w for w in warnings))

    def test_loose_cards_note_absent_without_sprints(self):
        # Проект без спринтов: любая открытая карточка без спринта и так в единственном виртуальном.
        data = project(cards=[card("c1", bl.NEW, sprint=None)])
        warnings = progress.check(data)
        self.assertFalse(any("без спринта" in w for w in warnings))

    def test_valid_data_no_warnings(self):
        data = project(
            sprints=[
                sprint("s1", "Волна 1", progress.PASSED),
                sprint("s2", "Ключи", progress.CURRENT),
            ],
            cards=[card("c1", bl.NEW, sprint="s2")],
        )
        self.assertEqual(progress.check(data), [])


# ---------------------------------------------------------------- layout (сборка целиком)


class LayoutTestCase(unittest.TestCase):
    def test_project_without_sprints_is_virtual_current(self):
        # Решение 6 спеки: проект без спринтов — один виртуальный текущий спринт со всеми
        # ОТКРЫТЫМИ карточками; завершённая карточка без спринта по правилу B2 не показывается
        # (иначе архив завершённых задач бесконечно рос бы в «Готово» без разбивки на спринты).
        data = project(cards=[
            card("c1", bl.NEW),
            card("c2", bl.REVIEW),
            card("c3", bl.ACCEPTED),
        ])
        result = progress.layout(data, today=TODAY)
        self.assertEqual(result["current"]["title"], progress.UNNAMED_SPRINT)
        self.assertEqual(result["counter"], {"n": 1, "m": 1})
        self.assertEqual(result["warnings"], [])
        self.assertEqual(len(result["columns"]["todo"]), 1)
        self.assertEqual(len(result["columns"]["waiting"]), 1)
        self.assertEqual(len(result["columns"]["done"]), 0)
        self.assertEqual(result["updated"], "2026-09-24")

    def test_three_passed_one_current_one_next(self):
        sprints = [
            sprint("s1", "Волна 1", progress.PASSED, closed_at="2026-09-05"),
            sprint("s2", "Волна 2", progress.PASSED, closed_at="2026-09-10"),
            sprint("s3", "Волна 3", progress.PASSED, closed_at="2026-09-15"),
            sprint("s4", "Ключи и первый прогон", progress.CURRENT, done_when="прогон зелёный"),
            sprint("s5", "Волна 4", progress.NEXT),
        ]
        cards = [
            card("c1", bl.NEW, sprint="s4", title="Завести ключи"),
            card("c2", bl.IN_PROGRESS, sprint="s4", title="Первый прогон"),
            card("c3", bl.REVIEW, sprint="s4", title="Проверка владельцем", priority="A"),
            card("c4", bl.ACCEPTED, sprint="s4", title="Готовая задача"),
            card("c5", bl.ACCEPTED, sprint="s1", title="Задача прошлого спринта"),
        ]
        data = project(
            sprints=sprints,
            cards=cards,
            fog=["видно проксирование через второй сервер"],
            finish={"text": "легаси выключен", "approved": False},
        )
        result = progress.layout(data, today=TODAY)

        self.assertEqual(result["project"], "demo")
        self.assertEqual(result["counter"], {"n": 4, "m": 5})
        self.assertEqual(result["current"]["title"], "Ключи и первый прогон")
        self.assertEqual(result["current"]["done_when"], "прогон зелёный")
        self.assertEqual(len(result["past"]), 3)
        self.assertEqual(result["next"], "Волна 4")
        self.assertEqual(result["fog"], ["видно проксирование через второй сервер"])
        self.assertEqual(result["warnings"], [])
        self.assertEqual({c["id"] for c in result["columns"]["todo"]}, {"c1", "c2"})
        self.assertEqual({c["id"] for c in result["columns"]["waiting"]}, {"c3"})
        self.assertEqual({c["id"] for c in result["columns"]["done"]}, {"c4"})
        self.assertFalse(result["finish"]["approved"])
        self.assertEqual(result["need"][-1]["title"], "Проверка владельцем")

    def test_variant_b_fields_passed_upcoming_tasks(self):
        # Вид Б (решение владельца 2026-09-24, «дорога к финишу»): путь с условиями пройденных,
        # все следующие спринты и задачи «Сейчас» одним списком с пометкой.
        sprints = [
            sprint("s1", "Волна 1", progress.PASSED, done_when="ядро работает", closed_at="2026-09-19"),
            sprint("s2", "Ключи", progress.CURRENT, done_when="прогон зелёный"),
            sprint("s3", "Волна 4", progress.NEXT, done_when="каналы переведены"),
            sprint("s4", "Волна 5", progress.NEXT),
        ]
        cards = [
            card("c1", bl.REVIEW, sprint="s2", title="Ключ шлюза"),
            card("c2", bl.NEW, sprint="s2", title="Аккаунты каналов"),
            card("c3", bl.ACCEPTED, sprint="s2", title="Готовая"),
            card("c4", bl.CANCELLED, sprint="s2", title="Отменённая"),
        ]
        result = progress.layout(project(sprints=sprints, cards=cards), today=TODAY)
        self.assertEqual(result["passed"],
                         [{"title": "Волна 1", "done_when": "ядро работает", "closed_at": "2026-09-19"}])
        self.assertEqual([u["title"] for u in result["upcoming"]], ["Волна 4", "Волна 5"])
        self.assertEqual(result["upcoming"][0]["done_when"], "каналы переведены")
        marks = {t["id"]: t["mark"] for t in result["tasks"]}
        self.assertEqual(marks, {"c1": "ждёт тебя", "c2": "", "c3": "готово"})
        self.assertEqual({t["id"]: t["column"] for t in result["tasks"]},
                         {"c1": "waiting", "c2": "todo", "c3": "done"})
        # Старые ключи не тронуты.
        self.assertEqual(result["next"], "Волна 4")
        self.assertEqual(result["past"], [{"title": "Волна 1", "closed_at": "2026-09-19"}])

    def test_data_errors_surface_as_warnings_not_exceptions(self):
        data = project(
            sprints=[
                sprint("s1", "Волна 1", progress.CURRENT),
                sprint("s2", "Волна 2", progress.CLOSING),
            ],
            cards=[card("c1", bl.NEW, sprint="unknown")],
            fog=[f"строка {i}" for i in range(progress.FOG_MAX + 2)],
        )
        result = progress.layout(data, today=TODAY)
        self.assertGreaterEqual(len(result["warnings"]), 2)

    def test_today_default_when_omitted(self):
        result = progress.layout(project())
        self.assertEqual(result["updated"], date.today().isoformat())

    def test_layout_acceptance_columns_marks_and_agent_note(self):
        # Готово-когда 8: «Выполнена» с приёмкой reviewer — «Сделать» с пометкой «ждёт проверки»,
        # с приёмкой owner — «Ждёт тебя»; принятая проверяющим — «Готово» с пометкой «принял агент»;
        # строка «вернуть принятое агентом можно на доске» — только если агент принял за неделю.
        sprints = [sprint("s1", "Ключи", progress.CURRENT)]
        cards = [
            card("c1", bl.DONE, sprint="s1", acceptance=bl.REVIEWER, title="Ждёт агента"),
            card("c2", bl.DONE, sprint="s1", acceptance=bl.OWNER, title="Ждёт владельца"),
            card("c3", bl.ACCEPTED, sprint="s1", title="Принял агент",
                 history=[{"who": bl.REVIEWER, "to": bl.ACCEPTED, "at": "2026-09-22T10:00:00+03:00"}]),
            card("c4", bl.ACCEPTED, sprint="s1", title="Принял владелец",
                 history=[{"who": bl.OWNER, "to": bl.ACCEPTED, "at": "2026-09-22T10:00:00+03:00"}]),
        ]
        result = progress.layout(project(sprints=sprints, cards=cards, finish={"text": "", "approved": True}), today=TODAY)
        self.assertEqual({t["id"]: t["column"] for t in result["tasks"]},
                         {"c1": "todo", "c2": "waiting", "c3": "done", "c4": "done"})
        self.assertEqual({t["id"]: t["mark"] for t in result["tasks"]},
                         {"c1": "ждёт проверки", "c2": "ждёт тебя", "c3": "принял агент", "c4": "готово"})
        self.assertEqual([c["id"] for c in result["columns"]["waiting"]], ["c2"])
        self.assertEqual([i["title"] for i in result["need"]], ["Ждёт владельца", progress.AGENT_ACCEPTED_NOTE])
        # приёмка агентом старше недели — строки нет
        cards[2]["history"][0]["at"] = "2026-09-10T10:00:00+03:00"
        result = progress.layout(project(sprints=sprints, cards=cards, finish={"text": "", "approved": True}), today=TODAY)
        self.assertEqual([i["title"] for i in result["need"]], ["Ждёт владельца"])


if __name__ == "__main__":
    unittest.main()
