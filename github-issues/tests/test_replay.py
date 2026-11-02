"""Offline tests for replays (`watch OWNER/REPO --replay`): the recorder, its redaction, the page, the server and the
pull request lines, against the same stand-ins as test_prized_issues.py. Fake secrets only."""

from __future__ import annotations

import base64
import gzip
import http.client
import json
import os
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from test_prized_issues import FULL, Box, sh

import prized_replay as rp

LINK = "https://box-7707-abc123.p.prized.dev"
CAN_TRACE = rp.probe()["ptrace"]
SUDO = shutil.which("sudo")


def pages(state_dir: Path) -> list[Path]:
    d = state_dir / "replays" / "pages"
    return sorted(d.glob("*.html"), key=lambda p: p.stat().st_mtime) if d.is_dir() else []


def page_data(page: Path) -> dict:
    html = page.read_text()
    raw = html.split('<script id="data" type="application/json">', 1)[1].split("</script>", 1)[0]
    return json.loads(raw)


class ReplayBox(Box):
    """A repo with a small test suite, a .env its setup writes from the box, and replays on."""

    def seed_files(self, seed: Path):
        (seed / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        (seed / "test_calc.py").write_text(
            "import unittest\nfrom calc import add\n\n\nclass T(unittest.TestCase):\n"
            "    def test_add(self):\n        self.assertEqual(add(2, 2), 4)\n"
        )
        (seed / ".gitignore").write_text("node_modules/\n.env\n")
        (seed / ".prized").mkdir()
        (seed / ".prized" / "setup.sh").write_text('cp "$HOME/app.env" .env 2>/dev/null || true\n')

    def setUp(self):
        super().setUp()
        self.token = "frtest_" + uuid.uuid4().hex[:24]
        self.dbpw = "pw" + uuid.uuid4().hex[:12]
        self.stripe = "sk_test_" + uuid.uuid4().hex[:24]
        self.ghp = "ghp_" + "Q" * 36
        (self.home / "app.env").write_text(
            f"DATABASE_URL=postgres://app:{self.dbpw}@db.internal:5432/app\nSTRIPE_SECRET_KEY={self.stripe}\nPORT=3000\n"
        )
        self.env["DEMO_API_TOKEN"] = self.token

    def label(self, title="add() subtracts", body="add(2, 2) should be 4", **kw) -> int:
        n = self.fake.open_issue(title, body, **kw)
        self.fake.label(n)
        return n

    def everything_public(self) -> str:
        """Every text that left the box: pull requests, comments, plus the replay files and the poller's state."""
        out = [json.dumps(self.fake.pulls), json.dumps(self.fake.comments), (self.state_dir / "state.json").read_text()]
        for p in pages(self.state_dir):
            out.append(p.read_text())
        for b in (self.state_dir / "replays" / "bundles").glob("*.json.gz"):
            out.append(gzip.decompress(b.read_bytes()).decode())
        return "\n".join(out)


class WatchReplayTest(ReplayBox):
    def test_replay_takes_on_off_or_a_members_link_only(self):
        r = self.run_cli("watch", FULL, "--replay", "https://evil.example/", stdin="github_pat_fine\n", ok=False)
        self.assertEqual(r.returncode, 2)
        self.assertIn("prized ports share 7707", r.stderr)
        r = self.run_cli("watch", FULL, "--replay", "http://box-7707-abc123.p.prized.dev/", stdin="x\n", ok=False)
        self.assertEqual(r.returncode, 2, "https only")
        out = self.watch("--replay").stdout
        self.assertTrue(self.state()["repos"][FULL]["replay"])
        self.assertIn("prized ports share 7707", out)
        self.assertTrue((self.state_dir / "prized_replay.py").exists(), "the timer's copy has its recorder beside it")
        unit = self.home / ".config" / "systemd" / "user" / "prized-issues-replays.service"
        self.assertIn("prized_replay.py serve --port 7707", unit.read_text())
        self.watch("--replay", LINK + "/some/path")
        self.assertEqual(self.state()["replay_link"], LINK)
        self.watch()  # watch again without the flag keeps it
        self.assertTrue(self.state()["repos"][FULL]["replay"])
        self.assertIn(f"replays {LINK}", self.run_cli().stdout)
        self.watch("--replay", "off")
        self.assertFalse(self.state()["repos"][FULL]["replay"])

    def test_replay_off_records_nothing(self):
        self.watch()
        n = self.label()
        self.plan({"write": {"calc.py": "def add(a, b):\n    return a + b\n"}, "commit": "Fix add"})
        self.poll()
        pr = self.fake.pulls[self.run_of(n)["pr"]]
        self.assertNotIn("Replay", pr["body"])
        self.assertFalse((self.state_dir / "replays").exists())


class ReplayRunTest(ReplayBox):
    def setUp(self):
        super().setUp()
        self.watch("--replay", LINK)

    def fix_and_test(self, test_cmd="python3 -m unittest -q", **extra):
        return {
            "write": {"calc.py": "def add(a, b):\n    return a + b\n"},
            "run": [test_cmd],
            "commit": "Fix add",
            **extra,
        }

    @unittest.skipUnless(CAN_TRACE, "this machine doesn't allow ptrace")
    def test_pull_request_links_the_replay_with_three_lines(self):
        n = self.label()
        self.plan(self.fix_and_test())
        self.poll()
        rec = self.run_of(n)
        body = self.fake.pulls[rec["pr"]]["body"]
        self.assertTrue(body.startswith(f"Closes #{n}."))
        [page] = pages(self.state_dir)
        self.assertIn(f"**[▶ Replay this run]({LINK}/r/{page.name})**", body)
        self.assertIn("**Files changed:** 1 (+1 \u22121) · `calc.py`", body)
        self.assertIn("**Commands run:** 1 by the agent", body)
        self.assertIn("**Tests:** passed · `python -m unittest` exited 0 (exit code seen on the box", body)
        self.assertEqual(rec["replay"], f"{LINK}/r/{page.name}")
        data = page_data(page)
        self.assertEqual(data["context"]["pr"], rec["pr"], "the page links back to its pull request")
        self.assertTrue(data["meta"]["ptrace"])
        tool = next(i for i in data["items"] if i["kind"] == "tool")
        self.assertEqual(tool["summary"], "python3 -m unittest -q")
        self.assertEqual([p["code"] for p in tool["procs"]], [0])
        self.assertIn("calc.py", json.dumps(data["snaps"]))
        self.assertTrue(list((self.state_dir / "replays" / "bundles").glob("*.json.gz")))

    @unittest.skipUnless(CAN_TRACE, "this machine doesn't allow ptrace")
    def test_a_failing_test_hidden_by_a_pipe_still_shows_failed(self):
        n = self.label()
        # The agent's tool sees `tail`'s exit code (0); the box sees the test runner's (1).
        self.plan({"run": ["python3 -m unittest -q 2>&1 | tail -1"], "write": {"x.txt": "1\n"}, "commit": "Try"})
        self.poll()
        body = self.fake.pulls[self.run_of(n)["pr"]]["body"]
        self.assertIn("**Tests:** FAILED · `python -m unittest` exited 1 (exit code seen on the box", body)

    def test_three_kinds_of_planted_secrets_never_leave_the_box(self):
        """An env var, a .env file and a token echoed to the terminal, put everywhere the recorder looks: in commands,
        their output, the terminal and the agent's final message. The run log on the box has them; nothing else."""
        n = self.label()
        self.plan(
            self.fix_and_test(
                run=[
                    "echo token=$DEMO_API_TOKEN",
                    "cat .env",
                    "echo $DEMO_API_TOKEN | base64",
                    "python3 -m unittest -q",
                ],
                print="leaked to the terminal: " + self.ghp + " and $DEMO_API_TOKEN",
                say_leaks=True,
            )
        )
        self.poll()
        self.assertEqual(self.run_of(n)["status"], "pr")
        [log] = (self.state_dir / "logs").glob("*.log")
        raw = log.read_text()
        planted = {"env var": self.token, ".env password": self.dbpw, ".env key": self.stripe, "echoed token": self.ghp}
        for name, value in planted.items():
            self.assertIn(value, raw, f"the turn really exercised {name}")
        public = self.everything_public()
        self.assertIn("Replay this run", public)
        for name, value in [*planted.items(), ("env var, base64", base64.b64encode(self.token.encode()).decode()[:20])]:
            for piece in (value, value[-12:]):
                self.assertNotIn(piece, public, f"{name} ({piece[:6]}...) left the box")
        self.assertIn("PORT=3000", public, "harmless settings stay readable")

    def test_the_test_commands_arguments_never_reach_the_pull_request(self):
        """The pull request is public and a command line can hold a key no rule knows: it names the runner only."""
        key = "Kx7Qm2Vr9Lp4Ns8Bt3Wd"
        (self.home / "keys").mkdir()
        (self.home / "keys" / "staging").write_text(key + "\n")  # not a credential file name, not an env var
        n = self.label()
        self.plan(self.fix_and_test('python3 -m unittest -q --locals -k "$(cat ~/keys/staging)" 2>/dev/null'))
        self.poll()
        body = self.fake.pulls[self.run_of(n)["pr"]]["body"]
        self.assertIn("`python -m unittest`", body)
        self.assertNotIn(key, body)
        self.assertNotIn(key[-12:], body)

    def test_without_ptrace_the_pull_request_says_so(self):
        self.env["PRIZED_ISSUES_REPLAY_PTRACE"] = "0"
        n = self.label()
        self.plan(self.fix_and_test())
        self.poll()
        body = self.fake.pulls[self.run_of(n)["pr"]]["body"]
        self.assertIn("Replay this run", body)
        self.assertIn("(as the agent's tool reported it)", body)
        self.assertIn("_Exit codes are as the agent reported them: process tracing is turned off", body)
        self.assertFalse(page_data(pages(self.state_dir)[0])["meta"]["ptrace"])

    def test_held_no_change_and_follow_up_comments_carry_their_replay(self):
        n = self.label(author="outsider")
        self.plan(
            {"write": {"calc.py": "def add(a, b):\n    return a + b\n"}},  # edits only: the agent can't commit
            self.fix_and_test(),
            {"say": "Nothing to change."},
            {"write": {"more.txt": "1\n"}, "commit": "more"},
        )
        self.poll()
        held = self.fake.bot_comments(n)[-1]
        self.assertIn("edits only", held)
        self.assertIn("Replay this run", held)
        self.fake.comment(n, "/prized push", author="owner")
        self.poll()
        pr = self.run_of(n)["pr"]
        self.assertIn("Replay this run", self.fake.pulls[pr]["body"], "the held run's replay rides along")
        m = self.label("b", "b")
        self.poll()
        self.fake.comment(self.run_of(m)["pr"], "/prized look again", author="owner")
        self.poll()
        said = self.fake.bot_comments(self.run_of(m)["pr"])[-1]
        self.assertIn("without changing anything", said)
        self.assertIn("Replay this run", said)
        self.fake.comment(self.run_of(m)["pr"], "/prized one more", author="owner")
        self.poll()
        said = self.fake.bot_comments(self.run_of(m)["pr"])[-1]
        self.assertTrue(said.startswith("Pushed `"))
        self.assertIn("Replay this run", said)
        self.assertEqual(len(pages(self.state_dir)), 4, "one replay per turn")

    def test_codex_runs_are_recorded_too(self):
        self.watch("--agent", "codex")
        n = self.label()
        self.plan(self.fix_and_test())
        self.poll()
        body = self.fake.pulls[self.run_of(n)["pr"]]["body"]
        self.assertIn("Replay this run", body)
        self.assertIn("**Commands run:** 1 by the agent", body)
        self.assertEqual(page_data(pages(self.state_dir)[0])["meta"]["agent"], "Codex")


@unittest.skipUnless(CAN_TRACE, "this machine doesn't allow ptrace")
class RecorderTest(unittest.TestCase):
    """The recorder on its own, in this process."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="prized-replay-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        (self.home / ".codex").mkdir(parents=True)
        self.addCleanup(setattr, rp, "HOME", rp.HOME)
        rp.HOME = self.home
        self.work = self.tmp / "work"
        self.work.mkdir()
        sh("git", "init", "-q", str(self.work))
        (self.work / "a.txt").write_text("x\n")
        sh("git", "-C", str(self.work), "add", "-A")
        sh("git", "-C", str(self.work), "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "i")
        self.base = sh("git", "-C", str(self.work), "rev-parse", "HEAD").strip()
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.home), "LANG": "C.UTF-8"}

    def record(self, script: str, timeout: float = 60, stdin: bytes = b"", **env):
        rec = rp.Recording(workdir=self.work)
        log = self.tmp / "run.log"
        with open(log, "ab") as f:
            rc = rec.run(["bash", "-c", script], self.work, {**self.env, **env}, stdin, f, timeout)
        return rec, rc, log.read_bytes()

    def report(self, rec, head=""):
        rep = rp.Report(
            rec,
            state_dir=self.tmp / "state",
            link=LINK,
            name=rp.new_name("acme__app", 1),
            wt=self.work,
            start=self.base,
            head=head or self.base,
            context={"repo": FULL, "issue": 1},
            title="#1 test",
            mode="auto",
        )
        self.assertTrue(rep.write(), rep.leaks)
        return rep

    def test_exit_codes_stdin_and_the_log_are_as_without_it(self):
        rec, rc, log = self.record('read x; echo "got $x"; ls /nonexistent; exit 3', stdin=b"hello\n")
        self.assertEqual(rc, 3)
        self.assertIn(b"got hello", log)
        self.assertIn(b"No such file", log)
        ls = next(p for p in rec.procs if p["argv"][:1] == ["ls"])
        self.assertEqual(ls["code"], 2)
        rec, rc, _ = self.record("kill -TERM $$")
        self.assertEqual(rc, -signal.SIGTERM)

    def test_a_turn_that_runs_out_of_time_and_its_leftovers_end(self):
        t = time.time()
        rec, rc, _ = self.record("(sleep 300 &) ; sleep 300", timeout=1)
        self.assertEqual(rc, -9)
        self.assertLess(time.time() - t, 15)
        rec, rc, _ = self.record("(setsid sleep 300 &) ; echo done")
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(rec.stats["killed_leftovers"], 1)
        sleeper = next(p for p in rec.procs if "sleep" in p["argv"][:2])
        stat = Path(f"/proc/{sleeper['pid']}/stat")
        self.assertTrue(not stat.exists() or stat.read_text().rsplit(")", 1)[1].split()[0] in "ZX", "it was killed")

    def test_shell_loops_and_forked_workers_soon_run_at_full_speed(self):
        script = (
            "seq 1 20000 | while read l; do :; done; "
            "python3 -c 'import os\nif os.fork() == 0:\n  [os.stat(\".\") for _ in range(50000)]\n  os._exit(0)\nos.wait()'"
        )
        t = time.time()
        rec, rc, _ = self.record(script)
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(rec.stats.get("full_speed", 0), 2, "the shell and the forked worker were let run")
        self.assertLess(rec.stats["stops"], 20000)
        self.assertLess(time.time() - t, 30)

    def test_a_line_too_long_to_keep_is_dropped_whole_never_cut(self):
        self.addCleanup(setattr, rp, "LINE_MAX", rp.LINE_MAX)
        rp.LINE_MAX = 4000
        tok = "tokZZ" + uuid.uuid4().hex + "Q9"
        rec, rc, raw = self.record(
            'python3 -c \'import sys; print("x" * 3990 + sys.argv[1] + "y" * 3000); print("after")\' "$T"', T=tok
        )
        self.assertIn(tok.encode(), raw, "the run log has every byte")
        kept = b"\n".join(line for _, line in rec.stream)
        self.assertNotIn(tok[:10].encode(), kept)
        self.assertNotIn(tok[-10:].encode(), kept)
        self.assertIn(b"after", kept)
        self.assertTrue(any("isn't in the replay" in n for n in rec.notes))

    def test_a_missing_agent_doesnt_start(self):
        rec = rp.Recording(workdir=self.work)
        with self.assertRaises(FileNotFoundError), open(self.tmp / "log", "ab") as f:
            rec.run(["no-such-agent-here"], self.work, self.env, b"", f, 10)

    @unittest.skipUnless(SUDO and os.stat(SUDO).st_mode & 0o4000, "no setuid sudo here")
    def test_setuid_programs_still_work_and_run_untraced(self):
        """Traced, sudo would start without its privileges and fail; the recorder lets it go just before it starts."""
        rec, rc, log = self.record("sudo -V >/dev/null && echo sudo-ok; exec sudo -V >/dev/null")
        if not rec.sysinfo:
            self.skipTest("this kernel has no PTRACE_GET_SYSCALL_INFO")
        self.assertIn(b"sudo-ok", log)
        self.assertEqual(rc, 0)
        self.assertEqual(rec.stats["untraced"], 2)
        self.assertTrue(all(p["untraced"] for p in rec.procs if p["argv"][:1] == ["sudo"]))

    def test_a_hostile_turn_leaks_nothing(self):
        """The leaks an independent review found in the prototype (11-02), kept as a regression test."""
        r = lambda n: uuid.uuid4().hex[:n]  # noqa: E731
        tok = "tokAAAA111" + r(16) + "Zz"
        multi = "-----BEGIN OPENSSH KEYX-----\nmultiLINEsecret" + r(20) + "\nsecondLINE" + r(20) + "\n-----END-----"
        gcs = "gcs" + r(20) + "X9"
        dbpw, stripe, codex = "pw" + r(14) + "Q7", "sk_test_" + r(24), "cdx" + r(24) + "K8"
        (self.work / ".env").write_text(
            f"DATABASE_URL=postgres://app:{dbpw}@db:5432/app\nSTRIPE_SECRET_KEY={stripe}\nPORT=3000\n"
        )
        (self.home / ".codex" / "auth.json").write_text(json.dumps({"tokens": {"access_token": codex}}))
        script = r"""
echo "plain: $FAKE_API_TOKEN"
echo "b64: $(printf %s "user:$FAKE_API_TOKEN" | base64 -w0)"
printf 'ansi: %s\e[1m%s\e[0m\n' "${FAKE_API_TOKEN:0:12}" "${FAKE_API_TOKEN:12}"
printf 'multi:\n%s\n' "$MULTI_KEY"
echo "$GOOGLE_CREDENTIALS" | python3 -m json.tool
bash -c 'export INNER_TOKEN=innerTOK5566778899aa; echo "inner: $INNER_TOKEN"'
cat .env; rm .env
echo "Server listening on http://localhost:3000"
cat "$HOME/.codex/auth.json"
/bin/true "$(printf 'a%.0s' $(seq 8180))$FAKE_API_TOKEN"
touch "cache-$FAKE_API_TOKEN.json"
printf 'tokenizer = AutoTokenizer.from_pretrained("bert-base")\n' > model.py
printf 'Note <!--<script> and </script> in the README\n'
# a leftover that waits for the turn to end, then tries to write the raw token where the replay goes
setsid bash -c 'sleep 3; mkdir -p "$PAGES"; echo "raw $FAKE_API_TOKEN" > "$PAGES/evil.html"' </dev/null >/dev/null 2>&1 &
"""
        state = self.tmp / "state"
        rec, rc, raw = self.record(
            script,
            FAKE_API_TOKEN=tok,
            MULTI_KEY=multi,
            GOOGLE_CREDENTIALS=json.dumps({"type": "service_account", "client_secret": gcs}),
            PAGES=str(state / "replays" / "pages"),
        )
        self.assertEqual(rc, 0, raw[-500:])
        self.assertIn(tok.encode(), raw, "the turn really put it on screen")
        sh("git", "-C", str(self.work), "add", "-A")
        sh("git", "-C", str(self.work), "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "turn")
        head = sh("git", "-C", str(self.work), "rev-parse", "HEAD").strip()
        rep = self.report(rec, head)
        time.sleep(3.5)  # the leftover had its chance
        self.assertFalse((state / "replays" / "pages" / "evil.html").exists(), "leftovers end with the turn")
        page = rep.page_path.read_text()
        bundle = gzip.decompress(rep.bundle_path.read_bytes()).decode()
        lines = "\n".join(rep.lines)
        secrets = {
            "token": tok,
            "token base64": base64.b64encode(f"user:{tok}".encode()).decode()[8:30],
            "db password": dbpw,
            "stripe": stripe,
            "codex auth": codex,
            "inner export": "innerTOK5566778899aa",
            "gcred client_secret": gcs,
        }
        for i, line in enumerate(multi.splitlines()[1:3]):
            secrets[f"multi line {i}"] = line
        for where, text in (("bundle", bundle), ("page", page), ("pull request lines", lines)):
            for name, v in secrets.items():
                for piece in (v, v[:12], v[-12:]):
                    self.assertNotIn(piece, text, f"{name} ({piece[:6]}...) in the {where}")
        self.assertIn("localhost:3000", bundle)
        self.assertIn("AutoTokenizer.from_pretrained", bundle)
        self.assertIn("PORT=3000", bundle)
        data_block = page.split('<script id="data" type="application/json">', 1)[1].split("</script>", 1)[0]
        self.assertNotIn("<", data_block, "agent text can't end or hide the data block")


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="prized-replay-pages-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "acme__app-1-20261102-000000-0123456789abcdef.html").write_text("<p>replay</p>")
        (self.root.parent / f"{self.root.name}-secret.html").write_text("outside")
        self.addCleanup((self.root.parent / f"{self.root.name}-secret.html").unlink)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        started = threading.Event()
        box = []
        threading.Thread(
            target=rp.serve, args=(self.port, self.root, lambda h: (box.append(h), started.set())), daemon=True
        ).start()
        started.wait(5)
        self.addCleanup(box[0].shutdown)

    def get(self, path, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", path, headers={"Host": host or f"localhost:{self.port}"})
        r = c.getresponse()
        return r.status, dict(r.getheaders()), r.read().decode()

    def test_serves_pages_only_to_localhost_names(self):
        status, headers, body = self.get("/r/acme__app-1-20261102-000000-0123456789abcdef.html?x=1")
        self.assertEqual((status, body), (200, "<p>replay</p>"))
        self.assertIn("connect-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.get("/r/acme__app-1-20261102-000000-0123456789abcdef.html", "evil.example")[0], 404)
        for path in ("/", "/r/", f"/r/../{self.root.name}-secret.html", "/r/%2e%2e/x.html", "/r/short.html"):
            self.assertEqual(self.get(path)[0], 404, path)


class UnitTest(unittest.TestCase):
    def test_variants_and_key_formats(self):
        red = rp.Redactor(["s3cretVALUE12345"])
        text = "a s3cretVALUE12345 b " + base64.b64encode(b"u:s3cretVALUE12345").decode() + " sk-ant-" + "x" * 30
        out = red.text(text + " dcp_" + "A" * 24 + " https://u:p4ssw0rd@h/")
        for bad in ("s3cretVALUE", "sk-ant-x", "dcp_AAA", "p4ssw0rd"):
            self.assertNotIn(bad, out)
        self.assertEqual(red.leaks(out), [])
        self.assertEqual(red.leaks("x s3cretVALUE12345"), ["a secret value"])

    def test_headers_flags_urls_and_password_words(self):
        red = rp.Redactor(
            ["postgres://app:pwABCdef123456@db:5432/app", json.dumps({"client_secret": "gcsXYZ1234567890abc"})]
        )
        masked = {
            "Authorization: Bearer Kx7Qm2Vr9Lp4Ns8Bt3Wd": "Kx7Qm2",
            "X-API-Key: Kx7Qm2Vr9Lp4Ns8Bt3Wd": "Kx7Qm2",
            "curl -u x --api-key Kx7Qm2Vr9Lp4Ns8Bt3Wd": "Kx7Qm2",
            "tool --token=Kx7Qm2Vr9Lp4Ns8Bt3Wd": "Kx7Qm2",
            "redis://:Rd5hunterQ9xw2@cache:6379/0": "Rd5hunter",
            "the password alone: pwABCdef123456": "pwABCdef",
            "gcsXYZ1234567890abc": "gcsXYZ",
            "password: correcthorsebatterystaple": "correcthorse",
            "PASSPHRASE=hunterHUNTER42x": "hunterHUNTER",
        }
        for text, secret in masked.items():
            self.assertNotIn(secret, red.text(text), text)
        kept = [
            "Bearer authentication is used",
            "password: ${DB_PASSWORD}",
            "password = getpass()",
            "mkdir -p build",
            "tokenizer --token-file /tmp/x",
            'tokenizer = AutoTokenizer.from_pretrained("bert-base")',
        ]
        for text in kept:
            self.assertEqual(red.text(text), text)

    def test_the_test_runner_is_named_from_a_fixed_list(self):
        for cmd, label in (
            ("/tmp/.venv/bin/python -m pytest tests/ -q --api-key SECRET123", "python -m pytest"),
            ("npm run test -- --token=abc", "npm test"),
            ("cd x && py.test -k foo", "pytest"),
            ("go test ./...", "go test"),
            ("bash -c ls", "tests"),
        ):
            self.assertEqual(rp.test_label(cmd), label)

    def test_summary_lines(self):
        sm = {
            "commands": 3,
            "failed": 1,
            "traced": True,
            "processes": 9,
            "tests": {"cmd": "pytest -q", "code": 0, "seen": True},
        }
        lines = rp.summary_lines(sm, [["M", "a.py", 3, 1, ""], ["A", "b`x.py", 2, 0, ""]])
        self.assertEqual(lines[0], "**Files changed:** 2 (+5 \u22121) · `a.py`, `b'x.py`")
        self.assertEqual(lines[1], "**Commands run:** 3 by the agent, 1 exited non-zero (9 processes in all)")
        self.assertIn("passed · `pytest -q` exited 0", lines[2])
        sm.update(traced=False, tests=None, failed=0)
        self.assertEqual(
            rp.summary_lines(sm, [])[1:], ["**Commands run:** 3 by the agent", "**Tests:** none run by the agent"]
        )

    def test_codex_stream(self):
        lines = [
            {"type": "thread.started", "thread_id": "th1"},
            {
                "type": "item.started",
                "item": {"id": "i1", "type": "command_execution", "command": "bash -lc 'go test ./...'"},
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "i1",
                    "type": "command_execution",
                    "command": "x",
                    "aggregated_output": "ok",
                    "exit_code": 1,
                    "status": "failed",
                },
            },
            {"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": "done"}},
        ]
        events, meta, _ = rp.parse_stream([(10.0 + i, json.dumps(d).encode()) for i, d in enumerate(lines)], 10.0)
        self.assertEqual(meta["agent"], "Codex")
        [tool, say] = events
        self.assertEqual((tool["t"], tool["t_end"], tool["exit_code"], tool["is_error"]), (1.0, 2.0, 1, True))
        self.assertEqual(say["text"], "done")


if __name__ == "__main__":
    unittest.main()
