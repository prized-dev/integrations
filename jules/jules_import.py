#!/usr/bin/env python3
"""Set up a Prized box (or any Linux machine with systemd) from a Google Jules export.

Run it on the box, after copying the folder Google's export wrote (~/jules-export) there:

    python3 jules_import.py ~/jules-export --dry-run    # print the plan, change nothing
    python3 jules_import.py ~/jules-export              # do it

What it does, per repository that has Jules configuration or sessions:

- clones it into ~/code/<repo> (with `gh` when it is signed in, so private repos work);
- installs the runtimes the setup script and the repo need that the box lacks (Node.js 22,
  Bun, Go, Rust, uv; user-level, nothing system-wide);
- runs the repo's Jules setup script once, with its environment variables;
- writes the environment variables to ~/.jules-import/env/<owner>__<repo>.env (mode 600,
  outside the repo, values never printed);
- adds the repo's Jules memories and test command to ~/.claude/CLAUDE.md (Claude Code) and
  ~/.codex/AGENTS.md (Codex), one block per repo, so no repo file changes;
- turns each scheduled task into ~/.jules-import/tasks/<name>.sh plus a systemd user timer:
  every run gets a fresh git worktree, runs the setup script, starts Claude Code or Codex
  headless with the task's prompt, then pushes a branch and opens a pull request with `gh`,
  the way Jules did. Paused tasks are written but not enabled;
- lists every Jules session in ~/.jules-import/sessions.md, unfinished ones first.

Running it again is safe: clones, files and timers are updated in place.

The Jules export's env-variables.json and scheduled-tasks.json are raw responses from Jules'
internal API, with no published schema, so this reads them by field meaning (a name and a value;
a prompt and a schedule) rather than fixed paths. Anything it can't place is listed at the end
with the file to read, and `jules_import.py schedule NAME 'SPEC'` sets a schedule by hand.

Standard library only; Python 3.9+.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore

HOME = Path(os.environ.get("HOME", str(Path.home())))
STATE = Path(os.environ.get("JULES_IMPORT_HOME", str(HOME / ".jules-import")))
GIT_URL = os.environ.get("JULES_IMPORT_GIT_URL", "")  # tests: file:///.../{owner}/{repo}.git
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# ---------------------------------------------------------------------------------------------
# Reading the export


def load_json(path: Path, notes: list[str]):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text() or "null")
    except ValueError as e:
        notes.append(f"{path.name} is not valid JSON ({e}); skipped it")
        return None


def walk(obj, ancestors=()):
    """Yield (dict, ancestors) for every dict in obj, depth first."""
    if isinstance(obj, dict):
        yield obj, ancestors
        for v in obj.values():
            yield from walk(v, (*ancestors, obj))
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v, ancestors)


def lower_keys(d: dict) -> dict:
    return {k.lower().replace("_", ""): v for k, v in d.items()}


def safe(s: str) -> str:
    """The folder-name mangling Google's export script uses for repos/<sourceId>."""
    return re.sub(r"[^a-zA-Z0-9._-]", "_", s)


