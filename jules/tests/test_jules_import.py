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
        cal, human, tz, paused, guess = self.cal(
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
        cal, _, _, paused, _ = self.cal(
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
        self.assertEqual(self.cal({"frequency": "HOURLY", "timeOfDay": {"minutes": 5}})[0], ["*-*-* *:05:00"])
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
        cal, _, _, _, guess = self.cal(
            {"frequency": "DAILY", "nextRunTime": "2026-10-30T16:30:00Z", "timeZone": "America/Los_Angeles"}
        )
        self.assertEqual(cal, ["*-*-* 09:30:00 America/Los_Angeles"])
        self.assertIn("taken from", guess)

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
        self.assertIn("gh pr create --base master --head jules/edge-case-test-", calls)
        self.assertFalse(any((state / "runs").iterdir()), "worktree removed after the pull request")
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


if __name__ == "__main__":
    unittest.main()
