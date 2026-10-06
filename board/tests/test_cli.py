"""Тесты команд агента `board ...` (board.py).

Каждый тест работает во временной папке через переменную окружения BOARD_DIR — ни один тест не
трогает настоящий каталог данных доски и не читает файлов вне папки tests. Большинство команд
проверяются запуском в отдельном процессе (subprocess), чтобы заодно проверить код выхода; тест
устаревшей версии — по необходимости (см. комментарий там) в этом же процессе с подменой boardlib.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_BOARD_DIR = _HERE.parent
if str(_BOARD_DIR) not in sys.path:
    sys.path.insert(0, str(_BOARD_DIR))

import boardlib  # noqa: E402
import board  # noqa: E402

BOARD_PY = _BOARD_DIR / "board.py"
# Образец проекта с блоком «## Пайплайн» — лежит рядом с тестами, живые файлы проектов не читаются.
FIXTURE_AGENTS_MD = _HERE / "fixtures" / "AGENTS.md"
HOW = "curl -sI https://demo.example.com → 200 и заголовок server без версии"
COMMAND = "curl -sI https://demo.example.com"


def _clean_env(boards_dir, project_dir=None, session_id=None, board_project=None):
    env = os.environ.copy()
    for key in ("BOARD_PROJECT", "CLAUDE_CODE_HOST_SESSION_ID", "BOARD_DIR"):
        env.pop(key, None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["BOARD_DIR"] = str(boards_dir)
    if session_id is not None:
        env["CLAUDE_CODE_HOST_SESSION_ID"] = session_id
    if board_project is not None:
        env["BOARD_PROJECT"] = board_project
    return env


def run_cli(args, boards_dir, cwd=None, session_id=None, board_project=None):
    env = _clean_env(boards_dir, session_id=session_id, board_project=board_project)
    proc = subprocess.run(
        [sys.executable, str(BOARD_PY), *args],
        capture_output=True, text=True, encoding="utf-8",
        env=env, cwd=str(cwd) if cwd else str(boards_dir),
    )
    return proc


class BoardCliTestCase(unittest.TestCase):
    """Базовый набор: список, показ, создание, перенос, комментарий, handoff, inbox, версия."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.boards_dir = self.tmp / "boards"
        self.boards_dir.mkdir()
        self.project_dir = self.tmp / "proj"
        self.project_dir.mkdir()
        self.project = "demo"
        boardlib.create_project(self.project, "демо-проект", str(self.project_dir), boards_dir=self.boards_dir)

    def tearDown(self):
        self._tmp.cleanup()

    # ---------------------------------------------------------- вспомогательное

    def run_ok(self, args, **kw):
        proc = run_cli(args, self.boards_dir, **kw)
        self.assertEqual(proc.returncode, 0, msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        return proc

    def owner_answers(self, card_id):
        """Владелец ответил на «На согласовании»: На_согласовании → В_работе (диаграмма спеки).

        Агенту напрямую в «Выполнена» из «На согласовании» хода нет (AGENT_EDGES); чтобы довести
        карточку до «Выполнена» в тесте, отвечает владелец — это то, что в реальности делает
        страница, а не агент, поэтому вызывается boardlib напрямую ролью owner.
        """
        boardlib.move_card(
            self.project, card_id, to=boardlib.IN_PROGRESS, role=boardlib.OWNER,
            boards_dir=self.boards_dir,
        )

    def add_card(self, **kw):
        args = ["add", "--project", self.project, "--title", kw.pop("title", "Заголовок")]
        for key, val in kw.items():
            args += [f"--{key.replace('_', '-')}", str(val)]
        proc = self.run_ok(args)
        m = re.match(r"([\w-]+)\s", proc.stdout)
        self.assertIsNotNone(m, proc.stdout)
        return m.group(1)

    # ---------------------------------------------------------- list / show / add

    def test_add_creates_backlog_card(self):
        card_id = self.add_card(title="Первая задача")
        self.assertTrue(card_id.startswith(f"{self.project}-"))
        card = boardlib.get_card(self.project, card_id, boards_dir=self.boards_dir)
        self.assertEqual(card["status"], boardlib.BACKLOG)
        self.assertEqual(card["title"], "Первая задача")

    def test_add_requires_title_argparse_error(self):
        proc = run_cli(["add", "--project", self.project], self.boards_dir)
        self.assertEqual(proc.returncode, 2)

    def test_list_shows_added_card_in_its_column(self):
        card_id = self.add_card(title="Видимая карточка")
        proc = self.run_ok(["list", "--project", self.project])
        self.assertIn(card_id, proc.stdout)
        self.assertIn("Бэклог", proc.stdout)

    def test_list_all_projects(self):
        self.add_card(title="Карточка demo")
        other_dir = self.tmp / "proj2"
        other_dir.mkdir()
        boardlib.create_project("other", "другой проект", str(other_dir), boards_dir=self.boards_dir)
        boardlib.add_card("other", boardlib.AGENT, title="Карточка other", boards_dir=self.boards_dir)
        proc = self.run_ok(["list", "--all"])
        self.assertIn("== demo ==", proc.stdout)
        self.assertIn("== other ==", proc.stdout)

    def test_show_prints_full_card(self):
        card_id = self.add_card(title="Карточка для show", body="Тело карточки")
        proc = self.run_ok(["show", card_id])
        self.assertIn("Карточка для show", proc.stdout)
        self.assertIn("Тело карточки", proc.stdout)
        self.assertIn("version:", proc.stdout)

    def test_show_no_project_flag_needed(self):
        # id несёт имя проекта — show находит проект сам, без --project
        card_id = self.add_card(title="Без --project у show")
        proc = run_cli(["show", card_id], self.boards_dir)
        self.assertEqual(proc.returncode, 0)

    # ---------------------------------------------------------- move: обычный путь

    def test_move_backlog_to_new(self):
        card_id = self.add_card(title="Переносим")
        self.run_ok(["move", card_id, "--to", "new"])
        card = boardlib.get_card(self.project, card_id, boards_dir=self.boards_dir)
        self.assertEqual(card["status"], boardlib.NEW)

    def test_move_review_requires_ask(self):
        card_id = self.add_card(title="Нужно ask", status="in_progress")
        proc = run_cli(["move", card_id, "--to", "review"], self.boards_dir)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ask", proc.stderr)

    def test_move_done_requires_proof_and_accept_how(self):
        card_id = self.add_card(title="Нужен proof", status="in_progress")
        proc = run_cli(["move", card_id, "--to", "done"], self.boards_dir)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("proof", proc.stderr)

    def test_move_full_funnel_to_done(self):
        card_id = self.add_card(title="Полный путь")
        self.run_ok(["move", card_id, "--to", "new"])
        self.run_ok(["move", card_id, "--to", "in_progress"])
        self.run_ok(["move", card_id, "--to", "review", "--ask", "Что решить?"])
        self.owner_answers(card_id)
        self.run_ok([
            "move", card_id, "--to", "done",
            "--proof", "прогнан тест", "--accept-how", "открой страницу → видна колонка",
        ])
        card = boardlib.get_card(self.project, card_id, boards_dir=self.boards_dir)
        self.assertEqual(card["status"], boardlib.DONE)

    # ---------------------------------------------------------- move: критерий и приёмка (спека 2026-09-29)

    def to_done(self, card_id, accept_how=HOW):
        for status in ("new", "in_progress"):
            self.run_ok(["move", card_id, "--to", status])
        args = ["move", card_id, "--to", "done", "--proof", "готово, проверено"]
        if accept_how is not None:
            args += ["--accept-how", accept_how]
        return run_cli(args, self.boards_dir)

    def test_move_done_without_accept_how_refused_for_any_acceptance(self):
        # Готово-когда 1: без --accept-how код 1 у карточки с любой приёмкой
        for acceptance in ("owner", "reviewer"):
            with self.subTest(acceptance=acceptance):
                card_id = self.add_card(title=f"Приёмка {acceptance}", acceptance=acceptance)
                proc = self.to_done(card_id, accept_how=None)
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertIn("accept_how", proc.stderr)
                self.assertEqual(boardlib.get_card(self.project, card_id, boards_dir=self.boards_dir)["status"],
                                 boardlib.IN_PROGRESS)

    def test_move_done_accept_how_format(self):
        card_id = self.add_card(title="Формат критерия")
        proc = self.to_done(card_id, accept_how="открой страницу и посмотри")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("что выполнить → что должно получиться", proc.stderr)
        proc = run_cli(["move", card_id, "--to", "done", "--proof", "готово", "--accept-how", HOW], self.boards_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(boardlib.get_card(self.project, card_id, boards_dir=self.boards_dir)["status"], boardlib.DONE)

    def test_add_defaults_to_reviewer_and_agent_may_set_owner(self):
        # Готово-когда 2
        default_id = self.add_card(title="Обычная")
        self.assertEqual(boardlib.get_card(self.project, default_id, boards_dir=self.boards_dir)["acceptance"],
                         boardlib.REVIEWER)
        owner_id = self.add_card(title="Письмо клиенту", acceptance="owner")
        self.assertEqual(boardlib.get_card(self.project, owner_id, boards_dir=self.boards_dir)["acceptance"],
                         boardlib.OWNER)
        proc = self.run_ok(["show", owner_id])
        self.assertIn("приёмка: владелец", proc.stdout)
        with self.assertRaises(boardlib.ForbiddenError):  # обратно — только владелец
            boardlib.edit_card(self.project, owner_id, boardlib.AGENT, acceptance="reviewer", boards_dir=self.boards_dir)

    def test_move_agent_cannot_accept_any_card(self):
        # Готово-когда 5: агент не ставит «Завершена», флага --as-reviewer нет (код 2)
        card_id = self.add_card(title="Этап 1", kind="этап", stage=1)
        self.assertEqual(self.to_done(card_id).returncode, 0)
        proc = run_cli(["move", card_id, "--to", "accepted"], self.boards_dir)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("verify", proc.stderr)
        proc = run_cli(["move", card_id, "--to", "accepted", "--as-reviewer", "--proof", "готово"], self.boards_dir)
        self.assertEqual(proc.returncode, 2)
        card = boardlib.get_card(self.project, card_id, boards_dir=self.boards_dir)
        self.assertEqual(card["status"], boardlib.DONE)

    def test_move_agent_cannot_accept_owner_card(self):
        card_id = self.add_card(title="Владелец принимает", acceptance="owner")
        self.assertEqual(self.to_done(card_id).returncode, 0)
        proc = run_cli(["move", card_id, "--to", "accepted"], self.boards_dir)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("владел", proc.stderr.lower())
        card = boardlib.get_card(self.project, card_id, boards_dir=self.boards_dir)
        self.assertEqual(card["status"], boardlib.DONE)

    # ---------------------------------------------------------- verify / verify-queue / migrate-acceptance

    def output_file(self, text="HTTP/2 200\nserver: nginx\n", name="out.txt"):
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_verify_ok_accepts_and_show_prints_check(self):
        # Готово-когда 3
        card_id = self.add_card(title="Деплой", kind="этап", stage=4)
        self.assertEqual(self.to_done(card_id).returncode, 0)
        proc = self.run_ok(["verify", card_id, "--ok", "--command", COMMAND, "--output-file", self.output_file()])
        self.assertIn("Выполнена → Завершена", proc.stdout)
        self.assertIn("принято проверяющим", proc.stdout)
        card = boardlib.get_card(self.project, card_id, boards_dir=self.boards_dir)
        self.assertEqual(card["status"], boardlib.ACCEPTED)
        self.assertEqual(card["verified"]["verdict"], "ok")
        proc = self.run_ok(["show", card_id])
        self.assertIn("=== ПРОВЕРКА ===", proc.stdout)
        self.assertIn(COMMAND, proc.stdout)
        self.assertIn("совпало", proc.stdout)
        self.assertIn("server: nginx", proc.stdout)
        self.assertIn("проверяющий: Выполнена → Завершена — принято проверяющим", proc.stdout)

    def test_verify_ok_refusals(self):
        card_id = self.add_card(title="Деплой")
        self.assertEqual(self.to_done(card_id).returncode, 0)
        before = boardlib.get_card(self.project, card_id, boards_dir=self.boards_dir)
        empty = self.tmp / "empty.txt"
        empty.write_text("", encoding="utf-8")
        cases = {
            "без файла": ["verify", card_id, "--ok", "--command", COMMAND],
            "нет файла": ["verify", card_id, "--ok", "--command", COMMAND, "--output-file", str(self.tmp / "нет.txt")],
            "пустой файл": ["verify", card_id, "--ok", "--command", COMMAND, "--output-file", str(empty)],
            "секрет": ["verify", card_id, "--ok", "--command", COMMAND,
                       "--output-file", self.output_file("HTTP/2 200\nAuthorization: Bearer abc\n", "secret.txt")],
            "без команды": ["verify", card_id, "--ok", "--output-file", self.output_file()],
        }
        for name, args in cases.items():
            with self.subTest(case=name):
                proc = run_cli(args, self.boards_dir)
                self.assertEqual(proc.returncode, 1, f"{name}: {proc.stderr}")
        owner_id = self.add_card(title="Принимаю сам", acceptance="owner")
        self.assertEqual(self.to_done(owner_id).returncode, 0)
        proc = run_cli(["verify", owner_id, "--ok", "--command", COMMAND, "--output-file", self.output_file()],
                       self.boards_dir)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("владелец", proc.stderr)
        proc = run_cli(["verify", card_id], self.boards_dir)
        self.assertEqual(proc.returncode, 2)  # вердикт обязателен уже в argparse
        after = boardlib.get_card(self.project, card_id, boards_dir=self.boards_dir)
        self.assertEqual((before["version"], before["status"]), (after["version"], after["status"]))
        self.assertIsNone(after["verified"])

    def test_verify_fail_and_need_criteria(self):
        # Готово-когда 4
        card_id = self.add_card(title="Вход")
        self.assertEqual(self.to_done(card_id).returncode, 0)
        proc = run_cli(["verify", card_id, "--fail", "--command", COMMAND, "--output-file", self.output_file("HTTP/2 502\n")],
                       self.boards_dir)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("comment", proc.stderr)
        proc = self.run_ok(["verify", card_id, "--fail", "--comment", "ответ 502, а не 200",
                            "--command", COMMAND, "--output-file", self.output_file("HTTP/2 502\n")])
        self.assertIn("на доработку", proc.stdout)
        card = boardlib.get_card(self.project, card_id, boards_dir=self.boards_dir)
        self.assertEqual((card["status"], card["rework"]), (boardlib.IN_PROGRESS, "ответ 502, а не 200"))
        self.assertIn("[на доработку]", self.run_ok(["list", "--project", self.project]).stdout)
        proc = run_cli(["move", card_id, "--to", "done"], self.boards_dir)  # без нового proof
        self.assertEqual(proc.returncode, 1)
        self.assertIn("свежее доказательство", proc.stderr)
        self.run_ok(["move", card_id, "--to", "done", "--proof", "поправлено, ответ 200"])
        proc = self.run_ok(["verify", card_id, "--need-criteria"])
        self.assertIn("запрошен критерий", proc.stdout)
        card = boardlib.get_card(self.project, card_id, boards_dir=self.boards_dir)
        self.assertEqual(card["status"], boardlib.IN_PROGRESS)
        self.assertEqual(card["rework"], boardlib.NEED_CRITERIA_COMMENT)

    def test_verify_queue_lists_reviewer_cards_without_proof(self):
        done_id = self.add_card(title="Домен", body="привязать домен")
        self.assertEqual(self.to_done(done_id).returncode, 0)
        owner_id = self.add_card(title="Тексты", acceptance="owner")
        self.assertEqual(self.to_done(owner_id).returncode, 0)
        proc = self.run_ok(["verify-queue", "--project", self.project])
        self.assertIn(done_id, proc.stdout)
        self.assertIn("привязать домен", proc.stdout)
        self.assertIn(f"критерий: {HOW}", proc.stdout)
        self.assertNotIn(owner_id, proc.stdout)
        self.assertNotIn("готово, проверено", proc.stdout)  # proof проверяющему не показывается
        self.assertIn("В очереди: 1", proc.stdout)
        proc = self.run_ok(["verify-queue", "--all"])
        self.assertIn(f"== {self.project} ==", proc.stdout)
        self.run_ok(["verify", done_id, "--ok", "--command", COMMAND, "--output-file", self.output_file()])
        self.assertIn("Очередь проверки пуста", self.run_ok(["verify-queue", "--project", self.project]).stdout)

    def test_summary_prints_verify_pending_line(self):
        card_id = self.add_card(title="Ждёт агента")
        self.assertEqual(self.to_done(card_id).returncode, 0)
        proc = self.run_ok(["summary"])
        self.assertIn("Ждёт проверки агентом: 1", proc.stdout)
        self.assertNotIn(card_id, proc.stdout.split("Не закончено")[0])

    def test_migrate_acceptance_dry_run_then_apply_then_nothing(self):
        # Готово-когда 9
        open_id = self.add_card(title="Открытая", acceptance="owner")
        accepted_id = self.add_card(title="Принятая", acceptance="owner")
        self.assertEqual(self.to_done(accepted_id).returncode, 0)
        boardlib.move_card(self.project, accepted_id, "accepted", boardlib.OWNER, boards_dir=self.boards_dir)
        file = boardlib.project_path(self.project, boards_dir=self.boards_dir)
        # новый проект уже помечен мигрированным — команда так и говорит и ничего не меняет
        before = file.read_bytes()
        proc = self.run_ok(["migrate-acceptance", "--project", self.project, "--dry-run"])
        self.assertIn("уже выполнена", proc.stdout)
        self.assertEqual(before, file.read_bytes())
        # файл старого кода: без отметки
        import json
        raw = json.loads(file.read_text(encoding="utf-8"))
        raw.pop("acceptance_migrated")
        file.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        before = file.read_bytes()
        proc = self.run_ok(["migrate-acceptance", "--project", self.project, "--dry-run"])
        self.assertIn(open_id, proc.stdout)
        self.assertNotIn(accepted_id, proc.stdout)
        self.assertIn("--dry-run", proc.stdout)
        self.assertEqual(before, file.read_bytes())
        proc = self.run_ok(["migrate-acceptance"])  # без --project — все проекты доски
        self.assertIn(open_id, proc.stdout)
        self.assertEqual(boardlib.get_card(self.project, open_id, boards_dir=self.boards_dir)["acceptance"],
                         boardlib.REVIEWER)
        self.assertEqual(boardlib.get_card(self.project, accepted_id, boards_dir=self.boards_dir)["acceptance"],
                         boardlib.OWNER)
        # владелец вернул «принимаю сам» — повтор (и dry-run) это не снимает
        boardlib.edit_card(self.project, open_id, boardlib.OWNER, acceptance="owner", boards_dir=self.boards_dir)
        before = file.read_bytes()
        proc = self.run_ok(["migrate-acceptance", "--project", self.project])
        self.assertIn("уже выполнена", proc.stdout)
        self.assertIn("менять нечего", proc.stdout)
        proc = self.run_ok(["migrate-acceptance", "--project", self.project, "--dry-run"])
        self.assertIn("уже выполнена", proc.stdout)
        self.assertEqual(before, file.read_bytes())
        self.assertEqual(boardlib.get_card(self.project, open_id, boards_dir=self.boards_dir)["acceptance"],
                         boardlib.OWNER)

    def test_no_owner_role_option_anywhere(self):
        card_id = self.add_card(title="Нет флага владельца")
        proc = run_cli(["move", card_id, "--to", "new", "--role", "owner"], self.boards_dir)
        self.assertEqual(proc.returncode, 2)  # argparse: такого аргумента не существует

    # ---------------------------------------------------------- comment / handoff

    def test_comment_visible_in_show(self):
        card_id = self.add_card(title="С комментарием")
        self.run_ok(["comment", card_id, "--text", "Проверил, всё ок"])
        proc = self.run_ok(["show", card_id])
        self.assertIn("Проверил, всё ок", proc.stdout)

    def test_handoff_records_stopped_and_resume(self):
        card_id = self.add_card(title="Хэндофф")
        self.run_ok([
            "handoff", card_id, "--stopped", "Написал команды", "--resume", "Продолжи с тестов",
        ])
        card = boardlib.get_card(self.project, card_id, boards_dir=self.boards_dir)
        self.assertEqual(card["stopped_at"], "Написал команды")
        self.assertEqual(card["resume"], "Продолжи с тестов")

    # ---------------------------------------------------------- ссылка на сессию

    def test_handoff_without_session_env_leaves_session_empty(self):
        card_id = self.add_card(title="Без сессии")
        self.run_ok(["handoff", card_id, "--stopped", "стоп", "--resume", "продолжи"])
        card = boardlib.get_card(self.project, card_id, boards_dir=self.boards_dir)
        self.assertIsNone(card["session"])

    def test_handoff_with_session_env_sets_link(self):
        card_id = self.add_card(title="С сессией")
        proc = run_cli(
            ["handoff", card_id, "--stopped", "стоп", "--resume", "продолжи", "--session-title", "Моя сессия"],
            self.boards_dir, session_id="abc123",
        )
        self.assertEqual(proc.returncode, 0)
        card = boardlib.get_card(self.project, card_id, boards_dir=self.boards_dir)
        self.assertEqual(card["session"]["link"], "claude://claude.ai/epitaxy/abc123")
        self.assertEqual(card["session"]["title"], "Моя сессия")

    def test_add_records_session_when_env_present(self):
        proc = run_cli(
            ["add", "--project", self.project, "--title", "Новая с сессией"],
            self.boards_dir, session_id="sess1",
        )
        self.assertEqual(proc.returncode, 0)
        card_id = re.match(r"([\w-]+)\s", proc.stdout).group(1)
        card = boardlib.get_card(self.project, card_id, boards_dir=self.boards_dir)
        self.assertEqual(card["session"]["link"], "claude://claude.ai/epitaxy/sess1")

    def test_move_keeps_session_title_when_same_link(self):
        card_id = self.add_card(title="Сохраняем название сессии")
        run_cli(
            ["handoff", card_id, "--stopped", "стоп", "--resume", "далее", "--session-title", "Сессия А"],
            self.boards_dir, session_id="sameid",
        )
        proc = run_cli(["move", card_id, "--to", "new"], self.boards_dir, session_id="sameid")
        self.assertEqual(proc.returncode, 0)
        card = boardlib.get_card(self.project, card_id, boards_dir=self.boards_dir)
        self.assertEqual(card["session"]["title"], "Сессия А")

    def test_move_clears_session_title_when_link_changes(self):
        card_id = self.add_card(title="Смена сессии")
        run_cli(
            ["handoff", card_id, "--stopped", "стоп", "--resume", "далее", "--session-title", "Сессия А"],
            self.boards_dir, session_id="sameid",
        )
        proc = run_cli(["move", card_id, "--to", "new"], self.boards_dir, session_id="other-id")
        self.assertEqual(proc.returncode, 0, msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        card = boardlib.get_card(self.project, card_id, boards_dir=self.boards_dir)
        self.assertEqual(card["session"]["title"], "")

    def test_add_with_garbage_session_env_still_succeeds(self):
        # Мусорное значение CLAUDE_CODE_HOST_SESSION_ID (не идентификатор) не должно превращать
        # уже выполненное действие в отказ: раньше add падал с кодом 1 на попытке записать
        # session.link, хотя карточка уже была создана — при повторе агент продублировал бы её.
        proc = run_cli(
            ["add", "--project", self.project, "--title", "Мусорная сессия"],
            self.boards_dir, session_id="../x",
        )
        self.assertEqual(proc.returncode, 0, msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        self.assertIn("переменная сессии не похожа на идентификатор", proc.stderr)
        card_id = re.match(r"([\w-]+)\s", proc.stdout).group(1)
        card = boardlib.get_card(self.project, card_id, boards_dir=self.boards_dir)
        self.assertIsNone(card["session"])

    def test_move_with_garbage_session_env_still_succeeds(self):
        card_id = self.add_card(title="Мусор при move")
        proc = run_cli(
            ["move", card_id, "--to", "new"], self.boards_dir, session_id='bad id "with quotes"',
        )
        self.assertEqual(proc.returncode, 0, msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        self.assertIn("переменная сессии не похожа на идентификатор", proc.stderr)
        card = boardlib.get_card(self.project, card_id, boards_dir=self.boards_dir)
        self.assertEqual(card["status"], boardlib.NEW)  # основное действие не пострадало
        self.assertIsNone(card["session"])

    def test_comment_with_garbage_session_env_still_succeeds(self):
        card_id = self.add_card(title="Мусор при comment")
        proc = run_cli(
            ["comment", card_id, "--text", "текст комментария"],
            self.boards_dir, session_id="../../etc",
        )
        self.assertEqual(proc.returncode, 0, msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        self.assertIn("переменная сессии не похожа на идентификатор", proc.stderr)
        card = boardlib.get_card(self.project, card_id, boards_dir=self.boards_dir)
        self.assertEqual(len(card["comments"]), 1)  # комментарий записан
        self.assertIsNone(card["session"])

    def test_handoff_with_garbage_session_env_still_writes_stopped_and_resume(self):
        card_id = self.add_card(title="Мусор при handoff")
        proc = run_cli(
            ["handoff", card_id, "--stopped", "стоп", "--resume", "далее"],
            self.boards_dir, session_id="not a valid id!",
        )
        self.assertEqual(proc.returncode, 0, msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        self.assertIn("переменная сессии не похожа на идентификатор", proc.stderr)
        card = boardlib.get_card(self.project, card_id, boards_dir=self.boards_dir)
        self.assertEqual(card["stopped_at"], "стоп")
        self.assertEqual(card["resume"], "далее")
        self.assertIsNone(card["session"])

    # ---------------------------------------------------------- inbox

    def test_inbox_shows_rework_once_and_ack_clears(self):
        card_id = self.add_card(title="На доработку")
        for status in ("new", "in_progress"):
            self.run_ok(["move", card_id, "--to", status])
        self.run_ok(["move", card_id, "--to", "review", "--ask", "Ок?"])
        self.owner_answers(card_id)
        self.run_ok([
            "move", card_id, "--to", "done", "--proof", "готово", "--accept-how", HOW,
        ])
        # возврат на доработку делаем напрямую boardlib ролью owner — CLI такой команды не даёт
        boardlib.move_card(
            self.project, card_id, to=boardlib.IN_PROGRESS, role=boardlib.OWNER,
            comment="Не то, переделай", boards_dir=self.boards_dir,
        )
        proc1 = self.run_ok(["inbox", "--project", self.project])
        self.assertIn("на доработку", proc1.stdout)
        self.assertIn("Не то, переделай", proc1.stdout)
        proc2 = self.run_ok(["inbox", "--project", self.project])
        self.assertNotIn("на доработку", proc2.stdout)
        self.assertIn("Входящих нет", proc2.stdout)

    def test_inbox_peek_does_not_move_cursor(self):
        card_id = self.add_card(title="Peek")
        boardlib.comment_card(self.project, card_id, boardlib.OWNER, "Комментарий владельца", boards_dir=self.boards_dir)
        proc1 = self.run_ok(["inbox", "--project", self.project, "--peek"])
        self.assertIn("Комментарий владельца", proc1.stdout)
        proc2 = self.run_ok(["inbox", "--project", self.project, "--peek"])
        self.assertIn("Комментарий владельца", proc2.stdout)  # курсор не сдвинулся
        proc3 = self.run_ok(["inbox", "--project", self.project])
        self.assertIn("Комментарий владельца", proc3.stdout)
        proc4 = self.run_ok(["inbox", "--project", self.project])
        self.assertIn("Входящих нет", proc4.stdout)

    # ---------------------------------------------------------- определение проекта

    def test_project_resolution_by_cwd(self):
        self.add_card(title="Найдено по папке")
        nested = self.project_dir / "sub" / "deep"
        nested.mkdir(parents=True)
        proc = run_cli(["list"], self.boards_dir, cwd=nested)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("Найдено по папке", proc.stdout)

    def test_project_resolution_by_env_var(self):
        self.add_card(title="Найдено по BOARD_PROJECT")
        proc = run_cli(["list"], self.boards_dir, cwd=self.tmp, board_project=self.project)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("Найдено по BOARD_PROJECT", proc.stdout)

    def test_project_resolution_fails_without_any_hint(self):
        proc = run_cli(["list"], self.boards_dir, cwd=self.tmp)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("project", proc.stderr)

    # ---------------------------------------------------------- версия карточки

    def test_stale_version_conflict_is_refused(self):
        # Настоящая гонка двух процессов недетерминирована; конфликт версии внутри одного
        # прохода cmd_move невозможен (команда сама читает version перед записью), поэтому
        # здесь подменяется boardlib.get_card — так же, как это сделала бы гонка с другим
        # процессом, изменившим карточку между чтением версии и записью.
        card_id = self.add_card(title="Устаревшая версия")
        real_card = boardlib.get_card(self.project, card_id, boards_dir=self.boards_dir)
        stale_card = dict(real_card)
        stale_card["version"] = real_card["version"] + 1  # версия, которой ещё не было
        os.environ["BOARD_DIR"] = str(self.boards_dir)
        try:
            with mock.patch.object(boardlib, "get_card", return_value=stale_card):
                buf_out, buf_err = [], []
                import io
                import contextlib
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = board.main(["move", card_id, "--to", "new"])
        finally:
            os.environ.pop("BOARD_DIR", None)
        self.assertEqual(code, 1)
        self.assertIn("перечитай", err.getvalue().lower())
        self.assertIn("повтори", err.getvalue().lower())


class ServeCliTestCase(unittest.TestCase):
    """board serve — та же программа, что server.py, через одну обёртку `board`."""

    def test_serve_help(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            proc = run_cli(["serve", "--help"], Path(tmp))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("--port", proc.stdout)
        self.assertIn("127.0.0.1", proc.stdout)

    def test_serve_without_data_dir_fails_clearly(self):
        # В процессе, с подменой: в отдельном процессе поиск раскладки нашёл бы настоящие диски.
        import io
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            clean = {k: v for k, v in os.environ.items() if k not in ("BOARD_DIR", "TATET_WIKI", "CLAUDE_CONFIG_DIR")}
            err = io.StringIO()
            with mock.patch.dict(os.environ, dict(clean, CLAUDE_CONFIG_DIR=tmp), clear=True), \
                    mock.patch.object(boardlib, "_layout_candidates", return_value=[]), \
                    mock.patch("sys.stderr", err):
                with self.assertRaises(SystemExit) as ctx:
                    board.main(["serve", "--port", "0"])
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("ПОМИЛКА  дані дошки", err.getvalue())
        self.assertIn("setup.py", err.getvalue())


class ProjectPathCliTestCase(unittest.TestCase):
    """Путь проекта (план доски-прогресс-бара, часть D): start, finish, fog, sprint, add --sprint,
    progress, inbox с событиями пути. Решения владельца — ролью «владелец в чате» с цитатой."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.boards_dir = self.tmp / "boards"
        self.boards_dir.mkdir()
        self.project = "demo"
        boardlib.create_project(self.project, "демо-проект", str(self.tmp / "proj"), boards_dir=self.boards_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def run_ok(self, args):
        proc = run_cli(args, self.boards_dir)
        self.assertEqual(proc.returncode, 0, msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        return proc

    def run_code(self, args, code):
        proc = run_cli(args, self.boards_dir)
        self.assertEqual(proc.returncode, code, msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        return proc

    def p(self, *args):
        """Аргументы команды с --project в конце."""
        return [*args, "--project", self.project]

    def data(self):
        return boardlib.read_project(self.project, boards_dir=self.boards_dir)

    def two_sprints(self, open_first=True):
        self.run_ok(self.p("sprint", "add", "--title", "Волна 1", "--done-when", "ядро на проде"))
        self.run_ok(self.p("sprint", "add", "--title", "Волна 2", "--done-when", "каналы подключены"))
        if open_first:
            self.run_ok(self.p("sprint", "open", "s1", "--owner-said", "ок, открывай первую"))

    def assert_need_owner_said(self, proc):
        self.assertEqual(proc.returncode, 1, msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        self.assertIn(boardlib.NEED_OWNER_SAID, proc.stderr)

    # ---------------------------------------------------------- D1: start, finish, fog

    def test_start_first_by_agent_repeat_needs_owner_said(self):
        self.run_ok(self.p("start", "--date", "2026-09-19", "--event", "спека утверждена"))
        self.assertEqual(self.data()["start"], {"date": "2026-09-19", "event": "спека утверждена"})
        self.assert_need_owner_said(run_cli(self.p("start", "--date", "2026-09-20", "--event", "другое"), self.boards_dir))
        self.assertEqual(self.data()["start"]["date"], "2026-09-19")  # не изменился
        proc = self.run_ok(self.p("start", "--date", "2026-09-20", "--event", "другое", "--owner-said", "старт с 20-го"))
        self.assertIn("записал решение владельца", proc.stdout)
        self.assertEqual(self.data()["start"]["date"], "2026-09-20")

    def test_start_bad_date_refused(self):
        proc = self.run_code(self.p("start", "--date", "19.09.2026", "--event", "старт"), 1)
        self.assertIn("ОШИБКА  date", proc.stderr)

    def test_finish_propose_then_approve_needs_owner_said(self):
        self.run_ok(self.p("finish", "--text", "легаси выключен"))
        self.assertFalse(self.data()["finish"]["approved"])
        self.assert_need_owner_said(run_cli(self.p("finish", "approve"), self.boards_dir))
        self.assertFalse(self.data()["finish"]["approved"])
        proc = self.run_ok(self.p("finish", "approve", "--owner-said", "да, финиш такой"))
        self.assertIn("«да, финиш такой»", proc.stdout)
        finish = self.data()["finish"]
        self.assertTrue(finish["approved"])
        self.assertEqual(finish["history"][-1]["who"], boardlib.OWNER_CHAT)
        self.assertEqual(finish["history"][-1]["said"], "да, финиш такой")
        listing = self.run_ok(self.p("sprint", "list", "--history")).stdout
        self.assertIn("Финиш: легаси выключен [утверждён]", listing)
        self.assertIn("«да, финиш такой»", listing)
        # утверждённый финиш агент уже не переписывает
        self.run_code(self.p("finish", "--text", "другой финиш"), 1)

    def test_finish_owner_said_only_with_approve(self):
        self.run_code(self.p("finish", "--text", "финиш", "--owner-said", "ок"), 2)
        self.assertIsNone(self.data()["finish"])

    def test_finish_without_text_is_argument_error(self):
        self.run_code(self.p("finish"), 2)

    def test_fog_set_whole_list_and_limit(self):
        lines = [f"строка {i}" for i in range(1, 6)]
        args = ["fog", "set"]
        for line in lines:
            args += ["--line", line]
        self.run_ok(self.p(*args))
        self.assertEqual(self.data()["fog"], lines)
        proc = self.run_code(self.p(*args, "--line", "шестая"), 1)
        self.assertIn("ОШИБКА  fog", proc.stderr)
        self.assertEqual(self.data()["fog"], lines)  # прежний туман цел
        self.run_ok(self.p("fog", "set"))
        self.assertEqual(self.data()["fog"], [])

    # ---------------------------------------------------------- D2, D5: спринты

    def test_sprint_open_and_close_need_owner_said(self):
        self.two_sprints(open_first=False)
        self.assert_need_owner_said(run_cli(self.p("sprint", "open", "s1"), self.boards_dir))
        self.assertEqual(self.data()["sprints"][0]["status"], boardlib.SPRINT_NEXT)
        self.run_ok(self.p("sprint", "open", "s1", "--owner-said", "ок, открывай первую"))
        # второй текущий — отказ правил, не аргументов
        self.run_code(self.p("sprint", "open", "s2", "--owner-said", "и вторую"), 1)
        self.run_ok(self.p("sprint", "ready", "s1", "--proof", "тесты зелёные, проверено на стенде"))
        self.assert_need_owner_said(run_cli(self.p("sprint", "close", "s1"), self.boards_dir))
        self.assertEqual(self.data()["sprints"][0]["status"], boardlib.SPRINT_CLOSING)
        self.run_ok(self.p("sprint", "close", "s1", "--owner-said", "закрываем, принято"))
        sprint = self.data()["sprints"][0]
        self.assertEqual(sprint["status"], boardlib.SPRINT_PASSED)
        whos = [(h["who"], h["to"], h["said"]) for h in sprint["history"]]
        self.assertEqual(whos, [
            (boardlib.AGENT, boardlib.SPRINT_NEXT, ""),
            (boardlib.OWNER_CHAT, boardlib.SPRINT_CURRENT, "ок, открывай первую"),
            (boardlib.AGENT, boardlib.SPRINT_CLOSING, ""),
            (boardlib.OWNER_CHAT, boardlib.SPRINT_PASSED, "закрываем, принято"),
        ])
        listing = self.run_ok(self.p("sprint", "list", "--history")).stdout
        self.assertIn("s1  [пройден]  Волна 1", listing)
        self.assertIn("владелец в чате: следующий → текущий — владелец: «ок, открывай первую»", listing)
        self.assertIn("владелец в чате: ждёт закрытия → пройден — владелец: «закрываем, принято»", listing)
        short = self.run_ok(self.p("sprint", "list")).stdout
        self.assertNotIn("«ок, открывай первую»", short)  # история — только с --history

    def test_sprint_ready_requires_proof(self):
        self.two_sprints()
        self.run_code(self.p("sprint", "ready", "s1"), 2)  # --proof обязателен уже в argparse
        proc = self.run_code(self.p("sprint", "ready", "s1", "--proof", "   "), 1)
        self.assertIn("sprint_proof", proc.stderr)
        self.assertEqual(self.data()["sprints"][0]["status"], boardlib.SPRINT_CURRENT)

    def test_owner_said_not_accepted_by_agent_commands(self):
        self.run_code(self.p("sprint", "add", "--title", "В", "--done-when", "д", "--owner-said", "ок"), 2)
        self.run_code(self.p("fog", "set", "--line", "x", "--owner-said", "ок"), 2)

    def test_sprint_list_without_sprints(self):
        out = self.run_ok(self.p("sprint", "list")).stdout
        self.assertIn("Спринтов нет", out)

    # ---------------------------------------------------------- D3: add --sprint, assign

    def test_add_goes_to_current_sprint_or_given_one(self):
        self.two_sprints()
        proc = self.run_ok(self.p("add", "--title", "В текущий"))
        self.assertIn("спринт s1", proc.stdout)
        card_id = proc.stdout.split()[0]
        self.assertEqual(boardlib.get_card(self.project, card_id, boards_dir=self.boards_dir)["sprint"], "s1")
        proc = self.run_ok(self.p("add", "--title", "Во второй", "--sprint", "s2"))
        card_id = proc.stdout.split()[0]
        self.assertEqual(boardlib.get_card(self.project, card_id, boards_dir=self.boards_dir)["sprint"], "s2")
        proc = self.run_ok(self.p("add", "--title", "Без спринта", "--sprint", "none"))
        card_id = proc.stdout.split()[0]
        self.assertIsNone(boardlib.get_card(self.project, card_id, boards_dir=self.boards_dir)["sprint"])
        self.run_code(self.p("add", "--title", "Чужой", "--sprint", "s9"), 1)

    def test_sprint_assign_agent_and_done_card(self):
        self.two_sprints()
        card_id = self.run_ok(self.p("add", "--title", "Задача")).stdout.split()[0]
        self.run_ok(["sprint", "assign", card_id, "--sprint", "s2"])
        self.assertEqual(boardlib.get_card(self.project, card_id, boards_dir=self.boards_dir)["sprint"], "s2")
        done = boardlib.add_card(
            self.project, boardlib.AGENT, title="Сделано", status=boardlib.DONE, proof="прогнан тест",
            accept_how="открой страницу → видна форма", sprint=None, boards_dir=self.boards_dir,
        )
        self.run_code(["sprint", "assign", done["id"], "--sprint", "s1"], 1)
        self.assertIsNone(boardlib.get_card(self.project, done["id"], boards_dir=self.boards_dir)["sprint"])
        self.assert_need_owner_said(run_cli(["sprint", "assign", done["id"], "--sprint", "s1", "--owner-said", ""], self.boards_dir))
        self.run_ok(["sprint", "assign", done["id"], "--sprint", "s1", "--owner-said", "это из первой волны"])
        self.assertEqual(boardlib.get_card(self.project, done["id"], boards_dir=self.boards_dir)["sprint"], "s1")

    # ---------------------------------------------------------- inbox: события пути

    def test_inbox_shows_path_decisions_with_quote(self):
        self.run_ok(self.p("finish", "--text", "легаси выключен"))
        self.two_sprints()
        self.run_ok(self.p("finish", "approve", "--owner-said", "да, финиш такой"))
        out = self.run_ok(["inbox", "--project", self.project]).stdout
        self.assertNotIn("None", out)
        self.assertIn("s1  [спринт открыт]  Волна 1 — владелец: «ок, открывай первую»", out)
        self.assertIn("финиш  [финиш утверждён]  легаси выключен — владелец: «да, финиш такой»", out)
        self.assertNotIn("[новый спринт]", out)  # спринты предлагал агент — не событие владельца
        self.assertIn("Входящих нет", self.run_ok(["inbox", "--project", self.project]).stdout)

    # ---------------------------------------------------------- D4: progress

    def page_path(self):
        return self.boards_dir / "progress" / f"{self.project}.html"

    def test_progress_builds_page_and_summary_line(self):
        self.run_ok(self.p("finish", "--text", "легаси выключен"))
        self.two_sprints()
        self.run_ok(self.p("add", "--title", "Поднять очередь"))
        proc = self.run_ok(self.p("progress"))
        page = self.page_path()
        self.assertTrue(page.is_file())
        html_text = page.read_text(encoding="utf-8")
        self.assertIn("Поднять очередь", html_text)
        self.assertIn("Волна 1", html_text)
        # нужно от тебя: только неутверждённый финиш — s1 уже текущий, предлагать открыть s2 рано
        # (задача 1 «исправления»: open_sprint всё равно откажет, пока текущий спринт идёт)
        self.assertIn(
            f"Страница собрана: {page} · спринт 1 из 2 · нужно от тебя: 1 · замечаний: 0", proc.stdout,
        )
        self.assertNotIn("Опубликовать", proc.stdout)
        self.assertEqual(list(page.parent.glob("*.tmp")), [])  # временный файл не остался

    def test_progress_project_without_sprints(self):
        self.run_ok(self.p("add", "--title", "Одна задача"))
        proc = self.run_ok(self.p("progress"))
        self.assertIn("спринт 1 из 1", proc.stdout)
        self.assertIn("замечаний: 0", proc.stdout)
        self.assertIn("Без названия", self.page_path().read_text(encoding="utf-8"))

    def test_progress_data_error_keeps_old_file(self):
        self.two_sprints()
        page = self.page_path()
        page.parent.mkdir(parents=True)
        page.write_text("старая страница", encoding="utf-8")
        # два текущих спринта командами не сделать — портим файл проекта напрямую
        import json
        file = boardlib.project_path(self.project, boards_dir=self.boards_dir)
        raw = json.loads(file.read_text(encoding="utf-8"))
        raw["sprints"][1]["status"] = boardlib.SPRINT_CURRENT
        file.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        proc = self.run_code(self.p("progress"), 1)
        self.assertIn("ОШИБКА  sprints", proc.stderr)
        self.assertNotIn("Страница собрана", proc.stdout)
        self.assertEqual(page.read_text(encoding="utf-8"), "старая страница")

    def test_progress_out_publish_and_set_url(self):
        out = self.tmp / "custom" / "page.html"
        proc = self.run_ok(self.p("progress", "--out", str(out), "--publish"))
        self.assertTrue(out.is_file())
        self.assertIn(f"Опубликовать: {out} → новый артефакт", proc.stdout)
        url = "https://claude.ai/code/artifact/abc"
        proc = self.run_ok(self.p("progress", "--set-url", url, "--publish"))
        self.assertIn(f"Опубликовать: {self.page_path()} → {url}", proc.stdout)
        self.assertEqual(self.data()["progress_url"], url)
        proc = self.run_code(self.p("progress", "--set-url", "ftp://x"), 1)
        self.assertIn("progress_url", proc.stderr)
        self.assertEqual(self.data()["progress_url"], url)

    def test_progress_notes_cards_without_sprint(self):
        self.run_ok(self.p("add", "--title", "До спринтов"))  # спринтов ещё нет — без спринта
        self.two_sprints()
        proc = self.run_ok(self.p("progress"))
        self.assertIn("ЗАМЕЧАНИЕ  cards: без спринта карточек 1", proc.stdout)
        self.assertIn("замечаний: 1", proc.stdout)


class ImportSyncPipelineTestCase(unittest.TestCase):
    """import-pipeline и sync-pipeline: используют копию блока «Пайплайн» из tests/fixtures/AGENTS.md.

    Фикстура открывается только на чтение; блок копируется текстом во временную папку, украинский
    текст сохраняется как есть. Какие строки блока какой тест кормят — в шапке самой фикстуры.
    """

    @classmethod
    def setUpClass(cls):
        text = FIXTURE_AGENTS_MD.read_text(encoding="utf-8-sig")
        m = re.search(r"(?m)^##\s*(?:Пайплайн|Pipeline)\s*$", text)
        assert m, f"в {FIXTURE_AGENTS_MD.name} нет блока «## Пайплайн»"
        rest = text[m.end():]
        nxt = re.search(r"(?m)^##\s+", rest)
        cls.pipeline_block = rest[: nxt.start()] if nxt else rest
        # Пометки, как они лежат в самом источнике — считаны отдельной, независимой регуляркой
        # (не STAGE_LINE_RE/SYNC_MARK_RE из board.py), чтобы ожидание в тестах ниже не зависело от
        # того же кода, который проверяется.
        cls.original_markers = {
            int(num): marker
            for marker, num in re.findall(r"(?m)^- \[([ x~?!])\]\s*(\d+)", cls.pipeline_block)
        }

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.boards_dir = self.tmp / "boards"
        self.boards_dir.mkdir()
        self.project_dir = self.tmp / "demo"
        self.project_dir.mkdir()
        self.agents_md = self.project_dir / "AGENTS.md"
        self.agents_md.write_text(
            "# Fixture\n\n## Пайплайн\n" + self.pipeline_block + "\n## Конец\n\nостальное\n",
            encoding="utf-8", newline="\n",
        )
        self.project = "demo"

    def tearDown(self):
        self._tmp.cleanup()

    def run_ok(self, args, **kw):
        proc = run_cli(args, self.boards_dir, **kw)
        self.assertEqual(proc.returncode, 0, msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        return proc

    def do_import(self):
        return self.run_ok([
            "import-pipeline", "--project", self.project,
            "--from", str(self.agents_md), "--path", str(self.project_dir),
            "--phrase", "фабрика SEO-страниц",
        ])

    def test_import_creates_seven_stage_cards(self):
        self.do_import()
        cards = [c for c in boardlib.list_cards(self.project, boards_dir=self.boards_dir) if c["kind"] == "stage"]
        # архивных тут нет — Завершена входит в COLUMNS, так что list_cards без status их не теряет
        stages = sorted(c["stage"] for c in cards)
        self.assertEqual(stages, list(boardlib.STAGES))

    def test_import_backlog_stage_from_unchecked_box(self):
        self.do_import()
        card = boardlib.find_stage_card(self.project, 6, boards_dir=self.boards_dir)
        self.assertIsNotNone(card)
        self.assertEqual(card["status"], boardlib.BACKLOG)

    def test_import_x_stage_stays_done_for_verifier(self):
        # Спека 2026-09-29: импорт сам не принимает — все «[x]» садятся в «Выполнена» с приёмкой
        # «проверяющий» и критерием в формате «а → б»; в «Завершена» их доводит board verify.
        self.do_import()
        for stage, marker in self.original_markers.items():
            if marker != "x":
                continue
            card = boardlib.find_stage_card(self.project, stage, boards_dir=self.boards_dir)
            self.assertIsNotNone(card, f"этап {stage}")
            self.assertEqual(card["status"], boardlib.DONE, f"этап {stage}")
            self.assertEqual(card["acceptance"], boardlib.REVIEWER, f"этап {stage}")
            self.assertEqual(card["proof"], board.PIPELINE_PROOF)
            self.assertIsNotNone(boardlib.accept_how_parts(card["accept_how"]))
        queue = boardlib.verify_queue(self.project, boards_dir=self.boards_dir)
        self.assertEqual(len(queue), sum(1 for m in self.original_markers.values() if m in "x~?"))

    def test_import_owner_stage_x_stays_done(self):
        # этапы 4 и 5 — «[x]» → остаются в «Выполнена» (приёмка теперь у проверяющего, не у владельца)
        self.do_import()
        for stage in (4, 5):
            card = boardlib.find_stage_card(self.project, stage, boards_dir=self.boards_dir)
            self.assertIsNotNone(card, f"этап {stage}")
            self.assertEqual(card["status"], boardlib.DONE, f"этап {stage}")

    def test_import_caveat_stage_stays_done_with_comment(self):
        # этап 3 — «[~]» с оговоркой в скобках → «Выполнена» + комментарий «предлагаемая оговорка»
        self.do_import()
        card = boardlib.find_stage_card(self.project, 3, boards_dir=self.boards_dir)
        self.assertIsNotNone(card)
        self.assertEqual(card["status"], boardlib.DONE)
        texts = " ".join(c["text"] for c in card["comments"])
        self.assertIn("предлагаемая оговорка", texts)
        self.assertIn("відхилення: мобільна версія", texts)

    def test_import_caveat_without_parenthesis_gets_no_comment_and_warns(self):
        # Тот же исход, с которым прежде падал соседний тест на живом файле (комментариев нет):
        # «[~]» есть, а пояснения «(відхилення|отклонение|оговорка: …)» в скобках нет — карточка
        # садится в «Выполнена» без комментария, команда печатает замечание. Другие пути к тому же
        # исходу — пометка не «[~]» или оговорка, не прошедшая textcheck.
        bare_md = self.project_dir / "AGENTS_BARE.md"
        bare_md.write_text(
            "# Fixture\n\n## Пайплайн\n\n"
            "- [~] 3 Дизайн: макет узгоджено, мобільна версія без анімації\n"
            "\n## Конец\n",
            encoding="utf-8", newline="\n",
        )
        proc = self.run_ok([
            "import-pipeline", "--project", self.project, "--from", str(bare_md),
            "--path", str(self.project_dir), "--phrase", "фабрика SEO-страниц",
        ])
        self.assertIn("нет текста оговорки в скобках", proc.stdout)
        card = boardlib.find_stage_card(self.project, 3, boards_dir=self.boards_dir)
        self.assertEqual(card["status"], boardlib.DONE)
        self.assertEqual(card["comments"], [])

    def test_import_is_idempotent_no_duplicates(self):
        self.do_import()
        before = len(boardlib.list_cards(self.project, boards_dir=self.boards_dir))
        proc = self.do_import()
        after = len(boardlib.list_cards(self.project, boards_dir=self.boards_dir))
        self.assertEqual(before, after)
        self.assertIn("уже на доске", proc.stdout)

    def test_import_stage_cards_have_no_sprint(self):
        # Задача 5 плана «исправления»: карточки-этапы импорта — из блока «Пайплайн» проекта, не
        # из текущей работы сессии, поэтому не должны попадать в открытый текущий спринт молча.
        boardlib.create_project(self.project, "фабрика SEO-страниц", str(self.project_dir), boards_dir=self.boards_dir)
        sprint = boardlib.add_sprint(
            self.project, boardlib.AGENT, "Волна 1", "ядро на проде", boards_dir=self.boards_dir,
        )
        boardlib.open_sprint(
            self.project, boardlib.OWNER_CHAT, sprint["id"], said="ок, открывай", boards_dir=self.boards_dir,
        )
        self.do_import()
        cards = [c for c in boardlib.list_cards(self.project, boards_dir=self.boards_dir) if c["kind"] == "stage"]
        self.assertTrue(cards)
        for card in cards:
            self.assertIsNone(card["sprint"], msg=f"этап {card['stage']} попал в спринт {card['sprint']!r}")

    def test_import_creates_project_when_missing(self):
        self.assertNotIn(self.project, boardlib.list_projects(boards_dir=self.boards_dir))
        self.do_import()
        self.assertIn(self.project, boardlib.list_projects(boards_dir=self.boards_dir))

    def test_import_warns_on_unrecognized_marker_and_continues(self):
        # Раньше строка с нераспознанной пометкой («- [q] ...») тихо пропускалась (не проходила
        # STAGE_LINE_RE вовсе) — ни предупреждения, ни намёка, что этап 4 не завёлся.
        bad_md = self.project_dir / "AGENTS_BAD.md"
        bad_md.write_text(
            "# Fixture\n\n## Пайплайн\n\n"
            "- [x] 1 Каркас: готово\n"
            "- [q] 4 Деплой: странная пометка\n"
            "- [ ] 5 Вхід: не начато\n"
            "\n## Конец\n",
            encoding="utf-8", newline="\n",
        )
        proc = self.run_ok([
            "import-pipeline", "--project", self.project, "--from", str(bad_md),
            "--path", str(self.project_dir), "--phrase", "фабрика SEO-страниц",
        ])
        self.assertIn("не распознана", proc.stdout)
        self.assertIn("[q]", proc.stdout)
        self.assertIsNotNone(boardlib.find_stage_card(self.project, 1, boards_dir=self.boards_dir))
        self.assertIsNone(boardlib.find_stage_card(self.project, 4, boards_dir=self.boards_dir))
        self.assertIsNotNone(boardlib.find_stage_card(self.project, 5, boards_dir=self.boards_dir))

    def test_import_without_path_and_phrase_fails_clearly(self):
        proc = run_cli(
            ["import-pipeline", "--project", self.project, "--from", str(self.agents_md)],
            self.boards_dir,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("path", proc.stderr + proc.stdout if False else proc.stderr)

    def test_sync_pipeline_lf_roundtrip_only_markers_change(self):
        self.do_import()
        # принимаем этап 1 (сделано импортом), переводим этап 6 в работу с доработкой
        card6 = boardlib.find_stage_card(self.project, 6, boards_dir=self.boards_dir)
        boardlib.move_card(self.project, card6["id"], to=boardlib.NEW, role=boardlib.AGENT, boards_dir=self.boards_dir)
        boardlib.move_card(self.project, card6["id"], to=boardlib.IN_PROGRESS, role=boardlib.AGENT, boards_dir=self.boards_dir)
        boardlib.move_card(
            self.project, card6["id"], to=boardlib.DONE, role=boardlib.AGENT,
            proof="сделано", accept_how="открыть адрес → страница отвечает", boards_dir=self.boards_dir,
        )
        boardlib.move_card(
            self.project, card6["id"], to=boardlib.IN_PROGRESS, role=boardlib.OWNER,
            comment="переделай", boards_dir=self.boards_dir,
        )

        original_bytes = self.agents_md.read_bytes()
        self.assertNotIn(b"\r\n", original_bytes)  # файл записан с newline="\n" в setUp

        proc = self.run_ok(["sync-pipeline", "--project", self.project, "--file", str(self.agents_md)])
        self.assertIn("этап", proc.stdout)
        new_bytes = self.agents_md.read_bytes()
        self.assertNotEqual(new_bytes, original_bytes)

        # Что должно получиться, решено здесь по сценарию теста (не спрошено у board.py):
        # импорт с 2026-09-29 сам не принимает — все [x]/[~]/[?] этапы сидят в «Выполнена» → [?];
        # 6 — довели до «В работе» с rework (комментарий владельца «переделай») → [!]; остальные
        # (Бэклог) — без изменений.
        expected = self._apply_markers(original_bytes, {**self._expected_after_import(), 6: "!"})
        self.assertEqual(new_bytes, expected)

    def test_sync_pipeline_crlf_roundtrip_only_markers_change(self):
        self.do_import()
        crlf_path = self.project_dir / "AGENTS_CRLF.md"
        lf_text = self.agents_md.read_text(encoding="utf-8")
        crlf_bytes = lf_text.replace("\n", "\r\n").encode("utf-8")
        crlf_path.write_bytes(crlf_bytes)

        proc = self.run_ok(["sync-pipeline", "--project", self.project, "--file", str(crlf_path)])
        self.assertIn("этап", proc.stdout)
        new_bytes = crlf_path.read_bytes()
        self.assertIn(b"\r\n", new_bytes)

        # Свежий импорт без дополнительных переносов: все [x]/[~]/[?] — «Выполнена» → [?];
        # Бэклог — без изменений.
        expected = self._apply_markers(crlf_bytes, self._expected_after_import())
        self.assertEqual(new_bytes, expected)

    def test_sync_pipeline_dry_run_does_not_write(self):
        self.do_import()
        card1 = boardlib.find_stage_card(self.project, 1, boards_dir=self.boards_dir)
        self.assertEqual(card1["status"], boardlib.DONE)  # маркер в файле "x", на доске — ждёт проверяющего
        # имитируем расхождение: вернём этап 1 на доработку напрямую через boardlib (роль owner)
        boardlib.move_card(
            self.project, card1["id"], to=boardlib.IN_PROGRESS, role=boardlib.OWNER,
            comment="назад", boards_dir=self.boards_dir,
        )
        before = self.agents_md.read_bytes()
        proc = self.run_ok([
            "sync-pipeline", "--project", self.project, "--file", str(self.agents_md), "--dry-run",
        ])
        self.assertIn("--dry-run", proc.stdout)
        after = self.agents_md.read_bytes()
        self.assertEqual(before, after)

    def test_sync_pipeline_missing_board_stage_warns_and_leaves_line(self):
        # не импортируем доску вовсе — ни один этап не заведён; строки должны остаться нетронутыми
        boardlib.create_project(self.project, "фабрика SEO-страниц", str(self.project_dir), boards_dir=self.boards_dir)
        before = self.agents_md.read_bytes()
        proc = self.run_ok(["sync-pipeline", "--project", self.project, "--file", str(self.agents_md)])
        self.assertIn("карточки на доске нет", proc.stdout)
        after = self.agents_md.read_bytes()
        self.assertEqual(before, after)

    # -------------------------------------------------------- вспомогательное для sync-тестов

    def _expected_after_import(self):
        """Маркеры после свежего импорта по правилу спеки доски: [x]/[~]/[?] → «Выполнена» → [?] (импорт
        с 2026-09-29 ничего не принимает сам), [!] → «В работе» с доработкой → [!], [ ] → как было."""
        return {num: "?" for num, marker in self.original_markers.items() if marker in "x~?"}

    def _apply_markers(self, original_bytes, new_by_stage):
        """Ожидаемое содержимое файла после sync-pipeline — построено НЕ через board.py.

        Не вызывает board._desired_marker и не использует board.SYNC_MARK_RE: new_by_stage — то,
        каким тест сам считает, что должен стать маркер каждого этапа (по правилу из спеки доски:
        «Завершена» без оговорки → [x], с оговоркой → [~], «Выполнена» → [?], «В работе» с rework
        → [!], иначе [ ]), решённое читая сценарий теста, а не спрашивая реализацию. Подстановка —
        обычная замена подстроки «- [<старое>] <N> » на «- [<новое>] <N> ».
        """
        bom = original_bytes.startswith(b"\xef\xbb\xbf")
        text = original_bytes.decode("utf-8-sig")
        for num, old in self.original_markers.items():
            new = new_by_stage.get(num, old)
            if new == old:
                continue
            old_snippet = f"- [{old}] {num} "
            new_snippet = f"- [{new}] {num} "
            self.assertEqual(
                text.count(old_snippet), 1,
                f"строка этапа {num} не найдена или не единственна в фикстуре",
            )
            text = text.replace(old_snippet, new_snippet, 1)
        new_bytes = text.encode("utf-8")
        if bom:
            new_bytes = b"\xef\xbb\xbf" + new_bytes
        return new_bytes


if __name__ == "__main__":
    unittest.main()
