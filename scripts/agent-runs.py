"""Журнал запусків субагентів: одне джерело для дошки проєкту, логу сесії та звірки таблиці моделей.
Рішення від 2026-09-19 (прийом «одне джерело статусу, багато читачів»).

Файл: <конфіг Claude Code>/agent-runs.jsonl (за замовчуванням ~/.claude, або папка з CLAUDE_CONFIG_DIR),
лише дописування. Рядок запуску пише хук SubagentStop сам; вердикт оркестратор дописує окремим рядком.
Значення беруться не з пам'яті моделі, а з файлів Claude Code: <сесія>/subagents/agent-<id>.meta.json
(завдання, тип, модель, чи фоновий) і agent-<id>.jsonl (токени, виклики, час).
«Токени» — те саме число, що в повідомленні про завершення агента: найбільший контекст одного ходу.

  python agent-runs.py hook                          вхід хука зі stdin (SubagentStop)
  python agent-runs.py verdict <agent_id> <вердикт> [примітка]    прийнято | повернено | провал | зупинено
  python agent-runs.py show [--all] [--session ID | --today | --since РРРР-ММ-ДД] [--json]
  python agent-runs.py backfill                      дописати минулі запуски з <конфіг>/projects (без дублів)

show без --all показує лише проєкт поточної папки (корінь git). Пошук agent_id у verdict — за початком рядка.
Старі російські слова вердикту (принят, возвращён, остановлен) приймаються і записуються українськими."""
import datetime
import glob
import json
import os
import sys



def config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


JOURNAL = os.path.join(config_dir(), "agent-runs.jsonl")
PROJECTS = os.path.join(config_dir(), "projects")
VERDICTS = ("прийнято", "повернено", "провал", "зупинено")
VERDICT_ALIASES = {"принят": "прийнято", "возвращён": "повернено", "остановлен": "зупинено"}


def project_root(cwd):
    path = os.path.abspath(cwd or ".")
    probe = path
    while True:
        if os.path.exists(os.path.join(probe, ".git")):
            return probe.replace("\\", "/")
        parent = os.path.dirname(probe)
        if parent == probe:
            return path.replace("\\", "/")
        probe = parent


def read_jsonl(path):
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
    except OSError:
        pass
    return rows


