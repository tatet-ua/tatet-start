"""Секреты из .env проекта в переменные сайта на хостинге менторства, без вывода значений.

Ключ студента mnt_… берётся из окружения MENTOR_KEY — запускать через склад ключей:
    python <набор>/scripts/with-env.py --only MENTOR_KEY -- \
        python put_site_env.py <путь к .env> <сайт> GOOGLE_SA_CLIENT_EMAIL GOOGLE_SA_PRIVATE_KEY GA4_PROPERTY_ID GSC_SITE_URL
Переменные ставятся с runtime_only: они нужны контейнеру, а не сборке. После — site_deploy.
Адрес хостинга можно сменить переменной MENTOR_URL (по умолчанию https://mentor.tatet.app).
"""
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


def load_env(path):
    env = {}
    for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[name.strip()] = value
    return env


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    env_path, site, names = sys.argv[1], sys.argv[2], sys.argv[3:]
    token = os.environ.get("MENTOR_KEY", "")
    if not token:
        sys.exit("ОШИБКА: нет MENTOR_KEY в окружении — запусти через with-env.py --only MENTOR_KEY")
    env = load_env(env_path)
    missing = [n for n in names if not env.get(n)]
    if missing:
        sys.exit("ОШИБКА: в .env нет " + ", ".join(missing))
    # в переменной сайта \n остаётся двумя символами — код дашборда меняет их на перевод строки сам
    body = json.dumps({"vars": {n: env[n] for n in names}, "runtime_only": True}).encode()
    base = os.environ.get("MENTOR_URL", "https://mentor.tatet.app").rstrip("/")
    req = urllib.request.Request(f"{base}/api/v1/sites/{site}/env", data=body, method="PUT",
                                 headers={"authorization": f"Bearer {token}", "content-type": "application/json"})
    try:
        res = json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as e:
        sys.exit(f"ОШИБКА {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
    print("OK: на сайте", res.get("site", site), "заданы", ", ".join(res.get("set", names)), "— теперь site_deploy")
