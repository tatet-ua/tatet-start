#!/usr/bin/env python3
"""Установник набору tatet-start: одна команда після `claude plugin install`.

Що робить:
  1. питає одне — диск (Windows) або підтвердження ~/project (macOS, Linux);
  2. створює <корінь>/DevOps (склад ключів .env і зразок .env.example) і <корінь>/llm-wiki (база знань зі схемою),
     існуючі файли не перезаписує, .env не чіпає ніколи; у llm-wiki робить git init (без першого коміту);
  3. пише змінні TATET_WIKI, TATET_DEVOPS, BOARD_DIR, BOARD_PORT у <конфіг Claude>/settings.json → "env"
     (решту файлу зберігає, перед записом робить резервну копію);
  4. дописує блок ритуалу сесії в <конфіг Claude>/CLAUDE.md між мітками (повторний запуск блок замінює, не дублює);
  5. створює команду `board` (обгортка в ~/.local/bin) і перевіряє, що дошка запускається.

Запуск:
  python scripts/setup.py                  питання з підказкою за замовчуванням
  python scripts/setup.py --yes            без питань, усе за замовчуванням
  python scripts/setup.py --root <шлях>    корінь project задано явно
  python scripts/setup.py --dry-run        показати, що буде зроблено, нічого не змінюючи
  python scripts/setup.py --home <шлях>    підмінити домашній каталог (для тестів; PATH у системі не змінюється)

Після кожного оновлення плагіна установник запускають ще раз: шлях до плагіна змінюється, обгортка board
переписується. Повторний запуск нічого не дублює. Код виходу: 0 — успіх, 1 — помилка.
Потрібен Python 3.10+, лише стандартна бібліотека.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shlex
import shutil
import string
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATES = PLUGIN_ROOT / "templates"
BLOCK_TEMPLATE = TEMPLATES / "claude-md-block.md"
BOARD_PY = PLUGIN_ROOT / "board" / "board.py"

BEGIN = "<!-- tatet-start:begin -->"
END = "<!-- tatet-start:end -->"
DEFAULT_PORT = "8790"
IS_WIN = os.name == "nt"


class SetupError(Exception):
    """Помилка, яку показуємо людині без трасування."""


def show(path: Path) -> str:
    return Path(path).as_posix()


class Log:
    """Друкує кроки одразу і збирає підсумок."""

    def __init__(self, dry: bool):
        self.dry = dry
        self.created: list[str] = []
        self.updated: list[str] = []
        self.skipped: list[str] = []
        self.manual: list[str] = []
        self.problems: list[str] = []

    def step(self, title: str) -> None:
        print(f"\n{title}")

    def create(self, what: str) -> None:
        self.created.append(what)
        print(f"  {'буде створено' if self.dry else 'створено'}: {what}")

    def update(self, what: str) -> None:
        self.updated.append(what)
        print(f"  {'буде оновлено' if self.dry else 'оновлено'}: {what}")

    def skip(self, what: str) -> None:
        self.skipped.append(what)
        print(f"  є, пропущено: {what}")

    def info(self, text: str) -> None:
        print(f"  {text}")

    def todo(self, text: str) -> None:
        if text not in self.manual:
            self.manual.append(text)

    def problem(self, text: str) -> None:
        self.problems.append(text)
        print(f"  ПРОБЛЕМА: {text}")


# ---------------------------------------------------------------- файли

def backup(path: Path) -> Path:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    target = path.with_name(f"{path.name}.bak-{stamp}")
    n = 2
    while target.exists():
        target = path.with_name(f"{path.name}.bak-{stamp}-{n}")
        n += 1
    shutil.copy2(path, target)
    return target


def write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-tatet-start")
    with open(tmp, "w", encoding=encoding, newline="") as f:
        f.write(text)
    os.replace(tmp, path)


# ---------------------------------------------------------------- корінь

def available_drives() -> list[str]:
    return [d for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]


def ask(prompt: str) -> str | None:
    """Відповідь користувача; None, якщо ввести нічого не можна (немає терміналу)."""
    try:
        return input(prompt).strip()
    except EOFError:
        print()
        return None


def choose_root(home: Path, assume_yes: bool) -> Path:
    if IS_WIN:
        drives = available_drives()
        default = (home.drive[:1] or "C").upper()
        if default not in drives and drives:
            default = drives[0]
        if assume_yes:
            print(f"Диск: {default} (за замовчуванням, --yes)")
            return Path(f"{default}:/project")
        print(f"Доступні диски: {', '.join(drives) or 'не знайдено'}")
        for _ in range(3):
            answer = ask(f"На якому диску створити папку project? [{default}]: ")
            if answer is None:
                print(f"Відповіді немає, беру {default}.")
                return Path(f"{default}:/project")
            letter = (answer.rstrip(":\\/ ") or default).upper()
            if len(letter) == 1 and letter in drives:
                return Path(f"{letter}:/project")
            print(f"Немає такого диска: {answer}. Введи одну літеру зі списку: {', '.join(drives)}.")
        raise SetupError("диск не вибрано; запусти ще раз або вкажи шлях: --root <диск>:/project")

    default_path = home / "project"
    if assume_yes:
        print(f"Папка project: {show(default_path)} (за замовчуванням, --yes)")
        return default_path
    answer = ask(f"Створити розкладку в {show(default_path)}? Enter — так, або введи інший шлях: ")
    if answer is None or answer.lower() in ("", "y", "yes", "т", "так"):
        return default_path
    if answer.lower() in ("n", "no", "н", "ні"):
        raise SetupError("скасовано; вкажи свій шлях: --root <шлях до папки project>")
    return Path(answer).expanduser().resolve()


# ---------------------------------------------------------------- settings.json

def load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise SetupError(f"{show(path)} — не JSON (рядок {e.lineno}, стовпчик {e.colno}): {e.msg}. "
                         "Виправ файл і запусти установник ще раз") from None
    if not isinstance(data, dict):
        raise SetupError(f"{show(path)} — очікувався об'єкт JSON {{...}}")
    env = data.get("env")
    if env is not None and not isinstance(env, dict):
        raise SetupError(f"{show(path)}: ключ \"env\" має бути об'єктом {{...}}")
    return data


def apply_settings(path: Path, data: dict, wanted: dict[str, str], log: Log) -> None:
    log.step(f"Змінні оточення: {show(path)}")
    env = dict(data.get("env") or {})
    changes = []
    for key, value in wanted.items():
        old = env.get(key)
        if old == value:
            log.info(f"{key} = {value} (вже є)")
            continue
        changes.append(f"{key}: {old} → {value}" if old is not None else f"{key} = {value}")
        env[key] = value
    if not changes:
        log.skip("settings.json: усі чотири змінні вже на місці")
        return
    for line in changes:
        log.info(line)
    existed = path.exists()
    if log.dry:
        (log.update if existed else log.create)("settings.json (env)")
        return
    if existed:
        log.info(f"резервна копія: {show(backup(path))}")
    data["env"] = env
    write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    (log.update if existed else log.create)(f"{show(path)} (env)")


# ---------------------------------------------------------------- CLAUDE.md

def read_block() -> str:
    block = BLOCK_TEMPLATE.read_text(encoding="utf-8").strip()
    if not (block.startswith(BEGIN) and block.endswith(END)):
        raise SetupError(f"шаблон {show(BLOCK_TEMPLATE)} пошкоджено: немає міток {BEGIN} … {END}")
    return block


def check_claude_md(path: Path) -> None:
    """До будь-яких змін: зламані мітки зупиняють установку, поки нічого не створено."""
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8-sig")
    begins, ends = text.count(BEGIN), text.count(END)
    if (begins, ends) == (0, 0) or ((begins, ends) == (1, 1) and text.index(BEGIN) < text.index(END)):
        return
    raise SetupError(f"у {show(path)} мітки tatet-start зламані (початків {begins}, кінців {ends}). "
                     f"Залиш одну пару {BEGIN} … {END} або прибери обидві й запусти ще раз")


def apply_claude_md(path: Path, log: Log) -> None:
    log.step(f"Ритуал сесії: {show(path)}")
    block = read_block()
    if not path.exists():
        if not log.dry:
            write_text(path, block + "\n")
        log.create(f"{show(path)} (блок tatet-start)")
        return
    with open(path, encoding="utf-8-sig", newline="") as f:
        text = f.read()
    eol = "\r\n" if "\r\n" in text else "\n"
    block_eol = block.replace("\n", eol)
    begins, ends = text.count(BEGIN), text.count(END)
    if begins == 0 and ends == 0:
        new = text
        if new and not new.endswith(("\n", "\r")):
            new += eol
        if new.strip():
            new += eol
        new += block_eol + eol
    elif begins == 1 and ends == 1 and text.index(BEGIN) < text.index(END):
        b, e = text.index(BEGIN), text.index(END) + len(END)
        new = text[:b] + block_eol + text[e:]
    else:
        raise SetupError(f"у {show(path)} мітки tatet-start зламані (початків {begins}, кінців {ends}). "
                         f"Залиш одну пару {BEGIN} … {END} або прибери обидві й запусти ще раз")
    if new == text:
        log.skip("CLAUDE.md: блок tatet-start актуальний")
        return
    if not log.dry:
        log.info(f"резервна копія: {show(backup(path))}")
        write_text(path, new)
    log.update(f"{show(path)} (блок tatet-start {'замінено' if begins else 'дописано в кінець'})")


# ---------------------------------------------------------------- розкладка

def copy_tree(src: Path, dst: Path, log: Log) -> None:
    for folder, dirs, files in os.walk(src):
        dirs.sort()
        rel = Path(folder).relative_to(src)
        for name in sorted(files):
            source = Path(folder) / name
            target = dst / rel / name
            if target.exists():
                log.skip(show(target))
                continue
            if not log.dry:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            log.create(show(target))


def make_env_file(devops: Path, log: Log) -> None:
    target = devops / ".env"
    if target.exists():
        log.skip(f"{show(target)} (склад ключів не чіпаю)")
        return
    if not log.dry:
        text = (TEMPLATES / "DevOps" / ".env.example").read_text(encoding="utf-8")
        write_text(target, text)
        if not IS_WIN:
            os.chmod(target, 0o600)
    log.create(f"{show(target)} (порожній, лише коментарі)")


def git_init(wiki: Path, log: Log) -> None:
    if (wiki / ".git").exists():
        log.skip(f"{show(wiki)}: git-репозиторій уже є")
        return
    git = shutil.which("git")
    if not git:
        log.info("git не знайдено: базу не зроблено git-репозиторієм")
        log.todo(f"Встанови git і виконай в {show(wiki)}: git init")
        return
    if not log.dry:
        r = subprocess.run([git, "init", "-q", str(wiki)], capture_output=True, text=True)
        if r.returncode != 0:
            log.problem(f"git init завершився з кодом {r.returncode}: {(r.stderr or r.stdout).strip()}")
            return
    log.create(f"{show(wiki)}: git-репозиторій (без першого коміту)")


# ---------------------------------------------------------------- команда board

def wrapper_content(env_vars: dict[str, str]) -> tuple[Path, bytes]:
    """Ім'я файлу обгортки і її вміст у байтах.

    Змінні з settings.json бачить лише Claude Code, а не звичайний термінал, тому обгортка сама задає
    BOARD_DIR, TATET_WIKI, TATET_DEVOPS, BOARD_PORT, якщо їх не задано в оточенні."""
    board = str(BOARD_PY)
    if IS_WIN:
        lines = ["@echo off", "setlocal", "set PYTHONIOENCODING=utf-8"]
        lines += [f'if not defined {k} set "{k}={v}"' for k, v in env_vars.items()]
        lines.append(f'python "{board}" %*')
        try:
            # cmd читає .cmd у кодовій сторінці консолі (OEM); шлях з кирилицею в UTF-8 зламався б
            return Path("board.cmd"), ("\r\n".join(lines) + "\r\n").encode("oem")
        except (UnicodeEncodeError, LookupError):
            lines.insert(1, "chcp 65001 >nul")
            return Path("board.cmd"), ("\r\n".join(lines) + "\r\n").encode("utf-8")
    python = "python3" if shutil.which("python3") else "python"
    lines = ["#!/bin/sh"]
    lines += [f'[ -n "${{{k}:-}}" ] || {k}={shlex.quote(v)}; export {k}' for k, v in env_vars.items()]
    lines.append(f'exec {python} {shlex.quote(board)} "$@"')
    text = "\n".join(lines) + "\n"
    return Path("board"), text.encode("utf-8")


def make_wrapper(bin_dir: Path, env_vars: dict[str, str], log: Log) -> Path:
    log.step("Команда board")
    name, content = wrapper_content(env_vars)
    target = bin_dir / name
    if target.exists() and target.read_bytes() == content and (IS_WIN or os.access(target, os.X_OK)):
        log.skip(f"{show(target)} (вже веде на {show(BOARD_PY)})")
        return target
    existed = target.exists()
    if not log.dry:
        bin_dir.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        if not IS_WIN:
            os.chmod(target, 0o755)
    (log.update if existed else log.create)(f"{show(target)} → {show(BOARD_PY)}")
    return target


def same_dir(a: str, b: Path) -> bool:
    a = os.path.expandvars(a.strip().strip('"'))
    if not a:
        return False
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(str(b)))


def in_process_path(bin_dir: Path) -> bool:
    return any(same_dir(p, bin_dir) for p in os.environ.get("PATH", "").split(os.pathsep))


def ensure_path(bin_dir: Path, test_home: bool, log: Log) -> None:
    if in_process_path(bin_dir):
        log.info(f"{show(bin_dir)} уже в PATH")
        return
    if not IS_WIN:
        log.info(f"{show(bin_dir)} немає в PATH")
        log.todo(f'Додай в ~/.zshrc або ~/.bashrc рядок: export PATH="{show(bin_dir)}:$PATH" '
                 "і перезапусти термінал")
        return
    if test_home:
        log.info(f"{show(bin_dir)} немає в PATH; запуск з --home — системний PATH не змінюю")
        log.todo(f"Додай {bin_dir} у PATH користувача (запуск з --home змінює лише тестову папку)")
        return
    import winreg  # лише Windows

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                        winreg.KEY_READ | winreg.KEY_WRITE) as key:
        try:
            current, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current, kind = "", winreg.REG_EXPAND_SZ
        entries = [p for p in str(current).split(";") if p.strip()]
        if any(same_dir(p, bin_dir) for p in entries):
            log.info(f"{bin_dir} уже в PATH користувача")
            log.todo("Перезапусти термінал, щоб команда board стала доступною")
            return
        if log.dry:
            log.update(f"PATH користувача: + {bin_dir}")
            return
        entries.append(str(bin_dir))
        if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
            kind = winreg.REG_EXPAND_SZ
        winreg.SetValueEx(key, "Path", 0, kind, ";".join(entries))
    try:  # повідомити Провідник, щоб нові термінали побачили PATH
        import ctypes
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, None)
    except Exception:
        pass
    log.update(f"PATH користувача: + {bin_dir}")
    log.todo("Перезапусти термінал, щоб команда board стала доступною")


def check_board(env_vars: dict[str, str], log: Log) -> None:
    log.step("Перевірка дошки")
    if not BOARD_PY.exists():
        log.info("дошка: ще не встановлена, пропущено")
        return
    if log.dry:
        log.info("дошка: у пробному запуску не перевіряю")
        return
    env = os.environ.copy()
    env.update(env_vars)
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # не залишати __pycache__ у папці плагіна
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        r = subprocess.run([sys.executable, str(BOARD_PY), "--help"], capture_output=True,
                           env=env, timeout=60, cwd=str(PLUGIN_ROOT))
    except subprocess.TimeoutExpired:
        log.problem("дошка: board.py --help не відповів за 60 секунд")
        return
    if r.returncode == 0:
        log.info("дошка: ок")
    else:
        tail = r.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
        log.problem(f"дошка: board.py --help завершився з кодом {r.returncode}: {' | '.join(tail)}")


def python_problem() -> str | None:
    """Хуки плагіна викликають `python` з PATH: команда має бути і запускати Python 3.10+.
    Повертає текст проблеми з інструкцією або None, якщо все гаразд."""
    if IS_WIN:
        how = ("встанови Python 3.10+ з python.org і в установнику познач «Add python.exe to PATH», "
               "потім перезапусти термінал")
    else:
        how = ('створи симлінк: ln -s "$(command -v python3)" ~/.local/bin/python '
               "(або alias python=python3 у профілі), чи встанови Python 3.10+, де є команда python")
    exe = shutil.which("python")
    if not exe:
        return f"команди python немає в PATH, а хуки плагіна викликають саме її.\nЩо зробити: {how}"
    try:
        r = subprocess.run([exe, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                           capture_output=True, text=True, timeout=30)
        version = r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        version = ""
    parts = version.split(".")
    if len(parts) == 2 and all(x.isdigit() for x in parts) and (int(parts[0]), int(parts[1])) >= (3, 10):
        print(f"python: ок ({version}, {show(Path(exe))})")
        return None
    return (f"команда python ({show(Path(exe))}) не запускає Python 3.10+ (отримано: {version or 'нічого'}).\n"
            f"Що зробити: {how}")


# ---------------------------------------------------------------- головне

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="setup.py",
        description="Установник набору tatet-start: розкладка project/, змінні, ритуал сесії, команда board.")
    p.add_argument("--root", help="корінь project (за замовчуванням <диск>:/project або ~/project)")
    p.add_argument("--home", help="підмінити домашній каталог (для тестів; системний PATH не змінюється)")
    p.add_argument("--yes", action="store_true", help="без питань, усе за замовчуванням")
    p.add_argument("--dry-run", action="store_true", help="показати, що буде зроблено, нічого не змінюючи")
    return p.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    if sys.version_info < (3, 10):
        raise SetupError(f"потрібен Python 3.10 або новіший, зараз {sys.version.split()[0]}")
    if not (TEMPLATES / "llm-wiki").is_dir() or not BLOCK_TEMPLATE.is_file():
        raise SetupError(f"не знайдено шаблонів у {show(TEMPLATES)}; перевстанови плагін tatet-start")

    log = Log(args.dry_run)
    test_home = bool(args.home)
    home = Path(args.home).expanduser().resolve() if test_home else Path.home()
    if test_home:
        config = home / ".claude"
        if os.environ.get("CLAUDE_CONFIG_DIR"):
            print("CLAUDE_CONFIG_DIR ігнорую: задано --home")
    else:
        config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude").expanduser()
    settings_path = config / "settings.json"

    print("Установник tatet-start" + (" — пробний запуск, нічого не змінюю" if log.dry else ""))
    print(f"Плагін: {show(PLUGIN_ROOT)}")
    print(f"Конфіг Claude: {show(config)}")

    problem = python_problem()  # першою: без python хуки плагіна не працюють, тож нічого не створюємо
    if problem:
        print(f"\nПомилка: {problem}\nУстановку зупинено, нічого не створено. Після виправлення запусти ще раз.")
        return 1

    settings = load_settings(settings_path)  # зламаний файл зупиняє установку до будь-яких змін
    check_claude_md(config / "CLAUDE.md")
    read_block()
    old_env = settings.get("env") or {}

    if args.root:
        root = Path(args.root).expanduser().resolve()
        wiki, devops = root / "llm-wiki", root / "DevOps"
        board_dir = wiki / "boards"
    elif old_env.get("TATET_WIKI"):
        wiki = Path(old_env["TATET_WIKI"]).expanduser()
        devops = Path(old_env.get("TATET_DEVOPS") or wiki.parent / "DevOps").expanduser()
        if devops.name != "DevOps":  # захист складу ключів розпізнає лише цю назву
            print(f"TATET_DEVOPS={show(devops)} — назва папки не DevOps, беру {show(wiki.parent / 'DevOps')}")
            devops = wiki.parent / "DevOps"
        board_dir = Path(old_env.get("BOARD_DIR") or wiki / "boards").expanduser()
        print(f"Шляхи вже задані в settings.json, беру їх (інший корінь — ключ --root)")
    else:
        root = choose_root(home, args.yes)
        wiki, devops = root / "llm-wiki", root / "DevOps"
        board_dir = wiki / "boards"

    print(f"База знань: {show(wiki)}")
    print(f"Склад ключів: {show(devops)}")

    wanted = {
        "TATET_WIKI": show(wiki),
        "TATET_DEVOPS": show(devops),
        "BOARD_DIR": show(board_dir),
        "BOARD_PORT": str(old_env.get("BOARD_PORT") or DEFAULT_PORT),
    }

    log.step(f"Склад ключів: {show(devops)}")
    copy_tree(TEMPLATES / "DevOps", devops, log)
    make_env_file(devops, log)

    log.step(f"База знань: {show(wiki)}")
    copy_tree(TEMPLATES / "llm-wiki", wiki, log)
    git_init(wiki, log)

    apply_settings(settings_path, settings, wanted, log)
    apply_claude_md(config / "CLAUDE.md", log)

    bin_dir = home / ".local" / "bin"
    make_wrapper(bin_dir, wanted, log)
    ensure_path(bin_dir, test_home, log)

    check_board(wanted, log)

    summary(log, wiki)
    return 1 if log.problems else 0


def summary(log: Log, wiki: Path) -> None:
    print("\n" + "=" * 60)
    print("Підсумок" + (" пробного запуску (нічого не змінено)" if log.dry else ""))
    changed = log.created or log.updated
    if not changed:
        print("  Усе вже на місці, нічого не змінено.")
    if log.created:
        print(f"  {'Буде створено' if log.dry else 'Створено'} ({len(log.created)}):")
        for item in log.created:
            print(f"    + {item}")
    if log.updated:
        print(f"  {'Буде оновлено' if log.dry else 'Оновлено'} ({len(log.updated)}):")
        for item in log.updated:
            print(f"    ~ {item}")
    if log.skipped:
        print(f"  Пропущено, бо вже є: {len(log.skipped)}")
    if log.problems:
        print(f"  Проблеми ({len(log.problems)}):")
        for item in log.problems:
            print(f"    ! {item}")

    print("  Склад ключів — папка з назвою рівно DevOps: за цією назвою захист не дає моделі читати .env; "
          "не перейменовуй її.")

    steps = list(log.manual)
    if changed and not log.dry:
        steps.append("Перезапусти термінал, щоб підхопилися змінні оточення і команда board")
    steps += [
        "Відкрий папку свого проєкту в терміналі й запусти у ній claude",
        "Перша сесія — твоя справжня задача; наприкінці скажи «завершуємо», агент запише лог у базу",
        "Після оновлення плагіна запусти установник ще раз: він перепише обгортку board",
    ]
    seen = []
    for s in steps:
        if s not in seen:
            seen.append(s)
    print("  Зробити руками:")
    for i, s in enumerate(seen, 1):
        print(f"    {i}. {s}")


def main(argv: list[str] | None = None) -> int:
    try:
        # UTF-8 і в консолі, і в конвеєрі (агент читає вивід як UTF-8; кодова сторінка Windows зіпсувала б кирилицю)
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return run(args)
    except SetupError as e:
        print(f"\nПомилка: {e}", file=sys.stderr)
    except KeyboardInterrupt:
        print("\nПерервано.", file=sys.stderr)
    except OSError as e:
        print(f"\nПомилка файлової системи: {e.strerror or e} ({e.filename or ''})", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 — показати людині, а не трасування
        print(f"\nНеочікувана помилка: {type(e).__name__}: {e}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
