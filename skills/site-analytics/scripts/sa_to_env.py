"""Перенос ключа сервисного аккаунта Google в .env проекта дашборда.

Значения не печатает. Файл ключа переносит в secrets/ рядом с .env.
    python sa_to_env.py <ключ.json> <путь к .env> --property <номер ресурса GA4> --domain <домен>
Пишет (добавляет или заменяет): GOOGLE_SA_CLIENT_EMAIL, GOOGLE_SA_PRIVATE_KEY (одной строкой с \\n),
GA4_PROPERTY_ID, GSC_SITE_URL=sc-domain:<домен>.
"""
import argparse
import json
import pathlib
import shutil
import sys

BS = chr(92)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser()
    p.add_argument("key_json")
    p.add_argument("env_path")
    p.add_argument("--property", required=True)
    p.add_argument("--domain", required=True)
    a = p.parse_args()

    key_path = pathlib.Path(a.key_json)
    env_path = pathlib.Path(a.env_path)
    data = json.loads(key_path.read_text(encoding="utf-8"))
    if data.get("type") != "service_account":
        sys.exit("ОШИБКА: это не ключ сервисного аккаунта")

    # private_key в .env — одной строкой, переводы строк как два символа \n
    private_key = data["private_key"].replace("\r", "").replace("\n", BS + "n")
    values = {
        "GOOGLE_SA_CLIENT_EMAIL": data["client_email"],
        "GOOGLE_SA_PRIVATE_KEY": '"' + private_key + '"',
        "GA4_PROPERTY_ID": a.property.strip(),
        "GSC_SITE_URL": "sc-domain:" + a.domain.strip().lower(),
    }

    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    seen = set()
    out = []
    for line in lines:
        name = line.split("=", 1)[0].strip()
        if name in values:
            out.append(f"{name}={values[name]}")
            seen.add(name)
        else:
            out.append(line)
    out += [f"{n}={v}" for n, v in values.items() if n not in seen]
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")

    secrets = env_path.parent / "secrets"
    secrets.mkdir(exist_ok=True)
    target = secrets / key_path.name
    if key_path.resolve() != target.resolve():
        shutil.move(str(key_path), target)

    gitignore = env_path.parent / ".gitignore"
    ignored = gitignore.read_text(encoding="utf-8").splitlines() if gitignore.exists() else []
    missing = [x for x in (".env", "secrets/") if x not in ignored]
    if missing:
        gitignore.write_text("\n".join(ignored + missing) + "\n", encoding="utf-8")

    print("OK: в .env записаны", ", ".join(values), "| ключ перенесён в", target.parent.name + "/")
    print("Почта сервисного аккаунта (её выдают в GA4 и Search Console):", data["client_email"])


if __name__ == "__main__":
    main()
