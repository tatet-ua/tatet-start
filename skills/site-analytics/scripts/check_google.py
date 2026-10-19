"""Проверка доступа сервисного аккаунта к GA4 и Search Console. Секреты не печатает.

    python check_google.py <путь к .env>              # runReport GA4 и список ресурсов Search Console
    python check_google.py <путь к .env> --realtime   # плюс отчёт реального времени GA4
Читает GOOGLE_SA_CLIENT_EMAIL, GOOGLE_SA_PRIVATE_KEY, GA4_PROPERTY_ID, GSC_SITE_URL.
Нужны пакеты google-auth и requests.
"""
import pathlib
import sys

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

BS = chr(92)
SCOPES = ["https://www.googleapis.com/auth/analytics.readonly",
          "https://www.googleapis.com/auth/webmasters.readonly"]


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


def session(env):
    key = env["GOOGLE_SA_PRIVATE_KEY"].replace(BS + "n", chr(10))
    creds = service_account.Credentials.from_service_account_info(
        {"type": "service_account", "client_email": env["GOOGLE_SA_CLIENT_EMAIL"], "private_key": key,
         "token_uri": "https://oauth2.googleapis.com/token"}, scopes=SCOPES)
    return AuthorizedSession(creds)


def fail(name, r):
    print(f"{name}: ОШИБКА {r.status_code}", r.text[:300])
    return False


def check_ga4(env, s):
    prop = env["GA4_PROPERTY_ID"].strip()
    r = s.post(f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport",
               json={"dateRanges": [{"startDate": "7daysAgo", "endDate": "today"}],
                     "metrics": [{"name": "sessions"}, {"name": "activeUsers"}]})
    if not r.ok:
        return fail("GA4", r)
    rows = r.json().get("rows", [])
    sessions, users = ([m["value"] for m in rows[0]["metricValues"]] if rows else ["0", "0"])
    print(f"GA4: OK, ресурс {prop}, за 7 дней сессий {sessions}, пользователей {users}")
    return True


def check_gsc(env, s):
    site = env["GSC_SITE_URL"].strip()
    r = s.get("https://www.googleapis.com/webmasters/v3/sites")
    if not r.ok:
        return fail("GSC", r)
    sites = {x["siteUrl"]: x["permissionLevel"] for x in r.json().get("siteEntry", [])}
    if site not in sites:
        print(f"GSC: ОШИБКА, {site} нет в списке ресурсов аккаунта; есть: {', '.join(sites) or 'ничего'}")
        return False
    print(f"GSC: OK, {site}, права {sites[site]}")
    return True


def check_realtime(env, s):
    prop = env["GA4_PROPERTY_ID"].strip()
    r = s.post(f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runRealtimeReport",
               json={"metrics": [{"name": "activeUsers"}]})
    if not r.ok:
        return fail("GA4 realtime", r)
    rows = r.json().get("rows", [])
    users = rows[0]["metricValues"][0]["value"] if rows else "0"
    print(f"GA4 realtime: OK, активных пользователей за 30 минут {users}")
    return users != "0"


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    env = load_env(sys.argv[1])
    s = session(env)
    ok = check_ga4(env, s) & check_gsc(env, s)
    if "--realtime" in sys.argv:
        ok = check_realtime(env, s) and ok
    sys.exit(0 if ok else 1)
