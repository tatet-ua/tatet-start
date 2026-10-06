"""Помічник для складу ключів (файл DevOps/.env): сесія бачить імена змінних, значення — ніколи.
Рішення від 2026-09-19: файл ключів читають скрипти, а не модель.

  python with-env.py --names                      імена змінних, по одному в рядку
  python with-env.py --has NAME [NAME ...]        код 0, якщо всі задані й непорожні; інакше друкує, яких немає
  python with-env.py [--only A,B] -- <команда>    запускає команду з ключами в оточенні

Для конвеєрів і підстановки змінних — через оболонку:
  python with-env.py --only CLOUDFLARE_API_TOKEN -- bash -c 'curl -s -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" https://...'

Вивід команди проходить через маску: будь-яке значення з файлу (від 6 символів) замінюється на ‹ЗМІННА›. Маска ловить
значення як є; закодоване (base64, URL-кодування) не зловить — не виводити ключі в перетвореному вигляді.

Де лежить склад: змінна TATET_DEVOPS (папка DevOps); якщо її немає — папка DevOps поруч із базою знань TATET_WIKI;
якщо немає й її — <диск>:/project/DevOps (Windows, диски C..Z) або ~/project/DevOps (інші системи).
--file <шлях> — інший env-файл замість складу."""
import os
import subprocess
import sys

MIN_MASK_LEN = 6

NOT_FOUND = ("with-env: не знайдено папку DevOps зі складом ключів. Запусти установник набору "
             "(scripts/setup.py з папки плагіна tatet-start) або задай змінну TATET_DEVOPS "
             "(шлях до папки DevOps) чи TATET_WIKI (шлях до llm-wiki, DevOps лежить поруч).")


def default_env_file():
    """Шлях до файлу складу ключів; None, якщо папку DevOps не знайдено."""
    explicit = os.environ.get("TATET_DEVOPS")
    if explicit:
        return os.path.join(explicit, ".env")
    wiki = os.environ.get("TATET_WIKI")
    if wiki:
        return os.path.join(os.path.dirname(os.path.normpath(wiki)), "DevOps", ".env")
    if os.name == "nt":
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            folder = f"{letter}:/project/DevOps"
            if os.path.isdir(folder):
                return folder + "/.env"
        return None
    folder = os.path.expanduser("~/project/DevOps")
    return folder + "/.env" if os.path.isdir(folder) else None


def parse_env(path):
    values = {}
    with open(path, encoding="utf-8-sig") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            elif " #" in value:
                value = value.split(" #", 1)[0].rstrip()
            if key:
                values[key] = value
    return values


def mask(text, secrets):
    for name, value in secrets:
        text = text.replace(value, f"‹{name}›")
    return text


def main(argv):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    env_file, only, names_mode, has_mode = None, None, False, None
    args = list(argv)
    command = []
    while args:
        arg = args.pop(0)
        if arg == "--":
            command = args
            break
        if arg == "--names":
            names_mode = True
        elif arg == "--has":
            has_mode = []
            while args and args[0] != "--":
                has_mode.append(args.pop(0))
        elif arg == "--only" and args:
            only = [n.strip() for n in args.pop(0).split(",") if n.strip()]
        elif arg == "--file" and args:
            env_file = args.pop(0)
        else:
            print(f"with-env: невідомий аргумент {arg!r}; опис на початку файлу", file=sys.stderr)
            return 2

    if env_file is None:
        env_file = default_env_file()
        if env_file is None:
            print(NOT_FOUND, file=sys.stderr)
            return 2

    try:
        values = parse_env(env_file)
    except OSError as err:
        print(f"with-env: не вдалося відкрити {env_file}: {err.strerror}. "
              "Перевір TATET_DEVOPS або запусти установник набору.", file=sys.stderr)
        return 2

    if names_mode:
        for name in sorted(values):
            print(name if values[name] else f"{name}  (порожньо)")
        return 0

    if has_mode is not None:
        missing = [n for n in has_mode if not values.get(n)]
        if missing:
            print("немає або порожньо: " + ", ".join(missing))
            return 1
        print("усі задані: " + ", ".join(has_mode))
        return 0

    if not command:
        print("with-env: потрібна команда після «--» або --names / --has", file=sys.stderr)
        return 2

    if only is not None:
        absent = [n for n in only if n not in values]
        if absent:
            print("with-env: у файлі немає змінних: " + ", ".join(absent), file=sys.stderr)
            return 2
        values = {n: values[n] for n in only}

    # Маскуємо всі значення файлу, а не лише передані: команда може дістати ключ іншим шляхом.
    all_values = parse_env(env_file)
    secrets = sorted(((n, v) for n, v in all_values.items() if len(v) >= MIN_MASK_LEN),
                     key=lambda item: len(item[1]), reverse=True)

    env = dict(os.environ)
    env.update(values)
    try:
        proc = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", bufsize=1)
    except OSError as err:
        print(f"with-env: не вдалося запустити {command[0]!r}: {err.strerror}", file=sys.stderr)
        return 127
    for line in proc.stdout:
        sys.stdout.write(mask(line, secrets))
        sys.stdout.flush()
    return proc.wait()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
