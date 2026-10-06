"""Проверки textcheck: из папки board — python -m unittest discover -s tests -p "test_textcheck.py".

Папка tests — не пакет (нет __init__.py) — путь к папке board добавляется в
sys.path вручную, чтобы `import textcheck` работал независимо от того, откуда запущен unittest.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import textcheck  # noqa: E402


class ForbiddenPatterns(unittest.TestCase):
    """Перенесённые случаи из тестов прежнего сборщика доски-артефакта (удалён 2026-09-24)
    плюс шаблоны из плана 1.1."""

    # --- отклонены ---

    def test_commit_hash_rejected(self):
        errors = textcheck.check_text("коммит a1b2c3d4e5f6a7b8 в основной ветке", "body")
        self.assertEqual(len(errors), 1)
        self.assertIn("ОШИБКА  body:", errors[0])
        self.assertIn("хеш коммита", errors[0])

    def test_uuid_rejected(self):
        # Без цифр и строчных hex-букв — иначе первым по порядку в FORBIDDEN сработает
        # «хеш коммита» (внутри группы из 8 или 12 hex-символов, как в настоящем uuid).
        errors = textcheck.check_text("ресурс AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA создан", "body")
        self.assertEqual(len(errors), 1)
        self.assertIn("ОШИБКА  body:", errors[0])
        self.assertIn("uuid", errors[0])

    def test_resource_id_rejected(self):
        errors = textcheck.check_text("приложение vjeuupeztqkwuvkoiglrmmm1 создано", "body")
        self.assertEqual(len(errors), 1)
        self.assertIn("идентификатор ресурса", errors[0])

    def test_windows_path_rejected(self):
        errors = textcheck.check_text("поправить X:/project/demo/app", "body")
        self.assertEqual(len(errors), 1)
        self.assertIn("путь к файлу", errors[0])

    def test_unix_path_rejected(self):
        errors = textcheck.check_text("файл лежит в ~/project/board/data", "body")
        self.assertEqual(len(errors), 1)
        self.assertIn("путь к файлу", errors[0])

    def test_filename_rejected(self):
        errors = textcheck.check_text("дописать блок в CLAUDE.md проекта", "body")
        self.assertEqual(len(errors), 1)
        self.assertIn("имя файла", errors[0])

    # --- проходят ---

    def test_domain_passes(self):
        self.assertEqual(textcheck.check_text("сервис на demo.example.com", "body"), [])

    def test_date_passes(self):
        self.assertEqual(textcheck.check_text("сделано 2026-09-19", "body"), [])

    def test_version_passes(self):
        self.assertEqual(textcheck.check_text("версия набора 1.6.0", "body"), [])

    def test_web_address_with_scheme_passes(self):
        # Отличие от оригинала: «https://» не читается как диск «s:/» — accept_how называет адрес страницы.
        for s in ("Открой https://demo-new.example.com/settings и нажми «Сохранить»",
                  "Скрипт лежит на https://example.com/app/main.js",
                  "Локально: http://localhost:8790/health"):
            self.assertEqual(textcheck.check_text(s, "accept_how"), [], s)
        self.assertTrue(textcheck.check_text("Лежит в X:/project/demo", "body"))
        self.assertTrue(textcheck.check_text("Лежит в (X:\\project)", "body"))

    def test_web_address_with_js_passes(self):
        # DOMAIN_HEAD: «сайт.зона/путь/файл.js» — адрес в сети, а не путь в проекте.
        # Ровно случай из оригинального test_web_address_is_not_a_file_path: домен без схемы
        # (http:// перед доменом даёт другую, тоже унаследованную из оригинала, ложную ловушку —
        # см. отчёт).
        errors = textcheck.check_text("Посмотреть виджет на cdn.jsdelivr.net/npm/vue.js", "body")
        self.assertEqual(errors, [])

    def test_plain_russian_text_passes(self):
        errors = textcheck.check_text("Сделали вход через Google и проверили руками.", "body")
        self.assertEqual(errors, [])


class LengthLimit(unittest.TestCase):
    def test_over_limit_reports_length_and_limit(self):
        errors = textcheck.check_text("а" * 10, "title", limit=5)
        self.assertEqual(len(errors), 1)
        self.assertIn("ОШИБКА  title:", errors[0])
        self.assertIn("10", errors[0])
        self.assertIn("5", errors[0])

    def test_within_limit_is_fine(self):
        self.assertEqual(textcheck.check_text("коротко", "title", limit=120), [])

    def test_no_limit_means_no_length_check(self):
        self.assertEqual(textcheck.check_text("а" * 10000, "title"), [])


class AllowPaths(unittest.TestCase):
    def test_allow_paths_lets_path_through(self):
        errors = textcheck.check_text(
            "открой X:/project/llm-wiki/board и файл textcheck.py",
            "resume",
            allow_paths=True,
        )
        self.assertEqual(errors, [])

    def test_allow_paths_still_rejects_hash(self):
        errors = textcheck.check_text(
            "коммит a1b2c3d4e5f6a7b8 готов, папка X:/project/llm-wiki",
            "resume",
            allow_paths=True,
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("хеш коммита", errors[0])


class CheckFields(unittest.TestCase):
    def test_resume_with_path_has_no_errors(self):
        errors = textcheck.check_fields({
            "resume": "Открой X:/project/llm-wiki/board, там textcheck.py и README.md.",
        })
        self.assertEqual(errors, [])

    def test_body_with_path_fails(self):
        errors = textcheck.check_fields({
            "body": "Поправить X:/project/llm-wiki/board/textcheck.py",
        })
        self.assertEqual(len(errors), 1)
        self.assertIn("ОШИБКА  body:", errors[0])

    def test_empty_fields_are_skipped(self):
        errors = textcheck.check_fields({"title": "", "body": None, "ask": "   "})
        self.assertEqual(errors, [])

    def test_unknown_field_has_no_length_limit(self):
        errors = textcheck.check_fields({"tag": "а" * 10000})
        self.assertEqual(errors, [])

    def test_known_field_uses_its_limit(self):
        errors = textcheck.check_fields({"title": "а" * 200})
        self.assertEqual(len(errors), 1)
        self.assertIn("ОШИБКА  title:", errors[0])
        self.assertIn(str(textcheck.LIMITS["title"]), errors[0])

    def test_project_path_limits(self):
        # Путь проекта (план 2026-09-24, A2): шесть полей и цитата владельца.
        expected = {
            "sprint_title": 80, "done_when": 200, "sprint_proof": 400, "fog": 120,
            "finish": 120, "start_event": 80, "owner_said": 300,
        }
        for field, limit in expected.items():
            with self.subTest(field=field):
                self.assertEqual(textcheck.LIMITS[field], limit)
                self.assertEqual(textcheck.check_fields({field: "а" * limit}), [])
                errors = textcheck.check_fields({field: "а" * (limit + 1)})
                self.assertEqual(len(errors), 1)
                self.assertIn(f"ОШИБКА  {field}:", errors[0])


class VerifiedOutput(unittest.TestCase):
    """Вывод проверяющего (спека 2026-09-29, пункт 8): секреты — отказ, пути и хеши разрешены, предел."""

    def test_secrets_rejected(self):
        cases = {
            "Authorization:": "HTTP/2 200\nAuthorization: Basic dXNlcg==\n",
            "authorization в нижнем регистре": "authorization: bearer x\n",
            "Bearer ": "x-auth: Bearer eyJhbGciOi\n",
            "token=": "https://a.b/?token=abc\n",
            "key=": "api_key=12345\n",
            "password": '{"password": "секрет"}\n',
            "sk-…": "OPENAI sk-abcdefghijklmnopqrstuvwxyz\n",
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                errors = textcheck.check_output(text, "output")
                self.assertEqual(len(errors), 1)
                self.assertIn("ОШИБКА  output:", errors[0])
                self.assertIn("секрет", errors[0])

    def test_paths_hashes_and_ids_allowed(self):
        text = ("HTTP/2 200\nserver: nginx\ncommit a1b2c3d4e5f6a7b8\nX:/project/x/y.json\n"
                "id vjeuupeztqkwuvkoiglrmmm1\nuuid AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA\n~/x\n")
        self.assertEqual(textcheck.check_output(text, "output"), [])
        self.assertEqual(textcheck.check_output("", "output"), [])
        self.assertEqual(textcheck.check_output("монитор tokens: 5, keys: 3", "output"), [])  # без «=»

    def test_trim_output_limit_and_mark(self):
        text, truncated = textcheck.trim_output("a" * textcheck.OUTPUT_LIMIT)
        self.assertEqual((len(text), truncated), (textcheck.OUTPUT_LIMIT, False))
        text, truncated = textcheck.trim_output("b" * (textcheck.OUTPUT_LIMIT + 1))
        self.assertTrue(truncated)
        self.assertTrue(text.endswith("\n" + textcheck.OUTPUT_TRUNCATED_MARK))
        self.assertEqual(len(text), textcheck.OUTPUT_LIMIT + 1 + len(textcheck.OUTPUT_TRUNCATED_MARK))
        self.assertEqual(textcheck.OUTPUT_LIMIT, 4000)

    def test_verify_command_limit(self):
        self.assertEqual(textcheck.LIMITS["verify_command"], 600)


if __name__ == "__main__":
    unittest.main()