class Source:
    def __init__(self, owner: str, repo: str, branch: str = "", private=None):
        self.owner, self.repo, self.branch, self.private = owner, repo, branch, private
        self.ids: set[str] = {f"{owner}/{repo}", f"github/{owner}/{repo}", f"sources/github/{owner}/{repo}"}
        self.setup = ""  # setup script text
        self.test = ""  # test command
        self.memories: list[str] = []
        self.env: dict[str, str] = {}
        self.sessions = 0
        self.dir: Path | None = None

    @property
    def full(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def key(self) -> str:
        return f"{self.owner}__{self.repo}"

    def has_config(self) -> bool:
        return bool(self.setup or self.test or self.memories or self.env)


def owner_repo(value) -> tuple[str, str] | None:
    """('owner', 'repo') from the shapes a repo reference takes, or None."""
    if isinstance(value, dict):
        d = lower_keys(value)
        for o, r in (("owner", "repo"), ("repoowner", "reponame"), ("owner", "name"), ("org", "repo")):
            if isinstance(d.get(o), str) and isinstance(d.get(r), str) and d[o] and d[r]:
                return d[o], d[r]
        for k in ("githubrepo", "githubsource", "repository", "repo", "source", "swebotsource"):
            if isinstance(d.get(k), dict):
                found = owner_repo(d[k])
                if found:
                    return found
        for k in ("sourceid", "source", "id", "name", "url", "fullname", "repo", "repository"):
            if isinstance(d.get(k), str):
                found = owner_repo(d[k])
                if found:
                    return found
        return None
    if not isinstance(value, str):
        return None
    s = value.strip()
    m = re.search(r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", s)
    if m:
        return m.group(1), m.group(2)
    s = re.sub(r"^sources/", "", s)
    s = re.sub(r"^(github|gh)/", "", s)
    m = re.fullmatch(r"([\w.-]+)/([\w.-]+)", s)
    return (m.group(1), m.group(2)) if m else None


def read_sources(export: Path, notes: list[str]) -> list[Source]:
    raw = load_json(export / "sources.json", notes)
    items = raw.get("sources", []) if isinstance(raw, dict) else raw or []
    out: dict[str, Source] = {}
    for item in items if isinstance(items, list) else []:
        found = owner_repo(item)
        if not found:
            notes.append(f"sources.json: couldn't find the repo in {json.dumps(item)[:120]}")
            continue
        src = out.get(f"{found[0]}/{found[1]}".lower()) or Source(*found)
        flat = {}
        for d, _ in walk(item):
            for k, v in lower_keys(d).items():
                flat.setdefault(k, v)
        idsrc = [item] + [v for k, v in item.items() if isinstance(v, dict) and k.lower().endswith("source")]
        for d in idsrc:
            for k in ("sourceId", "id", "name"):
                if isinstance(d.get(k), str) and d[k]:
                    src.ids.add(d[k])
                    src.ids.add(re.sub(r"^sources/", "", d[k]))
        if isinstance(flat.get("defaultbranch"), str):
            src.branch = flat["defaultbranch"]
        elif isinstance(flat.get("defaultbranch"), dict) and isinstance(flat["defaultbranch"].get("displayName"), str):
            src.branch = flat["defaultbranch"]["displayName"]
        if isinstance(flat.get("isprivate"), bool):
            src.private = flat["isprivate"]
        out[src.full.lower()] = src
    return list(out.values())


def find_source(sources: list[Source], ref, create=True) -> Source | None:
    """The Source a sourceId, folder name or repo reference points at."""
    if ref is None:
        return None
    if isinstance(ref, str):
        for s in sources:
            if ref in s.ids or ref.lower() == s.full.lower() or any(safe(i) == ref for i in s.ids):
                return s
        for s in sources:  # a repos/ folder name: github_owner_repo
            if ref.lower().endswith(safe(f"_{s.owner}_{s.repo}").lower()) or ref.lower() == safe(s.full).lower():
                return s
        same = [s for s in sources if s.repo.lower() == ref.lower()]
        if len(same) == 1:  # a bare repo name, when only one repo has it
            return same[0]
    found = owner_repo(ref)
    if not found:
        return None
    for s in sources:
        if s.full.lower() == f"{found[0]}/{found[1]}".lower():
            return s
    if not create:
        return None
    s = Source(*found)
    sources.append(s)
    return s


REPO_KEYS = (
    "sourceid",
    "source",
    "sourcename",
    "repo",
    "repository",
    "reponame",
    "githubrepo",
    "githubsource",
    "sourcecontext",
)


def repo_of(d: dict, ancestors, sources: list[Source]) -> Source | None:
    """The repo a record belongs to: its own repo fields, a child's ({"task": {"source": ...}}), else the
    nearest ancestor's."""
    for node in (d, *(v for v in d.values() if isinstance(v, dict)), *reversed(ancestors)):
        low = lower_keys(node)
        for k in REPO_KEYS:
            v = low.get(k)
            if v is None:
                continue
            if k == "reponame" and isinstance(low.get("repoowner"), str):
                v = f"{low['repoowner']}/{v}"
            s = find_source(sources, v)
            if s:
                return s
    return None


def read_repo_folders(export: Path, sources: list[Source], notes: list[str]) -> None:
    repos = export / "repos"
    if not repos.is_dir():
        return
    for d in sorted(p for p in repos.iterdir() if p.is_dir()):
        src = find_source(sources, d.name, create=False)
        if not src:
            m = re.fullmatch(r"(?:github_)?([\w.-]+?)_([\w.-]+)", d.name)
            notes.append(
                f"repos/{d.name}: no repo in sources.json matches this folder; skipped it"
                + (f" (is it {m.group(1)}/{m.group(2)}?)" if m else "")
            )
            continue
        if (d / "setup.sh").exists():
            src.setup = (d / "setup.sh").read_text()
        if (d / "test-command.txt").exists():
            src.test = (d / "test-command.txt").read_text().strip()
        mems = load_json(d / "memories.json", notes)
        if isinstance(mems, dict):
            mems = mems.get("memories", [])
        if isinstance(mems, list) and mems:
            for m in mems:
                if isinstance(m, dict) and not m.get("archived"):
                    fact = m.get("fact") or m.get("Fact") or m.get("content") or m.get("text")
                    if isinstance(fact, str) and fact.strip():
                        src.memories.append(" ".join(fact.split()))
        elif (d / "memories.md").exists():
            for line in (d / "memories.md").read_text().splitlines():
                if line.startswith("- ") and line[2:].strip() not in ("", "None"):
                    src.memories.append(line[2:].strip())


def read_env(export: Path, sources: list[Source], notes: list[str]) -> dict[str, str]:
    """Fill each Source's env; return the variables that belong to no repo."""
    raw = load_json(export / "env-variables.json", notes)
    glob: dict[str, str] = {}
    if raw in (None, {}, []):
        return glob
    found = 0
    for d, anc in walk(raw):
        low = lower_keys(d)
        name = next(
            (
                low[k]
                for k in ("name", "key", "variablename", "envname", "envvarname", "displayname")
                if isinstance(low.get(k), str)
            ),
            None,
        )
        value = next(
            (
                low[k]
                for k in ("value", "variablevalue", "envvalue", "plaintextvalue")
                if isinstance(low.get(k), (str, int, float, bool))
            ),
            None,
        )
        if name is None or value is None:
            continue
        name = name.rsplit("/", 1)[-1]
        if not NAME_RE.match(name):
            notes.append(f"env-variables.json: skipped a variable whose name isn't a shell name ({name[:40]!r})")
            continue
        found += 1
        src = repo_of(d, anc, sources)
        (src.env if src else glob)[name] = str(value)
    if not found:  # a plain {"NAME": "value"} map, possibly one level down
        for d, anc in walk(raw):
            if (
                d
                and all(isinstance(v, str) for v in d.values())
                and all(NAME_RE.match(k) for k in d)
                and not owner_repo(d)
                and not set(lower_keys(d)) & {*REPO_KEYS, "owner", "name", "id"}
            ):
                src = repo_of(anc[-1], anc[:-1], sources) if anc else None
                (src.env if src else glob).update(d)
                found += len(d)
    if not found:
        notes.append(
            "env-variables.json: found no name/value pairs in it; read it and set them by hand "
            "(see 'Environment variables' in the README)"
        )
    return glob


# ---------------------------------------------------------------------------------------------
# Schedules: everything becomes a list of systemd OnCalendar expressions

PROMPT_KEYS = (
    "prompt",
    "taskprompt",
    "description",
    "taskdescription",
    "instruction",
    "instructions",
    "query",
    "userprompt",
    "text",
)
SCHEDULE_HINTS = (
    "schedule",
    "cron",
    "cronexpression",
    "frequency",
    "cadence",
    "recurrence",
    "rrule",
    "interval",
    "timeofday",
    "daysofweek",
    "dayofweek",
    "nextruntime",
    "trigger",
    "repeat",
)


def find_prompt(d: dict) -> str:
    """The task's prompt: a prompt-like field on it, or on a dict one level down ({"task": {"prompt": ...}})."""
    for node in (d, *(v for v in d.values() if isinstance(v, dict))):
        low = lower_keys(node)
        for k in PROMPT_KEYS:
            if isinstance(low.get(k), str) and low[k].strip():
                return low[k]
    return ""


def clean_title(s: str) -> str:
    """A title that is safe inside double quotes in the generated script."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w .,:()+#@'-]", "", s)).strip()[:80]


class ScheduledTask:
    def __init__(self, raw: dict, source: Source | None):
        self.raw, self.source = raw, source
        low = lower_keys(raw)
        self.prompt = find_prompt(raw)
        self.title = clean_title(
            next(
                (
                    low[k]
                    for k in ("title", "displayname", "name", "label")
                    if isinstance(low.get(k), str) and low[k].strip() and "/" not in low[k]
                ),
                "",
            )
        )
        self.id = next((str(low[k]) for k in ("id", "scheduledtaskid", "taskid", "name") if low.get(k)), "")
        self.branch = next(
            (low[k] for k in ("startingbranch", "branch", "basebranch", "targetbranch") if isinstance(low.get(k), str)),
            "",
        )
        if not self.branch:
            for d, _ in walk(raw):
                b = lower_keys(d).get("startingbranch")
                if isinstance(b, str):
                    self.branch = b
        self.calendar, self.human, self.tz, self.paused, self.guess = parse_schedule(raw)
        self.slug = ""


def is_task(d: dict) -> bool:
    if not find_prompt(d):
        return False
    keys = set()
    for sub, _ in walk(d):
        keys.update(lower_keys(sub))
    return any(h in keys for h in SCHEDULE_HINTS)


def read_tasks(export: Path, sources: list[Source], notes: list[str]) -> list[ScheduledTask]:
    raw = load_json(export / "scheduled-tasks.json", notes)
    tasks: list[ScheduledTask] = []
    taken: list[dict] = []
    for d, anc in walk(raw):
        if any(a is t for a in anc for t in taken) or not is_task(d):
            continue
        taken.append(d)
        tasks.append(ScheduledTask(d, repo_of(d, anc, sources)))
    if raw not in (None, {}, []) and not tasks:
        notes.append(
            "scheduled-tasks.json: found no task with a prompt and a schedule in it; read it and use "
            "`jules_import.py schedule` (see the README)"
        )
    used: set[str] = set()
    for t in tasks:
        base = re.sub(r"[^a-z0-9]+", "-", (t.title or " ".join(t.prompt.split()[:6])).lower()).strip("-")[:40]
        base = base.strip("-") or "task"
        slug, n = base, 2
        while slug in used:
            slug, n = f"{base}-{n}", n + 1
        used.add(slug)
        t.slug = slug
    return tasks


def tz_name(value) -> str:
    if isinstance(value, dict):
        value = value.get("id") or value.get("name") or value.get("timeZone")
    if not isinstance(value, str) or not value.strip():
        return ""
    value = value.strip()
    if value.upper() in ("UTC", "GMT", "Z", "ETC/UTC"):
        return "UTC"
    if ZoneInfo is None:
        return value
    try:
        ZoneInfo(value)
        return value
    except Exception:
        return ""


def parse_time(value) -> tuple[int, int] | None:
    """(hour, minute) from {"hours": 9, "minutes": 30} (google.type.TimeOfDay drops zero fields), "09:30",
    "9:30 PM" or "9am"."""
    if isinstance(value, dict):
        low = lower_keys(value)
        h, m = low.get("hours", low.get("hour")), low.get("minutes", low.get("minute"))
        if not all(isinstance(x, (int, float)) or x is None for x in (h, m)):
            return None
        return int(h or 0), int(m or 0)
    if not isinstance(value, str):
        return None
    m = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?(?::\d{2}(?:\.\d+)?)?\s*([ap]\.?m\.?)?\s*", value, re.I)
    if not m or (m.group(2) is None and m.group(3) is None):
        return None
    h, minute = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3):
        h = h % 12 + (12 if m.group(3).lower().startswith("p") else 0)
    return (h, minute) if h < 24 and minute < 60 else None


def parse_timestamp(value) -> dt.datetime | None:
    if isinstance(value, dict) and "seconds" in value:
        try:
            return dt.datetime.fromtimestamp(int(value["seconds"]), dt.timezone.utc)
        except (TypeError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", value.replace("Z", "+00:00")))
    except ValueError:
        return None


def day_names(value) -> list[str]:
    """Weekday names (Mon..Sun) from MONDAY / mon / 1 (google.type.DayOfWeek: 1 = Monday, 7 = Sunday)."""
    items = value if isinstance(value, list) else re.split(r"[,\s]+", value) if isinstance(value, str) else [value]
    out = []
    for item in items:
        if isinstance(item, dict):
            item = item.get("day") or item.get("dayOfWeek") or item.get("value")
        if isinstance(item, bool):
            continue
        if isinstance(item, int) and 1 <= item <= 7:
            out.append(DAYS[item - 1])
        elif isinstance(item, str) and item.strip():
            s = item.strip().upper()
            s = {"MO": "MON", "TU": "TUE", "WE": "WED", "TH": "THU", "FR": "FRI", "SA": "SAT", "SU": "SUN"}.get(s, s)
            for d in DAYS:
                if s.startswith(d.upper()):
                    out.append(d)
    return sorted(set(out), key=DAYS.index)


CRON_MACROS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def cron_field(field: str, lo: int, hi: int, names: list[str] | None = None, base: int = 0) -> list[int] | None:
    """Expand one cron field to its values; None means '*'."""
    if field == "*":
        return None
    values: set[int] = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a, b = (cron_value(x, names, base) for x in part.split("-", 1))
        else:
            a = cron_value(part, names, base)
            b = hi if step != 1 else a
        if not (lo <= a <= hi and lo <= b <= hi) or step < 1:
            raise ValueError(field)
        values.update(range(a, b + 1, step))
    return sorted(values)


def cron_value(s: str, names: list[str] | None, base: int) -> int:
    if names and s[:3].upper() in names:
        return names.index(s[:3].upper()) + base
    return int(s)


def cron_to_calendar(expr: str, tz: str) -> list[str]:
    expr = CRON_MACROS.get(expr.strip().lower(), expr.strip())
    f = expr.split()
    if len(f) != 5:
        raise ValueError(expr)
    minute = cron_field(f[0], 0, 59)
    hour = cron_field(f[1], 0, 23)
    dom = cron_field(f[2], 1, 31)
    month = cron_field(f[3], 1, 12, MONTHS, 1)
    dow = cron_field(f[4], 0, 7, ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"], 0)

    def j(v, width=2):
        return "*" if v is None else ",".join(str(x).zfill(width) for x in v)

    days = None if dow is None else sorted({DAYS[(d - 1) % 7] for d in dow}, key=DAYS.index)
    tail = f" {j(hour)}:{j(minute)}:00" + (f" {tz}" if tz else "")
    if dom is not None and days is not None:  # cron runs when either matches: two expressions
        return [f"*-{j(month)}-{j(dom)}{tail}", f"{','.join(days)} *-{j(month)}-*{tail}"]
    prefix = f"{','.join(days)} " if days else ""
    return [f"{prefix}*-{j(month)}-{j(dom)}{tail}"]


def rrule_to_calendar(rule: str, tz: str, at: tuple[int, int] | None) -> list[str] | None:
    parts = dict(p.split("=", 1) for p in rule.upper().replace("RRULE:", "").split(";") if "=" in p)
    freq = parts.get("FREQ", "")
    h = int(parts["BYHOUR"].split(",")[0]) if parts.get("BYHOUR") else (at or (9, 0))[0]
    m = int(parts["BYMINUTE"].split(",")[0]) if parts.get("BYMINUTE") else (at or (9, 0))[1]
    days = day_names(parts.get("BYDAY", ""))
    return build_calendar(freq, (h, m), days, int(parts.get("BYMONTHDAY", "1").split(",")[0]), tz)


def build_calendar(freq: str, at: tuple[int, int], days: list[str], dom: int, tz: str) -> list[str] | None:
    freq = freq.upper()
    t = f"{at[0]:02d}:{at[1]:02d}:00" + (f" {tz}" if tz else "")
    if "HOUR" in freq:
        return [f"*-*-* *:{at[1]:02d}:00"]
    if "WEEKDAY" in freq or "BUSINESS" in freq:
        return [f"Mon..Fri *-*-* {t}"]
    if "DAI" in freq or "DAY" == freq or "EVERY_DAY" in freq:
        return [f"{','.join(days)} *-*-* {t}" if days and len(days) < 7 else f"*-*-* {t}"]
    if "WEEK" in freq:
        return [f"{','.join(days or ['Mon'])} *-*-* {t}"]
    if "MONTH" in freq:
        return [f"*-*-{dom:02d} {t}"]
    return None


def parse_schedule(raw: dict):
    """(calendar expressions or None, a readable summary, tz, paused, what was guessed)."""
    flat: dict = {}
    for d, _ in walk(raw):
        for k, v in lower_keys(d).items():
            flat.setdefault(k, v)
    tz = next((tz_name(flat[k]) for k in ("timezone", "timezoneid", "tz", "zone") if k in flat), "") or "UTC"
    state = " ".join(str(flat.get(k, "")) for k in ("state", "status", "schedulestate")).upper()
    paused = (
        flat.get("paused") is True
        or flat.get("enabled") is False
        or flat.get("active") is False
        or any(w in state for w in ("PAUS", "DISABL", "INACTIVE", "STOPPED"))
    )
    guess = ""
    # 1. a cron expression
    for k in ("cron", "cronexpression", "cronschedule", "crontab", "schedule", "expression", "spec"):
        v = flat.get(k)
        if isinstance(v, str) and (v.strip().startswith("@") or len(v.split()) == 5):
            try:
                return cron_to_calendar(v, tz), f"cron `{v.strip()}` ({tz})", tz, paused, guess
            except (ValueError, IndexError):
                pass
    at = None
    for k in ("timeofday", "time", "starttime", "runtime", "scheduledtime", "localtime", "runat", "at", "runtimeofday"):
        if k in flat:
            at = parse_time(flat[k])
            if at:
                break
    if at is None and isinstance(flat.get("hour", flat.get("hours")), (int, float)):
        at = (int(flat.get("hour", flat.get("hours"))), int(flat.get("minute", flat.get("minutes", 0)) or 0))
    when = next(
        (
            parse_timestamp(flat[k])
            for k in ("nextruntime", "nextrun", "nextrunat", "lastruntime", "lastrun", "createtime", "createdat")
            if parse_timestamp(flat.get(k))
        ),
        None,
    )
    if when and ZoneInfo is not None:
        when = when.astimezone(ZoneInfo(tz))
    # 2. an RRULE
    for v in flat.values():
        if isinstance(v, str) and "FREQ=" in v.upper():
            cal = rrule_to_calendar(v, tz, at)
            if cal:
                return cal, f"`{v.strip()}` ({tz})", tz, paused, guess
    # 3. a frequency word, a time, days
    freq = ""
    for k in (
        "frequency",
        "cadence",
        "recurrence",
        "repeat",
        "period",
        "interval",
        "type",
        "schedule",
        "frequencytype",
        "recurrencetype",
        "unit",
    ):
        v = flat.get(k)
        if isinstance(v, str) and re.search(r"HOUR|DAI|DAY|WEEK|MONTH", v.upper()):
            freq = v
            break
    if not freq:
        return None, "", tz, paused, guess
    days = []
    for k in ("daysofweek", "dayofweek", "weekdays", "weekday", "days", "byday", "day"):
        if k in flat:
            days = day_names(flat[k])
            if days:
                break
    if at is None:
        if when:
            at, guess = (when.hour, when.minute), "time of day taken from the last or next run"
        else:
            at, guess = (9, 0), "no time of day in the export: 09:00 picked"
    if "WEEK" in freq.upper() and "WEEKDAY" not in freq.upper() and not days:
        if when:
            days, guess = (
                [DAYS[when.weekday()]],
                (guess + "; " if guess else "") + "weekday taken from the last or next run",
            )
        else:
            days, guess = ["Mon"], (guess + "; " if guess else "") + "no weekday in the export: Monday picked"
    dom = next((int(flat[k]) for k in ("dayofmonth", "monthday", "daysofmonth") if isinstance(flat.get(k), int)), 0)
    if "MONTH" in freq.upper() and not dom:
        dom = when.day if when else 1
    cal = build_calendar(freq, at, days, dom or 1, tz)
    human = f"{freq.lower()}" + (f" on {', '.join(days)}" if days else "") + f" at {at[0]:02d}:{at[1]:02d} {tz}"
    return cal, human, tz, paused, guess


def check_calendar(specs: list[str]) -> str:
    """'' when systemd accepts every expression (or can't be asked), else its complaint."""
    tool = shutil.which("systemd-analyze")
    if not tool:
        return ""
    for spec in specs:
        r = subprocess.run([tool, "calendar", spec], capture_output=True, text=True)
        if r.returncode != 0:
            return (r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else spec
    return ""


# ---------------------------------------------------------------------------------------------
# Doing it


class Run:
    def __init__(self, args):
        self.dry = args.dry_run
        self.notes: list[str] = []  # things to tell the user at the end
        self.done: list[str] = []

    def log(self, msg: str):
        print(("[dry run] " if self.dry else "") + msg, flush=True)

    def sh(self, cmd: list[str], cwd: Path | None = None, env=None, log: Path | None = None, timeout=None) -> int:
        if self.dry:
            return 0
        if log:
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "a") as fh:
                fh.write(f"\n$ {' '.join(shlex.quote(c) for c in cmd)}\n")
                fh.flush()
                try:
                    return subprocess.run(
                        cmd, cwd=cwd, env=env, stdout=fh, stderr=subprocess.STDOUT, timeout=timeout
                    ).returncode
                except subprocess.TimeoutExpired:
                    fh.write(f"timed out after {timeout} s\n")
                    return 124
        return subprocess.run(cmd, cwd=cwd, env=env, timeout=timeout).returncode

    def write(self, path: Path, content: str, mode: int | None = None):
        if self.dry:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode if mode is not None else 0o644)
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
        os.replace(tmp, path)
        if mode is not None:
            os.chmod(path, mode)


def gh_ready() -> bool:
    return bool(shutil.which("gh")) and subprocess.run(["gh", "auth", "status"], capture_output=True).returncode == 0


def clone(run: Run, src: Source, code: Path, use_gh: bool) -> bool:
    dest = src.dir
    if dest is None:
        return False
    if (dest / ".git").exists():
        run.log(f"{src.full}: already cloned in {dest}; fetching")
        run.sh(["git", "-C", str(dest), "fetch", "--quiet", "origin"])
        return True
    run.log(f"{src.full}: cloning into {dest}")
    if run.dry:
        return True
    code.mkdir(parents=True, exist_ok=True)
    if GIT_URL:
        rc = run.sh(["git", "clone", "--quiet", GIT_URL.format(owner=src.owner, repo=src.repo), str(dest)])
    elif use_gh:
        rc = run.sh(["gh", "repo", "clone", src.full, str(dest), "--", "--quiet"])
    else:
        rc = run.sh(
            ["git", "clone", "--quiet", f"https://github.com/{src.full}.git", str(dest)],
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    if rc != 0:
        run.notes.append(
            f"{src.full}: clone failed"
            + (
                ""
                if use_gh
                else ": if it's private, sign the GitHub CLI "
                "in on the box (`gh auth login`, then `gh auth setup-git`) and run this again"
            )
        )
        return False
    if not src.branch:
        r = subprocess.run(
            ["git", "-C", str(dest), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            capture_output=True,
            text=True,
        )
        src.branch = r.stdout.strip().removeprefix("origin/") or "main"
    return True


# Runtimes a Jules VM had that a fresh box doesn't; installed per user, never with sudo.
TOOLS = {
    "node": (r"\b(npm|npx|node|pnpm|yarn|corepack|nvm)\b", ("package.json",), "node"),
    "bun": (r"\b(bun|bunx)\b", ("bun.lockb", "bun.lock", "bunfig.toml"), "bun"),
    "go": (r"(^|[;&|\s])go\s+(build|test|mod|run|install|get|vet|generate|work)\b", ("go.mod",), "go"),
    "rust": (r"\b(cargo|rustc|rustup)\b", ("Cargo.toml",), "cargo"),
    "uv": (r"\b(uv|uvx)\b", ("uv.lock",), "uv"),
}
TOOLCHAIN = STATE / "toolchain.sh"
OPT = STATE / "opt"


def needed_tools(src: Source) -> list[str]:
    text = f"{src.setup}\n{src.test}"
    out = []
    for name, (pattern, files, _) in TOOLS.items():
        if re.search(pattern, text, re.M) or (src.dir and any((src.dir / f).exists() for f in files)):
            out.append(name)
    if re.search(r"\b(pnpm|yarn)\b", text) or (
        src.dir and any((src.dir / f).exists() for f in ("pnpm-lock.yaml", "yarn.lock"))
    ):
        out.append("corepack")
    return out


def tool_present(name: str) -> bool:
    binary = {"node": "node", "bun": "bun", "go": "go", "rust": "cargo", "uv": "uv", "corepack": "pnpm"}[name]
    path = os.pathsep.join(
        [
            str(OPT / "node" / "bin"),
            str(OPT / "go" / "bin"),
            str(HOME / ".bun" / "bin"),
            str(HOME / ".cargo" / "bin"),
            str(HOME / ".local" / "bin"),
            os.environ.get("PATH", ""),
        ]
    )
    return bool(shutil.which(binary, path=path))


def fetch(url: str, dest: Path):
    req = urllib.request.Request(url, headers={"User-Agent": "prized-jules-import"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as fh:
        shutil.copyfileobj(r, fh)


def untar(archive: Path, dest: str):
    with tarfile.open(archive) as t:
        if hasattr(tarfile, "data_filter"):
            t.extractall(dest, filter="data")
        else:  # pragma: no cover - Python < 3.12 without the backport
            t.extractall(dest)


def install_tool(run: Run, name: str, log: Path) -> bool:
    run.log(f"installing {name} for your user (the Jules VM had it; this box doesn't)")
    if run.dry:
        return True
    OPT.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            if name == "node":
                with urllib.request.urlopen("https://nodejs.org/dist/index.json", timeout=60) as r:
                    version = next(v["version"] for v in json.load(r) if v["version"].startswith("v22."))
                arch = {"x86_64": "x64", "aarch64": "arm64"}.get(os.uname().machine, "x64")
                fetch(f"https://nodejs.org/dist/{version}/node-{version}-linux-{arch}.tar.xz", Path(tmp, "n.tar.xz"))
                untar(Path(tmp, "n.tar.xz"), tmp)
                shutil.rmtree(OPT / "node", ignore_errors=True)
                shutil.move(str(Path(tmp, f"node-{version}-linux-{arch}")), str(OPT / "node"))
            elif name == "go":
                with urllib.request.urlopen("https://go.dev/dl/?mode=json", timeout=60) as r:
                    version = json.load(r)[0]["version"]
                arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(os.uname().machine, "amd64")
                fetch(f"https://go.dev/dl/{version}.linux-{arch}.tar.gz", Path(tmp, "go.tgz"))
                untar(Path(tmp, "go.tgz"), tmp)
                shutil.rmtree(OPT / "go", ignore_errors=True)
                shutil.move(str(Path(tmp, "go")), str(OPT / "go"))
            elif name == "corepack":
                corepack = OPT / "node" / "bin" / "corepack"
                if not corepack.exists():
                    corepack = Path(shutil.which("corepack") or "corepack")
                return run.sh([str(corepack), "enable"], log=log) == 0
            else:
                script = {
                    "bun": ("https://bun.sh/install", "bash", []),
                    "rust": ("https://sh.rustup.rs", "sh", ["-y", "--no-modify-path"]),
                    "uv": ("https://astral.sh/uv/install.sh", "sh", []),
                }[name]
                fetch(script[0], Path(tmp, "install.sh"))
                env = {**os.environ, "UV_NO_MODIFY_PATH": "1", "BUN_INSTALL": str(HOME / ".bun")}
                if run.sh([script[1], str(Path(tmp, "install.sh")), *script[2]], env=env, log=log) != 0:
                    raise RuntimeError(f"its installer failed (log: {log})")
    except Exception as e:
        run.notes.append(f"couldn't install {name}: {e}. Install it yourself before the setup script needs it")
        return False
    return True


def write_toolchain(run: Run):
    """~/.jules-import/toolchain.sh puts the installed runtimes on PATH; login and interactive shells load it."""
    body = (
        "# Written by jules_import.py: runtimes it installed for your user.\n"
        'for d in "$HOME/.jules-import/opt/node/bin" "$HOME/.jules-import/opt/go/bin" "$HOME/go/bin" '
        '"$HOME/.bun/bin" "$HOME/.cargo/bin" "$HOME/.local/bin"; do\n'
        '  case ":$PATH:" in *":$d:"*) ;; *) [ -d "$d" ] && PATH="$d:$PATH" ;; esac\n'
        "done\nexport PATH\n"
    )
    run.write(TOOLCHAIN, body)
    line = '[ -f "$HOME/.jules-import/toolchain.sh" ] && . "$HOME/.jules-import/toolchain.sh"  # jules-import\n'
    login = next(
        (HOME / f for f in (".bash_profile", ".bash_login", ".profile") if (HOME / f).exists()), HOME / ".profile"
    )
    for rc in (login, HOME / ".bashrc"):
        text = rc.read_text() if rc.exists() else ""
        if "# jules-import" not in text and not run.dry:
            run.write(rc, text + ("" if text.endswith("\n") or not text else "\n") + line)


def env_file(src: Source | None) -> Path:
    return STATE / "env" / (f"{src.key}.env" if src else "_all.env")


def write_env(run: Run, src: Source | None, env: dict[str, str]):
    if not env:
        return
    path = env_file(src)
    body = (
        "# Jules environment variables"
        + (f" for {src.full}" if src else " for every repo")
        + ". Load with: set -a; . "
        + str(path)
        + "; set +a\n"
    )
    body += "".join(f"{k}={shlex.quote(v)}\n" for k, v in sorted(env.items()))
    if not run.dry:
        (STATE / "env").mkdir(parents=True, exist_ok=True)
        os.chmod(STATE / "env", 0o700)
    run.write(path, body, 0o600)
    run.log(
        f"{src.full if src else 'all repos'}: {len(env)} environment variable(s) -> {path} "
        f"({', '.join(sorted(env))}; values not shown)"
    )


def run_setup(run: Run, src: Source, log: Path) -> None:
    if not src.setup.strip():
        return
    script = STATE / "setup" / f"{src.key}.sh"
    body = src.setup.replace("/home/jules/", "$HOME/").replace("/home/jules", "$HOME")
    run.write(
        script,
        "#!/bin/bash\n# Jules setup script for " + src.full + "\n" + body + ("" if body.endswith("\n") else "\n"),
        0o755,
    )
    run.log(f"{src.full}: running its Jules setup script (log: {log})")
    if run.dry:
        return
    cmd = [
        "bash",
        "-c",
        'for f in "$@"; do [ -f "$f" ] && { set -a; . "$f"; set +a; }; done; '
        f". {shlex.quote(str(TOOLCHAIN))} 2>/dev/null; exec bash {shlex.quote(str(script))}",
        "_",
        str(env_file(None)),
        str(env_file(src)),
    ]
    rc = run.sh(cmd, cwd=src.dir, log=log, timeout=3600)
    if rc == 0:
        run.done.append(f"{src.full}: setup script ran (exit 0)")
    else:
        tail = log.read_text().splitlines()[-5:] if log.exists() else []
        run.notes.append(
            f"{src.full}: the setup script exited {rc}; fix {script} and run it again in {src.dir}. "
            f"Last lines of {log}:\n      " + "\n      ".join(tail)
        )


def write_memories(run: Run, sources: list[Source]):
    for target, agent in ((HOME / ".claude" / "CLAUDE.md", "Claude Code"), (HOME / ".codex" / "AGENTS.md", "Codex")):
        text = target.read_text() if target.exists() else ""
        changed = False
        for src in sources:
            if not (src.memories or src.test):
                continue
            start, end = f"<!-- jules-import {src.full} -->", f"<!-- /jules-import {src.full} -->"
            lines = [
                start,
                f"## {src.full} (carried over from Jules)",
                "",
                f"Apply these only when working in {src.full}" + (f" ({src.dir})." if src.dir else "."),
            ]
            lines += [""] + [f"- {m}" for m in src.memories]
            if src.test:
                lines += [f"- Run its tests with `{src.test}`."]
            lines += [end]
            block = "\n".join(lines)
            if start in text and end in text:
                text = text[: text.index(start)] + block + text[text.index(end) + len(end) :]
            else:
                text = text + ("\n" if text and not text.endswith("\n") else "") + ("\n" if text else "") + block + "\n"
            changed = True
        if changed:
            run.write(target, text)
            run.log(f"memories -> {target} ({agent})")


TASK_TEMPLATE = r"""#!/bin/bash -l
# Jules scheduled task "{title}", imported by jules_import.py on {today}.
# {schedule_line}
# Run it now: systemctl --user start jules-{slug}   (or: bash -l {self_path})
# Each run: a fresh worktree of {repo} from origin/{base}, the setup script, {agent} with the prompt
# below, then a pushed branch and a pull request with gh. Edit the prompt or the agent freely.
set -uo pipefail
REPO={repo_dir}
BASE={base}
AGENT={agent}
SETUP={setup}
TEST_CMD={test_cmd}
ENV_FILES=({env_files})
read -r -d '' PROMPT <<'{eof}'
{prompt}
{eof}

# shellcheck source=/dev/null
[ -f "$HOME/.jules-import/toolchain.sh" ] && . "$HOME/.jules-import/toolchain.sh"
# shellcheck source=/dev/null
for f in "${{ENV_FILES[@]}}"; do [ -f "$f" ] && {{ set -a; . "$f"; set +a; }}; done
stamp=$(date -u +%Y%m%d-%H%M%S)
branch="jules/{slug}-$stamp"
work="$HOME/.jules-import/runs/{slug}-$stamp"
log="$HOME/.jules-import/logs/{slug}-$stamp.log"
mkdir -p "$(dirname "$log")"
exec > >(tee -a "$log") 2>&1
echo "== {title}: $stamp, branch $branch"

git -C "$REPO" fetch --quiet origin "$BASE" || {{ echo "git fetch failed"; exit 1; }}
git -C "$REPO" worktree add --quiet -b "$branch" "$work" "origin/$BASE" || exit 1
cd "$work" || exit 1
[ -n "$SETUP" ] && [ -f "$SETUP" ] && {{ bash "$SETUP" || echo "setup script exited $? (continuing)"; }}
# What the setup script left behind (installs, build output) is not the agent's work: never commit it.
setup_left="$HOME/.jules-import/logs/{slug}-$stamp.setup-files"
{{ git ls-files --others --exclude-standard -z; git diff --name-only -z; }} > "$setup_left"

NOTE="You are running unattended on a schedule (a task carried over from Jules), in a fresh git worktree of {repo} on branch $branch, based on $BASE. Nobody is watching, so don't ask questions: make your best call and say why in the commit message."
[ -n "$TEST_CMD" ] && NOTE="$NOTE Run the tests with \`$TEST_CMD\` before you finish."
NOTE="$NOTE When you're done, commit your work on this branch. Don't push or open a pull request: the script that started you does that. If nothing is worth changing, commit nothing."
case "$AGENT" in
  codex) codex exec --dangerously-bypass-approvals-and-sandbox "$PROMPT

$NOTE" ;;
  *) claude -p --dangerously-skip-permissions "$PROMPT

$NOTE" ;;
esac
agent_rc=$?
echo "== agent exited $agent_rc"

git add -A
[ -s "$setup_left" ] && git reset --quiet --pathspec-from-file="$setup_left" --pathspec-file-nul
git diff --cached --quiet || git commit --quiet -m "{title}: changes from the $stamp run"
rm -f "$setup_left"
if [ "$(git rev-list --count "origin/$BASE..HEAD")" -eq 0 ]; then
  echo "== no changes; nothing to push"
  cd "$HOME" && git -C "$REPO" worktree remove --force "$work" && git -C "$REPO" branch -D "$branch" >/dev/null
  exit "$agent_rc"
fi
title=$(git log --reverse --format=%s "origin/$BASE..HEAD" | head -n 1)
if ! git push --quiet -u origin "$branch"; then
  echo "== git push failed; the work stays in $work on branch $branch"
  exit 1
fi
cd "$HOME" && git -C "$REPO" worktree remove --force "$work"
if gh pr create --repo {repo} --base "$BASE" --head "$branch" --title "$title" \
    --body "Scheduled task **{title}** (carried over from Jules), run $stamp on $(hostname) by $AGENT. Log: \`$log\`"; then
  echo "== pull request opened"
else
  echo "== pushed $branch, but gh couldn't open the pull request; open it on GitHub"
  exit 1
fi
"""


def task_script(t: ScheduledTask, src: Source, agent: str) -> str:
    eof = "JULES_PROMPT_" + secrets.token_hex(4).upper()
    env_files = " ".join(shlex.quote(str(p)) for p in (env_file(None), env_file(src)))
    setup = STATE / "setup" / f"{src.key}.sh" if src.setup.strip() else ""
    sched = (
        f"Schedule: {t.human} (OnCalendar {' | '.join(t.calendar)})"
        if t.calendar
        else "Schedule: none yet; set one with jules_import.py schedule " + t.slug + " 'SPEC'"
    )
    return TASK_TEMPLATE.format(
        title=(t.title or t.slug).replace('"', "'"),
        today=dt.date.today().isoformat(),
        schedule_line=sched,
        slug=t.slug,
        self_path=STATE / "tasks" / f"{t.slug}.sh",
        repo=src.full,
        repo_dir=shlex.quote(str(src.dir)),
        base=shlex.quote(t.branch or src.branch or "main"),
        agent=agent,
        setup=shlex.quote(str(setup)),
        test_cmd=shlex.quote(src.test),
        env_files=env_files,
        eof=eof,
        prompt=t.prompt.rstrip("\n"),
    )


def systemd_user() -> bool:
    if os.environ.get("JULES_IMPORT_NO_SYSTEMD"):
        return False
    return (
        bool(shutil.which("systemctl"))
        and subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True).returncode == 0
    )


def install_timer(run: Run, slug: str, title: str, calendar: list[str], enable: bool) -> bool:
    unit_dir = HOME / ".config" / "systemd" / "user"
    run.write(
        unit_dir / f"jules-{slug}.service",
        f"[Unit]\nDescription=Jules scheduled task: {title}\n\n[Service]\nType=oneshot\n"
        f"ExecStart=/bin/bash -l %h/.jules-import/tasks/{slug}.sh\nTimeoutStartSec=6h\n",
    )
    run.write(
        unit_dir / f"jules-{slug}.timer",
        f"[Unit]\nDescription=Jules scheduled task: {title}\n\n[Timer]\n"
        + "".join(f"OnCalendar={c}\n" for c in calendar)
        + "Persistent=true\n\n[Install]\nWantedBy=timers.target\n",
    )
    if run.dry:
        return True
    if run.sh(["systemctl", "--user", "daemon-reload"]) != 0:
        return False
    if enable:
        return run.sh(["systemctl", "--user", "enable", "--now", f"jules-{slug}.timer"]) == 0
    run.sh(["systemctl", "--user", "disable", "--now", f"jules-{slug}.timer"])
    return True


def read_sessions(export: Path, sources: list[Source]) -> list[tuple]:
    """One row per session, unfinished first, newest first; counts each repo's sessions."""
    rows = []
    for f in sorted((export / "sessions").glob("*/session.json")) if (export / "sessions").is_dir() else []:
        try:
            s = json.loads(f.read_text() or "{}")
        except ValueError:
            continue
        if not isinstance(s, dict):
            continue
        src = find_source(sources, s.get("sourceId"), create=False) if s.get("sourceId") else None
        if src:
            src.sessions += 1
        branch = ""
        for o in s.get("outputs", []) or []:
            if isinstance(o, dict):
                branch = (o.get("gitCommit") or {}).get("gitBranchName") or branch
        status = str(s.get("taskStatus", "")).upper()
        finished = any(w in status for w in ("COMPLETED", "FAILED", "CANCEL"))
        title = str(s.get("title") or s.get("description") or "").split("\n")[0][:80]
        rows.append(
            (
                finished,
                str(s.get("createdAt", "")),
                src.full if src else str(s.get("sourceId", "")),
                status,
                title,
                branch,
                f.parent,
            )
        )
    rows.sort(key=lambda r: r[1], reverse=True)
    rows.sort(key=lambda r: r[0])
    return rows


def write_sessions(run: Run, export: Path, rows: list[tuple]) -> None:
    if not rows:
        return
    lines = [
        "# Jules sessions",
        "",
        f"From {export}, unfinished ones first. To pick one up, ask your agent in the repo "
        "to read its summary.md and finish that work; its patches are next to it (`git apply patch-1.diff`).",
        "",
        "| Created | Repo | Status | Title | Branch | Summary |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for _, created, repo, status, title, branch, d in rows:
        lines.append(
            f"| {created[:10]} | {repo} | {status or '?'} | {title.replace('|', '/')} | {branch} | {d / 'summary.md'} |"
        )
    run.write(STATE / "sessions.md", "\n".join(lines) + "\n")
    run.log(f"{len(rows)} session(s) listed in {STATE / 'sessions.md'} ({sum(1 for r in rows if not r[0])} unfinished)")


def agent_signed_in(agent: str) -> bool:
    if agent == "codex":
        return (HOME / ".codex" / "auth.json").exists() or bool(
            os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_API_KEY")
        )
    claude_json = HOME / ".claude.json"
    return (
        (HOME / ".claude" / ".credentials.json").exists()
        or bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
        or bool(os.environ.get("ANTHROPIC_API_KEY"))
        or (claude_json.exists() and "oauthAccount" in claude_json.read_text(errors="ignore"))
    )


def main_import(args) -> int:
    export = Path(args.export).expanduser()
    if not export.is_dir() or not any(
        (export / f).exists() for f in ("sources.json", "sessions", "scheduled-tasks.json")
    ):
        print(
            f"{export} doesn't look like a Jules export (no sources.json, sessions/ or scheduled-tasks.json).\n"
            "Export first: https://jules.google/docs/changelog/2026-10-29",
            file=sys.stderr,
        )
        return 2
    run = Run(args)
    notes = run.notes
    sources = read_sources(export, notes)
    read_repo_folders(export, sources, notes)
    glob_env = read_env(export, sources, notes)
    tasks = read_tasks(export, sources, notes)
    sessions = read_sessions(export, sources)

    agent = args.agent or ("claude" if shutil.which("claude") or not shutil.which("codex") else "codex")
    if args.repos:
        wanted = {r.strip().lower() for r in args.repos.split(",") if r.strip()}
        chosen = [s for s in sources if s.full.lower() in wanted]
        for w in wanted - {s.full.lower() for s in chosen}:
            notes.append(f"--repos {w}: not in this export")
    elif args.all_repos:
        chosen = list(sources)
    else:
        task_repos = {id(t.source) for t in tasks}
        chosen = [s for s in sources if s.has_config() or s.sessions or id(s) in task_repos]
    skipped = len(sources) - len(chosen)
    code = Path(args.code_dir).expanduser()
    names = [s.repo.lower() for s in chosen]
    for s in chosen:
        s.dir = code / (f"{s.owner}-{s.repo}" if names.count(s.repo.lower()) > 1 else s.repo)

    print(
        f"Jules export: {export}\n  {len(sources)} repo(s) ({len(chosen)} to set up, {skipped} with nothing "
        f"to import{'; --all-repos clones them too' if skipped else ''}), {len(tasks)} scheduled task(s), "
        f"agent for scheduled runs: {agent}\n",
        flush=True,
    )

    use_gh = gh_ready()
    if not use_gh and not GIT_URL:
        notes.append(
            "the GitHub CLI isn't signed in here, so private repos won't clone and scheduled runs can't push "
            "or open pull requests. From your laptop: `prized agents handoff claude --gh`; or on the box: "
            "`gh auth login` then `gh auth setup-git`"
        )
    if not run.dry and subprocess.run(["git", "config", "user.email"], capture_output=True).returncode != 0:
        notes.append(
            "git has no user.email here, so scheduled runs can't commit: `prized agents handoff claude --gh` "
            "from your laptop sets it, or `git config --global user.email you@example.com`"
        )

    cloned = [s for s in chosen if clone(run, s, code, use_gh)]

    if not args.no_toolchain:
        wanted_tools = []
        for s in cloned:
            wanted_tools += [t for t in needed_tools(s) if t not in wanted_tools]
        missing = [t for t in wanted_tools if not tool_present(t)]
        log = STATE / "logs" / "toolchain.log"
        for t in missing:
            if install_tool(run, t, log) and not run.dry:
                run.done.append(f"installed {t}")
        if missing or wanted_tools:
            write_toolchain(run)
            if not run.dry:
                os.environ["PATH"] = os.pathsep.join(
                    [
                        str(OPT / "node" / "bin"),
                        str(OPT / "go" / "bin"),
                        str(HOME / ".bun" / "bin"),
                        str(HOME / ".cargo" / "bin"),
                        str(HOME / ".local" / "bin"),
                        os.environ.get("PATH", ""),
                    ]
                )

    write_env(run, None, glob_env)
    for s in cloned:
        write_env(run, s, s.env)
    if not args.no_setup:
        for s in cloned:
            run_setup(run, s, STATE / "logs" / f"setup-{s.key}.log")
    else:
        for s in cloned:
            if s.setup.strip():
                run.write(STATE / "setup" / f"{s.key}.sh", "#!/bin/bash\n" + s.setup, 0o755)
    write_memories(run, chosen)
    write_sessions(run, export, sessions)

    timers = 0
    if tasks:
        have_systemd = systemd_user() if not run.dry else True
        if not args.no_schedule and not have_systemd:
            notes.append(
                "no systemd user session here, so no timers were installed; each task's script says its "
                "schedule: add a cron line for it (cron reads UTC on a Prized box)"
            )
        for t in tasks:
            src = t.source if t.source in cloned else None
            if not src:
                notes.append(
                    f"scheduled task {t.title or t.slug!r}: its repo "
                    + (f"{t.source.full} wasn't cloned" if t.source else "isn't named in the export")
                    + "; skipped it. Its prompt starts: "
                    + " ".join(t.prompt.split())[:100]
                )
                continue
            path = STATE / "tasks" / f"{t.slug}.sh"
            run.write(path, task_script(t, src, agent), 0o755)
            problem = check_calendar(t.calendar) if t.calendar else "no schedule found"
            state = "paused in Jules, timer left off" if t.paused else ""
            if problem:
                notes.append(
                    f"scheduled task {t.slug}: {problem}. Raw entry: {json.dumps(t.raw)[:300]}\n      "
                    f"Set it with: python3 {STATE / 'jules_import.py'} schedule {t.slug} '*-*-* 09:00:00 UTC'"
                )
                run.log(f"task {t.slug} -> {path} (no timer yet)")
                continue
            if t.guess:
                notes.append(
                    f"scheduled task {t.slug}: {t.guess}; check `OnCalendar` in "
                    f"~/.config/systemd/user/jules-{t.slug}.timer"
                )
            if have_systemd and not args.no_schedule:
                ok = install_timer(run, t.slug, t.title or t.slug, t.calendar, enable=not t.paused)
                timers += ok and not t.paused
                run.log(
                    f"task {t.slug} -> {path}, timer jules-{t.slug}.timer: {t.human}" + (f" ({state})" if state else "")
                )
            else:
                run.log(f"task {t.slug} -> {path}: {t.human}")
    if not run.dry and Path(__file__).is_file() and Path(__file__).resolve() != (STATE / "jules_import.py").resolve():
        STATE.mkdir(parents=True, exist_ok=True)
        shutil.copy(__file__, STATE / "jules_import.py")

    print("\nDone." if not run.dry else "\nDry run: nothing was changed.")
    for line in run.done:
        print(f"  - {line}")
    if tasks and timers:
        print(
            f"\n  {timers} timer(s) on: `systemctl --user list-timers 'jules-*'` shows the next runs; "
            "`systemctl --user start jules-NAME` runs one now; logs in ~/.jules-import/logs.\n"
            "  A paused box runs no timers: a run it missed starts once when the box wakes. To run on time, "
            "turn auto-pause off for this box (`prized box auto-pause BOX off` on your laptop)."
        )
    if not agent_signed_in(agent):
        notes.append(
            f"{agent} isn't signed in on this box yet, so scheduled runs will fail: run `{agent}` once here, "
            f"or from your laptop `prized agents handoff {agent}`"
        )
    if notes:
        print("\nNeeds you:")
        for n in notes:
            print(f"  - {n}")
    if (export / "env-variables.json").exists():
        print(
            f"\n{export / 'env-variables.json'} holds your variable values in plain text; delete it once the "
            f"files in {STATE / 'env'} look right."
        )
    return 0


def main_schedule(args) -> int:
    """Set or change the schedule of an imported task."""
    if not (STATE / "tasks" / f"{args.name}.sh").exists():
        print(f"no task {args.name} in {STATE / 'tasks'}", file=sys.stderr)
        return 2
    specs = [args.spec]
    if len(args.spec.split()) == 5 or args.spec.startswith("@"):
        specs = cron_to_calendar(args.spec, args.tz or "UTC")
    problem = check_calendar(specs)
    if problem:
        print(f"systemd doesn't accept {specs}: {problem}", file=sys.stderr)
        return 2
    run = Run(argparse.Namespace(dry_run=False))
    if not install_timer(run, args.name, args.name, specs, enable=True):
        return 1
    print(f"jules-{args.name}.timer: {' | '.join(specs)}")
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["schedule"]:
        s = argparse.ArgumentParser(
            prog="jules_import.py schedule", description="Set or change an imported task's schedule."
        )
        s.add_argument("name", help="the task's name, as in ~/.jules-import/tasks/NAME.sh")
        s.add_argument("spec", help="systemd OnCalendar ('Mon *-*-* 09:00:00 America/New_York') or cron ('0 9 * * 1')")
        s.add_argument("--tz", help="time zone for a cron line (default UTC)")
        return main_schedule(s.parse_args(argv[1:]))
    p = argparse.ArgumentParser(
        prog="jules_import.py",
        description=__doc__.split("\n\n")[0],
        epilog="Also: jules_import.py schedule NAME SPEC sets an imported task's schedule.",
    )
    p.add_argument("export", nargs="?", default=str(HOME / "jules-export"), help="the export folder (~/jules-export)")
    p.add_argument("--dry-run", action="store_true", help="print what it would do; change nothing")
    p.add_argument("--agent", choices=["claude", "codex"], help="agent for scheduled runs (default: claude)")
    p.add_argument("--repos", help="only these repos, comma separated (owner/repo,...)")
    p.add_argument("--all-repos", action="store_true", help="also clone repos with no Jules configuration or sessions")
    p.add_argument("--code-dir", default=str(HOME / "code"), help="where repos are cloned (~/code)")
    p.add_argument("--no-setup", action="store_true", help="don't run setup scripts now")
    p.add_argument("--no-toolchain", action="store_true", help="don't install missing runtimes")
    p.add_argument("--no-schedule", action="store_true", help="write task scripts but install no timers")
    return main_import(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
