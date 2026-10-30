"""Offline tests for jules_import.py: the parsers, then a full import of sample-export into a throwaway HOME
with a local git remote and stand-ins for systemctl, gh and the agents (tests/bin), then one scheduled run."""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPT = ROOT / "jules_import.py"
SAMPLE = ROOT / "sample-export"
sys.path.insert(0, str(ROOT))
import jules_import as ji

HAVE_ANALYZE = bool(shutil.which("systemd-analyze"))


class Schedules(unittest.TestCase):
    def cal(self, raw):
        return ji.parse_schedule(raw)

    def test_daily_time_of_day_and_zone(self):
        cal, human, tz, paused, guess, why = self.cal(
            {
                "schedule": {
                    "frequency": "DAILY",
                    "timeOfDay": {"hours": 9, "minutes": 30},
                    "timeZone": "America/Los_Angeles",
                },
                "state": "ACTIVE",
            }
        )
        self.assertEqual(cal, ["*-*-* 09:30:00 America/Los_Angeles"])
        self.assertFalse(paused)
        self.assertEqual(guess, "")

    def test_weekly_days_paused_and_zero_minutes_dropped(self):
        cal, _, _, paused, _, _ = self.cal(
            {
                "schedule": {"frequency": "WEEKLY", "daysOfWeek": ["MONDAY", "THURSDAY"], "timeOfDay": {"hours": 7}},
                "state": "PAUSED",
            }
        )
        self.assertEqual(cal, ["Mon,Thu *-*-* 07:00:00 UTC"])
        self.assertTrue(paused)

    def test_flat_fields_and_string_time(self):
        cal, *_ = self.cal(
            {"cadence": "Weekly", "dayOfWeek": 5, "time": "6:15 pm", "tz": "Europe/Berlin", "enabled": True}
        )
        self.assertEqual(cal, ["Fri *-*-* 18:15:00 Europe/Berlin"])

    def test_weekdays_hourly_monthly(self):
        self.assertEqual(self.cal({"frequency": "WEEKDAYS", "time": "08:00"})[0], ["Mon..Fri *-*-* 08:00:00 UTC"])
        self.assertEqual(self.cal({"frequency": "HOURLY", "timeOfDay": {"minutes": 5}})[0], ["*-*-* *:05:00 UTC"])
        self.assertEqual(
            self.cal({"frequency": "MONTHLY", "dayOfMonth": 15, "time": "02:00"})[0], ["*-*-15 02:00:00 UTC"]
        )

    def test_cron_and_macros(self):
        self.assertEqual(
            self.cal({"cronExpression": "30 9 * * 1-5", "timeZone": "Asia/Tokyo"})[0],
            ["Mon,Tue,Wed,Thu,Fri *-*-* 09:30:00 Asia/Tokyo"],
        )
        self.assertEqual(self.cal({"schedule": "*/15 * * * *"})[0], ["*-*-* *:00,15,30,45:00 UTC"])
        self.assertEqual(self.cal({"cron": "@weekly"})[0], ["Sun *-*-* 00:00:00 UTC"])
        self.assertEqual(ji.cron_to_calendar("0 6 1 * MON", "UTC"), ["*-*-01 06:00:00 UTC", "Mon *-*-* 06:00:00 UTC"])

    def test_rrule(self):
        self.assertEqual(
            self.cal({"recurrence": "RRULE:FREQ=WEEKLY;BYDAY=TU,FR;BYHOUR=10;BYMINUTE=0"})[0],
            ["Tue,Fri *-*-* 10:00:00 UTC"],
        )

    def test_time_from_next_run_is_a_guess(self):
        cal, _, _, _, guess, _ = self.cal(
            {"frequency": "DAILY", "nextRunTime": "2026-10-30T16:30:00Z", "timeZone": "America/Los_Angeles"}
        )
        self.assertEqual(cal, ["*-*-* 09:30:00 America/Los_Angeles"])
        self.assertIn("taken from", guess)

    def test_rrule_details(self):
        cases = {
            "FREQ=HOURLY;INTERVAL=6;BYMINUTE=15": ["*-*-* 00/6:15:00 UTC"],
            "FREQ=DAILY;BYHOUR=9,17;BYMINUTE=0": ["*-*-* 09,17:00:00 UTC"],
            "FREQ=MONTHLY;BYDAY=1MO;BYHOUR=8": ["Mon *-*-01..07 08:00:00 UTC"],
            "FREQ=YEARLY;BYMONTH=3;BYMONTHDAY=15;BYHOUR=6": ["*-03-15 06:00:00 UTC"],
            "DTSTART;TZID=America/New_York:20261030T091500\nRRULE:FREQ=WEEKLY;BYDAY=TU": [
                "Tue *-*-* 09:15:00 America/New_York"
            ],
        }
        for rule, want in cases.items():
            cal, human, *_ = self.cal({"recurrence": rule})
            self.assertEqual(cal, want, rule)
            self.assertNotIn("\n", human)

    def test_what_a_timer_cant_say_is_refused(self):
        for raw in (
            {"recurrence": "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO"},
            {"recurrence": "FREQ=MONTHLY;BYDAY=-1FR"},
            {"recurrence": "FREQ=DAILY;BYHOUR=9AM"},
            {"frequency": "BIWEEKLY", "time": "09:00"},
            {"frequency": "WEEKLY", "interval": 2, "dayOfWeek": "MONDAY"},
            {"cron": "0 9 * * FRI-MON"},
            {"cron": "0 9 L * *"},
            {"frequency": "WEEKLY", "daysOfWeek": "1MO"},
            {"frequency": "DAILY", "minute": "30m", "hour": 9},
        ):
            cal, _, _, _, _, why = self.cal(raw)
            self.assertIsNone(cal, raw)
            self.assertTrue(why, raw)

    def test_cron_dom_and_dow(self):
        # Cron ORs the two day fields only when neither starts with "*".
        self.assertEqual(ji.cron_to_calendar("0 0 */10 * 1", "UTC"), ["Mon *-*-01,11,21,31 00:00:00 UTC"])

    def test_zones_days_and_sanitising(self):
        cal, _, tz, _, guess, _ = self.cal({"frequency": "DAILY", "time": "09:00", "timeZone": "america/los_angeles"})
        self.assertEqual((cal, guess), (["*-*-* 09:00:00 America/Los_Angeles"], ""))
        cal, _, _, _, guess, _ = self.cal({"frequency": "DAILY", "time": "09:00", "timeZone": "-07:00"})
        self.assertEqual(cal, ["*-*-* 09:00:00 Etc/GMT+7"])
        self.assertIn("no daylight saving", guess)
        cal, _, _, _, guess, _ = self.cal({"frequency": "DAILY", "time": "09:00", "timeZone": "Mars/Olympus"})
        self.assertEqual(cal, ["*-*-* 09:00:00 UTC"])
        self.assertIn("not recognised", guess)
        self.assertEqual(
            self.cal({"frequency": "WEEKLY", "daysOfWeek": "MON-FRI", "time": "8:00"})[0],
            ["Mon,Tue,Wed,Thu,Fri *-*-* 08:00:00 UTC"],
        )
        cal, _, _, _, guess, _ = self.cal(
            {"frequency": "DAILY", "time": {"seconds": 1793350800}, "nextRunTime": "2026-10-30T16:30:00Z"}
        )
        self.assertEqual(cal, ["*-*-* 16:30:00 UTC"])
        _, human, *_ = self.cal({"frequency": "DAILY\ntouch /tmp/pwned #", "time": "09:00"})
        self.assertNotIn("\n", human)

    def test_no_schedule(self):
        self.assertIsNone(self.cal({"frequency": "sometimes"})[0])
        self.assertIsNone(self.cal({"nextRunTime": "2026-10-30T16:30:00Z"})[0])

    @unittest.skipUnless(HAVE_ANALYZE, "no systemd-analyze")
    def test_systemd_accepts_what_we_write(self):
        for spec in (
            "*-*-* 09:30:00 America/Los_Angeles",
            "Mon,Thu *-*-* 07:00:00 UTC",
            "*-*-* *:00,15,30,45:00",
            "Mon..Fri *-*-* 08:00:00 UTC",
            "*-*-15 02:00:00 UTC",
            "*-01-01 00:00:00 UTC",
        ):
            self.assertEqual(ji.check_calendar([spec]), "", spec)
        self.assertNotEqual(ji.check_calendar(["Mon *-*-* 25:00:00"]), "")

    def test_times_and_days(self):
        self.assertEqual(ji.parse_time({"hours": 9}), (9, 0))
        self.assertEqual(ji.parse_time({}), (0, 0))
        self.assertEqual(ji.parse_time("12am"), (0, 0))
        self.assertEqual(ji.parse_time("12:30 PM"), (12, 30))
        self.assertIsNone(ji.parse_time("noonish"))
        self.assertEqual(ji.day_names(["SUNDAY", 1, "tue"]), ["Mon", "Tue", "Sun"])
        self.assertEqual(ji.day_names("MO,WE"), ["Mon", "Wed"])


