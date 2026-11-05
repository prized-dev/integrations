"""Offline check, no key and no network: the stand-in agent's steps give the score
curve the README shows, and one line at the worst step gets the final run to 10/10.

    python test_offline.py
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).parent
CURVE = [0, 2, 3, 6, 8, 4, 4, 6]  # hidden tests passed after each commit, out of 10


def score(repo):
    run = subprocess.run([sys.executable, "-B", "-"], input=(HERE / "hidden_test.py").read_text(), cwd=repo,
                         capture_output=True, text=True, check=True)
    return json.loads(run.stdout.split("@score ", 1)[1])["passed"]


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=True).stdout


with tempfile.TemporaryDirectory() as tmp:
    repo = os.path.join(tmp, "repo")
    env = dict(os.environ, AGENT_HOME=tmp, AGENT_REPO=repo)
    subprocess.run([sys.executable, str(HERE / "agent.py"), "0"], env=env, check=True, capture_output=True)
    assert os.path.exists(os.path.join(tmp, "ready"))
    assert "stalls" in json.load(open(os.path.join(tmp, "done")))
    got = []
    for commit in git(repo, "rev-list", "--reverse", "HEAD").split():
        git(repo, "checkout", "-q", commit)
        got.append(score(repo))
    assert got == CURVE, got
    path = os.path.join(repo, "roman.py")  # the final state, with the refactor's bug fixed
    src = open(path).read()
    open(path, "w").write(src.replace("NUMERALS.sort()", "NUMERALS.sort(reverse=True)"))
    assert score(repo) == 10
    open(path, "w").write("def to_roman(n):\n    while n == 4:\n        pass\n")  # hangs on 4; from_roman is gone
    assert score(repo) == 0
print("ok: curve", CURVE, "10/10 with the one-line fix, 0/10 for code that hangs")
