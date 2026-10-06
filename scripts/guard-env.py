"""PreToolUse-хук: склад ключів DevOps/.env модель не читає.
Рішення власника проєкту від 2026-09-19: файл читають скрипти, сесія бачить лише імена змінних.

Забороняє Read / Grep / Edit / Write по цьому файлу і команди оболонки, які виводять його вміст.
Пропускає форми, де значення не потрапляє у вивід:
  - запуск через помічник with-env.py (лежить поруч із цим скриптом);
  - ЗМІННА=$(... DevOps/.env ...)        значення йде у змінну оболонки;
  - source / . файл, --env-file файл, ЗМІННА_FILE=файл   файл передають програмі, а не друкують.
`.env.example` не чіпає. Інші виклики не чіпає (порожній вивід, код 0).
Захист спрацьовує за назвою папки: склад має лежати в папці з назвою DevOps.
Це другий рубіж, а не гарантія: `echo $TOKEN` після дозволеної форми він не зловить — для цього є маска у with-env.py."""
import json
import os
import re
import sys

try:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")  # інакше на Windows причина відмови піде в cp1251
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(0)

tool = str(payload.get("tool_name") or "")
tool_input = payload.get("tool_input") or {}

# Шлях до складу в будь-якому записі (з зворотними або прямими слешами), а також `cd …/DevOps && cat .env`.
ENV_PATH = r"devops/\.env(?!\.example)(?![\w-])"
ENV_AFTER_CD = r"devops\b[^\n]*?(?<![\w/.-])\.env(?!\.example)(?![\w.-])"

HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "with-env.py").replace("\\", "/")


def config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def norm(text):
    return str(text).replace("\\", "/").lower()


def touches(text):
    text = norm(text)
    return bool(re.search(ENV_PATH, text) or re.search(ENV_AFTER_CD, text))


def deny(reason):
    try:
        import datetime
        with open(os.path.join(config_dir(), "hook-log.txt"), "a", encoding="utf-8") as log:
            log.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} guard-env: {tool} заблоковано\n")
    except Exception:
        pass
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }, ensure_ascii=False))
    sys.exit(0)


HOW = ("Склад ключів модель не читає (рішення від 2026-09-19). Імена змінних: "
       f"python \"{HELPER}\" --names. Запуск із ключами: "
       f"python \"{HELPER}\" --only ЗМІННА -- <команда> "
       "(для конвеєрів: -- bash -c '…$ЗМІННА…'). Новий ключ у файл вносить власник вручну. "
       "Без виводу значення хук пропускає: ЗМІННА=$(grep … DevOps/.env | …), source файл, --env-file файл, "
       "ЗМІННА_FILE=файл.")

if tool in ("Read", "Edit", "Write", "NotebookEdit"):
    if touches(tool_input.get("file_path") or tool_input.get("notebook_path") or ""):
        deny(HOW)
    sys.exit(0)

if tool == "Grep":
    target = f"{tool_input.get('path') or ''} {tool_input.get('glob') or ''}"
    if touches(target):
        deny(HOW)
    sys.exit(0)

if tool in ("Bash", "PowerShell"):
    command = norm(tool_input.get("command") or "")
    if not touches(command):
        sys.exit(0)
    if "with-env.py" in command:
        sys.exit(0)
    safe_forms = [
        r"\b\w+=\$\([^()]*\)",                                  # ЗМІННА=$( … )
        r"(?:^|[;&|\s])(?:source|\.)\s+[\"']?\S*devops/\.env[\"']?",   # source файл
        r"--env-file[=\s]+[\"']?\S*devops/\.env[\"']?",          # --env-file файл
        r"\b\w*file\w*=[\"']?\S*devops/\.env[\"']?",             # ENV_FILE=файл
    ]
    rest = command
    for form in safe_forms:
        rest = re.sub(form, " ", rest)
    if touches(rest):
        deny(HOW)
    sys.exit(0)

sys.exit(0)