class Shapes(unittest.TestCase):
    def export(self, files: dict) -> Path:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        for name, content in files.items():
            p = d / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content if isinstance(content, str) else json.dumps(content))
        return d

    def test_repo_references(self):
        for ref in (
            "github/o/r",
            "sources/github/o/r",
            "o/r",
            "https://github.com/o/r.git",
            {"githubRepo": {"owner": "o", "repo": "r"}},
            {"repoOwner": "o", "repoName": "r"},
            {"sweBotSource": {"githubSource": {"repoOwner": "o", "repoName": "r"}}},
        ):
            self.assertEqual(ji.owner_repo(ref), ("o", "r"), ref)
        self.assertIsNone(ji.owner_repo("12345"))
        self.assertIsNone(ji.owner_repo({"repoOwner": "o", "repoName": "r; rm -rf ~"}))

    def test_public_api_sources_and_folder_names(self):
        notes = []
        d = self.export(
            {
                "sources.json": {
                    "sources": [
                        {
                            "name": "sources/github/bobalover/boba",
                            "id": "github/bobalover/boba",
                            "githubRepo": {"owner": "bobalover", "repo": "boba"},
                        }
                    ]
                },
                "repos/github_bobalover_boba/test-command.txt": "pytest\n",
                "repos/sources_github_bobalover_boba/setup.sh": "pip install -e .\n",
                "repos/github_someone_else/setup.sh": "make\n",
            }
        )
        sources = ji.read_sources(d, notes)
        ji.read_repo_folders(d, sources, notes)
        self.assertEqual([s.full for s in sources], ["bobalover/boba"])
        self.assertEqual((sources[0].test, sources[0].setup), ("pytest", "pip install -e .\n"))
        self.assertTrue(any("github_someone_else" in n for n in notes))

    def test_env_shapes(self):
        cases = [
            {
                "environmentVariables": [
                    {"sourceId": "github/o/r", "name": "A", "value": "1"},
                    {"name": "G", "value": "global"},
                ]
            },
            {
                "sources": [{"sourceId": "github/o/r", "variables": [{"key": "A", "value": "1"}]}],
                "shared": [{"name": "envVars/G", "value": "global"}],
            },
            {"byRepo": [{"repo": {"owner": "o", "repo": "r"}, "env": {"A": "1"}}], "global": {"G": "global"}},
        ]
        for raw in cases:
            notes = []
            d = self.export({"sources.json": [{"sourceId": "github/o/r"}], "env-variables.json": raw})
            sources = ji.read_sources(d, notes)
            glob = ji.read_env(d, sources, notes)
            self.assertEqual(sources[0].env, {"A": "1"}, raw)
            self.assertEqual(glob, {"G": "global"}, raw)
        notes = []
        d = self.export({"env-variables.json": {"environmentVariables": [{"name": "bad name", "value": "x"}]}})
        ji.read_env(d, [], notes)
        self.assertTrue(any("shell name" in n for n in notes))

    def test_task_shapes(self):
        raw = {
            "items": [
                {
                    "name": "scheduledTasks/1",
                    "task": {"prompt": "do a", "sourceContext": {"source": "sources/github/o/r"}},
                    "cron": "0 9 * * *",
                },
                {"title": "B: things", "description": "do b", "repo": "r", "frequency": "DAILY"},
                {"prompt": "no schedule"},
            ]
        }
        notes = []
        d = self.export({"sources.json": [{"sourceId": "github/o/r"}], "scheduled-tasks.json": raw})
        sources = ji.read_sources(d, notes)
        tasks = ji.read_tasks(d, sources, notes)
        self.assertEqual([t.prompt for t in tasks], ["do a", "do b"])
        self.assertEqual([t.source.full for t in tasks], ["o/r", "o/r"])
        self.assertEqual([t.slug for t in tasks], ["do-a", "b-things"])
        self.assertEqual(tasks[1].calendar, ["*-*-* 09:00:00 UTC"])
        self.assertIn("09:00 picked", tasks[1].guess)

    def test_memories_from_markdown_when_no_json(self):
        notes = []
        d = self.export(
            {
                "sources.json": [{"sourceId": "github/o/r"}],
                "repos/github_o_r/memories.md": "# Memories\n\n- Use pnpm\n- None\n",
            }
        )
        sources = ji.read_sources(d, notes)
        ji.read_repo_folders(d, sources, notes)
        self.assertEqual(sources[0].memories, ["Use pnpm"])

    def test_prompt_survives_the_heredoc(self):
        src = ji.Source("o", "r", "main")
        src.dir = Path("/tmp/r")
        t = ji.ScheduledTask({"prompt": "-- careful\n  indented line\n", "cron": "0 9 * * *"}, src)
        t.slug = "careful"
        script = ji.task_script(t, src, "claude")
        head = script[: script.index("# shellcheck source=/dev/null")]
        r = subprocess.run(["bash", "-c", head + 'printf "%s" "$PROMPT"'], capture_output=True, text=True)
        self.assertEqual(r.stdout, "Task:\n-- careful\n  indented line\n")

    def test_not_an_export(self):
        d = self.export({"other.txt": "x"})
        r = subprocess.run([sys.executable, str(SCRIPT), str(d)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("doesn't look like a Jules export", r.stderr)


def git(*args, cwd=None, env=None):
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)


class EndToEnd(unittest.TestCase):
    """Import sample-export into a fresh HOME, then run one scheduled task, all offline."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.stub = self.tmp / "stub"
        self.stub.mkdir()
        (self.home / ".gitconfig").write_text(
            "[user]\n\tname = Test\n\temail = test@example.com\n[init]\n\tdefaultBranch = master\n"
        )
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / ".credentials.json").write_text("{}")
        self.env = {
            "PATH": f"{HERE / 'bin'}:{os.environ['PATH']}",
            "HOME": str(self.home),
            "STUB_LOG": str(self.stub / "calls.log"),
            "STUB_DIR": str(self.stub),
            "JULES_IMPORT_GIT_URL": f"file://{self.tmp}/remotes/{{owner}}/{{repo}}.git",
        }
        # The "GitHub" remote: a bare repo with a package.json and a test folder.
        seed = self.tmp / "seed"
        seed.mkdir()
        (seed / "package.json").write_text('{"name": "cors", "scripts": {"test": "true"}}\n')
        git("init", "-q", cwd=seed, env=self.env)
        git("add", ".", cwd=seed, env=self.env)
        git("commit", "-q", "-m", "init", cwd=seed, env=self.env)
        self.remote = self.tmp / "remotes" / "expressjs" / "cors.git"
        git("clone", "-q", "--bare", str(seed), str(self.remote), env=self.env)
        # The export, with a setup script that shows it ran with the repo's variables.
        self.export = self.tmp / "jules-export"
        shutil.copytree(SAMPLE, self.export)
        (self.export / "repos" / "github_expressjs_cors" / "setup.sh").write_text(
            'echo "$CORS_TEST_ORIGIN" > setup-ran.txt\necho "home is /home/jules/x"\n'
        )

    def run_import(self, *args):
        r = subprocess.run(
            [sys.executable, str(SCRIPT), str(self.export), "--no-toolchain", *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def test_import_then_a_scheduled_run(self):
        out = self.run_import()
        state = self.home / ".jules-import"
        repo = self.home / "code" / "cors"
        self.assertTrue((repo / ".git").is_dir())
        self.assertFalse((self.home / "code" / "cookie").exists(), "repos with nothing to import are skipped")

        env = state / "env" / "expressjs__cors.env"
        self.assertEqual(stat.S_IMODE(env.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((state / "env").stat().st_mode), 0o700)
        self.assertIn("FAKE_SERVICE_TOKEN=fake-0000-not-a-real-token", env.read_text())
        self.assertNotIn("fake-0000", out, "values never printed")
        self.assertEqual((repo / "setup-ran.txt").read_text().strip(), "https://app.example.test")
        self.assertIn("$HOME/x", (state / "setup" / "expressjs__cors.sh").read_text())

        for f in (self.home / ".claude" / "CLAUDE.md", self.home / ".codex" / "AGENTS.md"):
            text = f.read_text()
            self.assertIn("## expressjs/cors (carried over from Jules)", text)
            self.assertIn("- Tests use mocha with supertest", text)
            self.assertIn("Run its tests with `npm test`", text)
            self.assertNotIn("Node 10", text, "archived memories stay out")

        timer = (self.home / ".config/systemd/user/jules-edge-case-test.timer").read_text()
        self.assertIn("OnCalendar=*-*-* 09:30:00 America/Los_Angeles\n", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn(
            "ExecStart=/bin/bash -l %h/.jules-import/tasks/edge-case-test.sh",
            (self.home / ".config/systemd/user/jules-edge-case-test.service").read_text(),
        )
        calls = (self.stub / "calls.log").read_text()
        self.assertIn("systemctl --user enable --now jules-edge-case-test.timer", calls)
        self.assertIn("systemctl --user disable --now jules-bolt.timer", calls)
        self.assertNotIn("enable --now jules-bolt.timer", calls, "paused in Jules stays off")

        sessions = (state / "sessions.md").read_text().splitlines()
        rows = [line for line in sessions if line.startswith("| 2026")]
        self.assertIn("AWAITING_USER_FEEDBACK", rows[0], "unfinished first")
        self.assertIn("jules/origin-set", rows[0])

        if shutil.which("shellcheck"):
            for script in (state / "tasks").glob("*.sh"):
                r = subprocess.run(["shellcheck", "-s", "bash", str(script)], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stdout)

        # A scheduled run: worktree, setup, agent, commit, push, pull request.
        r = subprocess.run(
            ["bash", "-l", str(state / "tasks" / "edge-case-test.sh")],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        prompt = (self.stub / "prompt.txt").read_text()
        self.assertTrue(prompt.startswith("Run the test suite. If it passes, add one test"))
        self.assertIn("Run the tests with `npm test` before you finish.", prompt)
        self.assertIn(".jules-import/runs/edge-case-test-", (self.stub / "agent-cwd.txt").read_text())
        branches = subprocess.run(
            ["git", "branch", "--list", "jules/*"], cwd=self.remote, capture_output=True, text=True
        ).stdout
        self.assertIn("jules/edge-case-test-", branches)
        calls = (self.stub / "calls.log").read_text()
        self.assertIn("claude -p --dangerously-skip-permissions", calls)
        self.assertIn("gh pr create --repo expressjs/cors --base master --head jules/edge-case-test-", calls)
        self.assertEqual(
            [p for p in (state / "runs").iterdir() if not p.name.startswith(".")],
            [],
            "worktree removed after the pull request",
        )
        logs = list((state / "logs").glob("edge-case-test-*.log"))
        self.assertTrue(logs and "pull request opened" in logs[0].read_text())

        # Again: same files, one memory block per repo, existing clone reused.
        self.run_import("--agent", "codex")
        self.assertEqual((self.home / ".claude" / "CLAUDE.md").read_text().count("<!-- jules-import expressjs/cors"), 1)
        self.assertIn("AGENT=codex", (state / "tasks" / "edge-case-test.sh").read_text())

    def test_no_changes_means_no_pull_request(self):
        self.run_import("--no-schedule")
        script = self.home / ".jules-import" / "tasks" / "edge-case-test.sh"
        quiet = self.tmp / "quiet-bin"
        quiet.mkdir()
        (quiet / "claude").write_text("#!/bin/sh\nexit 0\n")
        (quiet / "claude").chmod(0o755)
        env = {**self.env, "PATH": f"{quiet}:{self.env['PATH']}"}
        r = subprocess.run(["bash", "-l", str(script)], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("no changes; nothing to push", r.stdout)
        self.assertNotIn("pr create", (self.stub / "calls.log").read_text())
        self.assertNotIn(
            "timer", " ".join(os.listdir(self.home / ".config")) if (self.home / ".config").exists() else ""
        )

    def test_pull_request_refused_after_push(self):
        self.run_import("--no-schedule")
        script = self.home / ".jules-import" / "tasks" / "edge-case-test.sh"
        nogh = self.tmp / "nogh-bin"
        nogh.mkdir()
        (nogh / "gh").write_text("#!/bin/sh\necho 'gh: not logged in' >&2\nexit 4\n")
        (nogh / "gh").chmod(0o755)
        env = {**self.env, "PATH": f"{nogh}:{self.env['PATH']}"}
        r = subprocess.run(["bash", "-l", str(script)], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("but gh couldn't open the pull request", r.stdout)
        branches = subprocess.run(
            ["git", "branch", "--list", "jules/*"], cwd=self.remote, capture_output=True, text=True
        ).stdout
        self.assertIn("jules/edge-case-test-", branches, "the branch is pushed anyway")

    def with_stub(self, name: str, body: str) -> dict:
        """self.env with a one-off stand-in for `name` first on PATH."""
        d = self.tmp / f"bin-{name}"
        d.mkdir(exist_ok=True)
        (d / name).write_text("#!/bin/sh\n" + body)
        (d / name).chmod(0o755)
        return {**self.env, "PATH": f"{d}:{self.env['PATH']}"}

    def run_task(self, slug: str, env=None):
        script = self.home / ".jules-import" / "tasks" / f"{slug}.sh"
        return subprocess.run(
            ["bash", "-l", str(script)], env=env or self.env, capture_output=True, text=True, timeout=120
        )

    def remote_files(self, prefix: str) -> tuple[str, list[str], str]:
        branch = subprocess.run(
            ["git", "branch", "--list", f"{prefix}*", "--format=%(refname:short)"],
            cwd=self.remote,
            capture_output=True,
            text=True,
        ).stdout.split()[-1]
        files = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", branch], cwd=self.remote, capture_output=True, text=True
        ).stdout.split()
        subject = subprocess.run(
            ["git", "log", "-1", "--format=%s", branch], cwd=self.remote, capture_output=True, text=True
        ).stdout.strip()
        return branch, files, subject

    def test_setup_leftovers_never_reach_the_branch(self):
        (self.export / "repos" / "github_expressjs_cors" / "setup.sh").write_text(
            'echo "TOKEN=$FAKE_SERVICE_TOKEN" > .env.local\necho "// setup" >> package.json\n'
        )
        self.run_import("--no-schedule")
        # An agent that commits everything itself, setup output included.
        env = self.with_stub(
            "claude", 'echo change > agent-was-here.txt\ngit add -A && git commit -q -m "Add the edge case"\n'
        )
        r = self.run_task("edge-case-test", env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        branch, files, subject = self.remote_files("jules/edge-case-test-")
        self.assertIn("agent-was-here.txt", files)
        self.assertNotIn(".env.local", files)
        self.assertEqual(subject, "Add the edge case")
        package = subprocess.run(
            ["git", "show", f"{branch}:package.json"], cwd=self.remote, capture_output=True, text=True
        ).stdout
        self.assertNotIn("// setup", package)

    def test_a_refused_commit_keeps_the_work(self):
        self.run_import("--no-schedule")
        hook = self.home / "code" / "cors" / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        r = self.run_task("edge-case-test")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("git commit failed", r.stdout)
        runs = [p for p in (self.home / ".jules-import" / "runs").iterdir() if p.is_dir()]
        self.assertTrue(runs and (runs[0] / "agent-was-here.txt").exists())

    def test_runs_that_fire_together(self):
        self.run_import("--no-schedule")
        script = self.home / ".jules-import" / "tasks"
        procs = [
            subprocess.Popen(
                ["bash", "-l", str(script / f"{slug}.sh")],
                env=self.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            for slug in ("edge-case-test", "bolt")
        ]
        outs = [p.communicate(timeout=120)[0] for p in procs]
        self.assertEqual([p.returncode for p in procs], [0, 0], outs)
        branches = subprocess.run(
            ["git", "branch", "--list", "jules/*"], cwd=self.remote, capture_output=True, text=True
        ).stdout
        self.assertIn("jules/edge-case-test-", branches)
        self.assertIn("jules/bolt-", branches)

    def test_running_it_again_keeps_what_you_changed(self):
        sources = json.loads((self.export / "sources.json").read_text())
        for s in sources:
            s["githubSource"].pop("defaultBranch")
        (self.export / "sources.json").write_text(json.dumps(sources))
        self.run_import()
        state = self.home / ".jules-import"
        (state / "env" / "expressjs__cors.env").open("a").write("CLAUDE_CODE_OAUTH_TOKEN=keep-me\n")
        (state / "tasks" / "edge-case-test.sh").open("a").write("# my edit\n")
        tasks = json.loads((self.export / "scheduled-tasks.json").read_text())
        tasks["scheduledTasks"][1]["title"] = "Bolt renamed"
        (self.export / "scheduled-tasks.json").write_text(json.dumps(tasks))
        out = self.run_import()
        env = (state / "env" / "expressjs__cors.env").read_text()
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN=keep-me", env)
        self.assertEqual(env.count("FAKE_SERVICE_TOKEN="), 1)
        self.assertTrue((state / "tasks" / "edge-case-test.sh").read_text().endswith("# my edit\n"))
        self.assertTrue((state / "tasks" / "edge-case-test.sh.new").exists())
        self.assertIn("changed since the last import", out)
        self.assertFalse((state / "tasks" / "bolt-renamed.sh").exists(), "a renamed task keeps its name")
        self.assertIn("BASE=master", (state / "tasks" / "bolt.sh").read_text(), "base branch from the clone")

    def test_dry_run_changes_nothing(self):
        out = self.run_import("--dry-run")
        self.assertIn("Dry run: nothing was changed.", out)
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), [".claude", ".gitconfig"])

    def test_schedule_command(self):
        self.run_import("--no-schedule")
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "schedule", "bolt", "0 7 * * 1"], env=self.env, capture_output=True, text=True
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(
            "OnCalendar=Mon *-*-* 07:00:00 UTC", (self.home / ".config/systemd/user/jules-bolt.timer").read_text()
        )


class Issues(unittest.TestCase):
    """`issues` and `poll`: label an issue, get a pull request; offline, against the same local remote."""

    setUp = EndToEnd.setUp

    def cli(self, *args, env=None, code=0):
        r = subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            env=env or self.env,
            cwd=self.tmp,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(r.returncode, code, r.stdout + r.stderr)
        return r.stdout

    def fixture(self, issues: list, events: dict, permissions: dict | None = None):
        (self.stub / "issues.json").write_text(json.dumps(issues))
        for n, evs in events.items():
            (self.stub / f"events-{n}.json").write_text(json.dumps(evs))
        for user, perm in (permissions or {}).items():
            (self.stub / f"permission-{user}.json").write_text(json.dumps(perm))

    @staticmethod
    def labeled(id_, who="me", when="2099-01-01T00:00:00Z", name="jules"):
        return {"id": id_, "event": "labeled", "label": {"name": name}, "actor": {"login": who}, "created_at": when}

    def state(self) -> dict:
        return json.loads((self.home / ".jules-import" / "issues.json").read_text())

    def calls(self) -> str:
        return (self.stub / "calls.log").read_text()

    def test_label_an_issue_get_a_pull_request(self):
        out = self.cli("issues", "expressjs/cors")
        self.assertIn("watching for issues labeled 'jules'", out)
        self.assertTrue((self.home / "code" / "cors" / ".git").is_dir())
        self.assertIn("gh label create jules --repo expressjs/cors", self.calls())
        self.assertIn("systemctl --user enable --now jules-issues.timer", self.calls())
        timer = (self.home / ".config/systemd/user/jules-issues.timer").read_text()
        self.assertIn("OnCalendar=*-*-* *:00/2:00\n", timer)
        self.assertIn("poll", (self.home / ".config/systemd/user/jules-issues.service").read_text())
        self.assertTrue((self.home / ".jules-import" / "jules_import.py").is_file())
        cfg = self.state()
        self.assertEqual((cfg["me"], cfg["repos"]["expressjs/cors"]["branch"]), ("me", "master"))

        # A title and body written to break out of the script: they stay text.
        body = "The README should say which origins are allowed.\nEOF\n`touch pwned-body`\n$(touch pwned-body2)"
        self.fixture(
            [{"number": 7, "title": 'Document origins "$(touch pwned-title)"', "body": body, "updatedAt": "u1"}],
            {7: [self.labeled(1, name="bug"), self.labeled(2, name="Jules")]},
        )
        self.cli("poll")
        prompt = (self.stub / "prompt.txt").read_text()
        self.assertTrue(prompt.startswith('GitHub issue #7 in expressjs/cors: Document origins "$(touch pwned-title)"'))
        self.assertIn(body, prompt)
        self.assertIn("You are running unattended on GitHub issue #7 of expressjs/cors", prompt)
        self.assertEqual(list(self.tmp.rglob("pwned*")), [], "nothing from the issue ran")
        calls = self.calls()
        self.assertIn("gh issue comment 7 --repo expressjs/cors --body Claude Code is working on this.", calls)
        self.assertIn("gh pr create --repo expressjs/cors --base master --head jules/issue-7-", calls)
        self.assertIn("--body Closes #7. Worked on by claude on", calls)
        self.assertIn("gh issue comment 7 --repo expressjs/cors --body Pull request: https://github.com/", calls)
        self.assertIn(
            "jules/issue-7-",
            subprocess.run(
                ["git", "branch", "--list", "jules/*"], cwd=self.remote, capture_output=True, text=True
            ).stdout,
        )
        st = self.state()["issues"]["expressjs/cors#7"]
        self.assertEqual((st["status"], st["event"]), ("pr", 2))
        self.assertTrue(st["pr"].startswith("https://github.com/expressjs/cors/pull/"))
        script = self.home / ".jules-import" / "issues" / "expressjs__cors-7.sh"
        self.assertEqual(stat.S_IMODE(script.stat().st_mode), 0o700)
        if shutil.which("shellcheck"):
            r = subprocess.run(["shellcheck", "-s", "bash", str(script)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout)

        # The next polls: our own comments changed the issue, but it ran once.
        self.fixture([{"number": 7, "title": "Document origins", "body": body, "updatedAt": "u2"}], {})
        self.cli("poll")
        self.cli("poll")
        self.assertEqual(self.calls().count("claude -p"), 1)
        self.assertEqual(self.calls().count("issues/7/events"), 2, "an issue that hasn't changed isn't looked at again")

        # Label removed and added again: it runs again.
        self.fixture(
            [{"number": 7, "title": "Document origins", "body": body, "updatedAt": "u3"}],
            {7: [self.labeled(2), {"id": 3, "event": "unlabeled", "label": {"name": "jules"}}, self.labeled(4)]},
        )
        self.cli("poll")
        self.assertEqual(self.calls().count("claude -p"), 2)
        self.assertEqual(self.state()["issues"]["expressjs/cors#7"]["event"], 4)

    def test_labels_that_dont_count(self):
        self.cli("issues", "expressjs/cors")
        self.fixture(
            [
                {"number": 8, "title": "Old", "body": "", "updatedAt": "a"},
                {"number": 9, "title": "Triager", "body": "", "updatedAt": "b"},
                {"number": 10, "title": "Template", "body": "", "updatedAt": "c"},
                {"number": 11, "title": "Maintainer", "body": "", "updatedAt": "d"},
            ],
            {
                8: [self.labeled(5, when="2001-01-01T00:00:00Z")],
                9: [self.labeled(6, who="triager")],
                10: [self.labeled(7, who="drive-by")],
                11: [self.labeled(8, who="maintainer")],
            },
            {
                "triager": {"permission": "read", "role_name": "triage"},
                "maintainer": {"permission": "write", "role_name": "maintain"},
            },
        )
        out = self.cli("poll", "--dry-run")
        self.assertIn("#8: labeled before watching started", out)
        self.assertIn("#9: skipped: the label was added by triager, who has triage access", out)
        self.assertIn("#10: skipped: couldn't check the access of drive-by", out)
        self.assertIn("#11: labeled by maintainer; would run claude on it", out)
        self.assertNotIn("claude -p", self.calls())
        self.assertEqual(self.state()["issues"], {}, "a dry run records nothing")

        self.cli("poll")
        calls = self.calls()
        self.assertEqual(calls.count("claude -p"), 1, "only the maintainer's label runs")
        for n in (8, 9, 10):
            self.assertNotIn(f"gh issue comment {n} ", calls, "skipped issues get no comment")
        issues = self.state()["issues"]
        self.assertEqual(issues["expressjs/cors#11"]["status"], "pr")
        self.assertTrue(issues["expressjs/cors#9"]["status"].startswith("skipped"))
        self.assertNotIn("status", issues["expressjs/cors#8"])

    def test_no_changes_and_failures_are_said_on_the_issue(self):
        self.cli("issues", "expressjs/cors")
        self.fixture([{"number": 12, "title": "Nothing to do", "body": "", "updatedAt": "a"}], {12: [self.labeled(9)]})
        quiet = self.tmp / "quiet-bin"
        quiet.mkdir()
        (quiet / "claude").write_text("#!/bin/sh\necho 'claude -p' >> \"$STUB_LOG\"\nexit 0\n")
        (quiet / "claude").chmod(0o755)
        self.cli("poll", env={**self.env, "PATH": f"{quiet}:{self.env['PATH']}"})
        calls = self.calls()
        self.assertIn(
            "gh issue comment 12 --repo expressjs/cors --body Claude Code finished without changing anything", calls
        )
        self.assertIn(".jules-import/logs/issue-expressjs__cors-12-", calls)
        self.assertNotIn("pr create", calls)
        self.assertEqual(self.state()["issues"]["expressjs/cors#12"]["status"], "none")

        # The remote is gone: the run fails, and the issue says so.
        self.fixture([{"number": 13, "title": "Push fails", "body": "", "updatedAt": "a"}], {13: [self.labeled(10)]})
        shutil.rmtree(self.remote)
        self.cli("poll")
        self.assertIn(
            "gh issue comment 13 --repo expressjs/cors --body The run on the box failed (git fetch failed)",
            self.calls(),
        )
        self.assertEqual(self.state()["issues"]["expressjs/cors#13"]["status"], "failed")

    def test_needs_a_signed_in_gh_and_a_real_repo(self):
        nogh = self.tmp / "nogh"
        nogh.mkdir()
        (nogh / "gh").write_text("#!/bin/sh\nexit 1\n")
        (nogh / "gh").chmod(0o755)
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "issues", "expressjs/cors"],
            env={**self.env, "PATH": f"{nogh}:{self.env['PATH']}"},
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 2)
        self.assertIn("isn't signed in", r.stderr)
        self.cli("issues", "not a repo; rm -rf ~", code=2)
        self.cli("issues", "expressjs/cors", "--label", "$(x)", code=2)
        self.assertIn("Not watching any repo", self.cli("poll"))


if __name__ == "__main__":
    unittest.main()