def parse_ts(text):
    try:
        return datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def run_record(transcript_path, session_id=None, agent_id=None, agent_type=None):
    """Збирає рядок журналу з транскрипту агента і файлу опису поруч із ним."""
    rows = read_jsonl(transcript_path)
    meta = {}
    try:
        with open(transcript_path[:-len(".jsonl")] + ".meta.json", encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        pass

    tokens = tool_uses = 0
    model = None
    for row in rows:
        if row.get("type") != "assistant":
            continue
        message = row.get("message") or {}
        model = message.get("model") or model
        usage = message.get("usage") or {}
        context = sum(int(usage.get(k) or 0) for k in (
            "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))
        tokens = max(tokens, context)
        for part in message.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "tool_use":
                tool_uses += 1

    stamps = [t for t in (parse_ts(r.get("timestamp")) for r in rows) if t]
    started, ended = (min(stamps), max(stamps)) if stamps else (None, None)
    first = rows[0] if rows else {}
    return {
        "kind": "run",
        "ts": (ended or datetime.datetime.now(datetime.timezone.utc)).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        "session_id": session_id or first.get("sessionId"),
        "project": project_root(first.get("cwd") or os.getcwd()),
        "agent_id": agent_id or first.get("agentId"),
        "task": meta.get("description") or "",
        "agent_type": agent_type or meta.get("agentType") or "",
        "model_asked": meta.get("model") or "успадкована",
        "model": model or "",
        "background": meta.get("requestShape") == "background",
        "tokens": tokens,
        "tool_uses": tool_uses,
        "duration_s": int((ended - started).total_seconds()) if started and ended else 0,
    }


def append(record):
    os.makedirs(os.path.dirname(JOURNAL), exist_ok=True)
    with open(JOURNAL, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def cmd_hook():
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    transcript = payload.get("agent_transcript_path")
    if not transcript or not os.path.exists(transcript):
        return 0
    try:
        append(run_record(transcript, payload.get("session_id"), payload.get("agent_id"), payload.get("agent_type")))
    except Exception as err:  # журнал не повинен ламати роботу агента
        try:
            with open(os.path.join(config_dir(), "hook-log.txt"), "a", encoding="utf-8") as log:
                log.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} agent-runs: помилка {err!r}\n")
        except Exception:
            pass
    return 0


def merged():
    """Останній рядок запуску на agent_id (агента могли продовжити) плюс останній вердикт."""
    runs, verdicts = {}, {}
    for row in read_jsonl(JOURNAL):
        if row.get("kind") == "run" and row.get("agent_id"):
            runs[row["agent_id"]] = row
        elif row.get("kind") == "verdict":
            verdicts[row.get("agent_id")] = row
    for agent_id, row in runs.items():
        verdict = verdicts.get(agent_id) or {}
        row["verdict"] = verdict.get("verdict", "")
        row["note"] = verdict.get("note", "")
    return sorted(runs.values(), key=lambda r: r.get("ts", ""))


def cmd_verdict(args):
    if len(args) >= 2:
        args = [args[0], VERDICT_ALIASES.get(args[1], args[1])] + args[2:]
    if len(args) < 2 or args[1] not in VERDICTS:
        print("потрібно: verdict <agent_id> <" + " | ".join(VERDICTS) + "> [примітка]", file=sys.stderr)
        return 2
    matches = [r for r in merged() if r["agent_id"].startswith(args[0])]
    if len(matches) != 1:
        print(f"agent_id «{args[0]}»: знайдено {len(matches)}, потрібен рівно один", file=sys.stderr)
        return 1
    append({"kind": "verdict", "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "agent_id": matches[0]["agent_id"], "verdict": args[1], "note": " ".join(args[2:])})
    print(f"{matches[0]['task']} → {args[1]}")
    return 0


def short_model(name):
    name = (name or "").replace("claude-", "")
    for family in ("fable", "opus", "sonnet", "haiku"):
        if family in name:
            return family
    return name or "?"


def cmd_show(args):
    rows = merged()
    if "--all" not in args:
        here = project_root(os.getcwd())
        rows = [r for r in rows if r.get("project") == here]
    if "--session" in args:
        session = args[args.index("--session") + 1]
        rows = [r for r in rows if str(r.get("session_id", "")).startswith(session)]
    if "--today" in args:
        today = datetime.date.today().isoformat()
        rows = [r for r in rows if r.get("ts", "").startswith(today)]
    if "--since" in args:
        since = args[args.index("--since") + 1]
        rows = [r for r in rows if r.get("ts", "") >= since]
    if "--json" in args:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    if not rows:
        print("запусків немає")
        return 0
    print("| коли | завдання | тип | модель | токени | виклики | хв | вердикт |")
    print("|---|---|---|---|---:|---:|---:|---|")
    for r in rows:
        print(f"| {r['ts'][5:16]} | {r['task']} | {r['agent_type']} | {short_model(r['model'])} | "
              f"{r['tokens']:,} | {r['tool_uses']} | {r['duration_s'] / 60:.1f} | {r['verdict'] or '—'} |".replace(",", " "))
    by_model = {}
    for r in rows:
        by_model[short_model(r["model"])] = by_model.get(short_model(r["model"]), 0) + r["tokens"]
    total = sum(by_model.values())
    parts = ", ".join(f"{m} {t:,}".replace(",", " ") for m, t in sorted(by_model.items(), key=lambda x: -x[1]))
    print(f"\nразом {len(rows)} запусків, {total:,} токенів: ".replace(",", " ") + parts
          + f"; без вердикту: {sum(1 for r in rows if not r['verdict'])}")
    return 0


def cmd_backfill():
    known = {r.get("agent_id") for r in read_jsonl(JOURNAL) if r.get("kind") == "run"}
    added = 0
    found = sorted(glob.glob(os.path.join(PROJECTS, "*", "*", "subagents", "agent-*.jsonl")), key=os.path.getmtime)
    for path in found:
        record = run_record(path)
        if record["agent_id"] and record["agent_id"] not in known and record["tokens"]:
            append(record)
            known.add(record["agent_id"])
            added += 1
    print(f"транскриптів знайдено {len(found)}, дописано {added}")
    return 0


def main(argv):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    command = argv[0] if argv else "show"
    if command == "hook":
        return cmd_hook()
    if command == "verdict":
        return cmd_verdict(argv[1:])
    if command == "show":
        return cmd_show(argv[1:])
    if command == "backfill":
        return cmd_backfill()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
