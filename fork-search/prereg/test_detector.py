"""python3 -I test_detector.py  (stdlib only)"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from detector import (CONFIRMATION_QUESTION, HARD_RULE_EXPLORATORY, NUDGE, Episode,  # noqa: E402
                      first_prompt, is_error_line, trigger)

ok = lambda: Episode(["ls\n"], "a.txt b.txt\n", False)  # noqa: E731
bad = lambda c="make\n": Episode([c], "make: *** [all] Error 2\n", False)  # noqa: E731
T, B = 900.0, 1.50

# nothing yet
assert trigger([], 0, T, 0, B) is None
# claim fires at any episode
assert trigger([Episode(["echo done\n"], "done\n", True)], 5, T, 0.01, B) == "claim"
# 3 errors, but too early (3 episodes)
assert trigger([bad(), bad(), bad()], 30, T, 0.02, B) is None
# 3 of the last 4 at 4 episodes -> hard (amendment 2026-11-07: the primary rule)
assert trigger([ok(), bad(), bad(), bad("cmake ..\n")], 40, T, 0.03, B) == "hard"
assert trigger([bad(), ok(), bad(), bad("cmake ..\n")], 40, T, 0.03, B) == "hard"
# 2 of the last 3 is no longer hard; it still is under the exploratory rule
assert trigger([ok(), ok(), bad(), bad("cmake ..\n")], 40, T, 0.03, B) is None
assert trigger([ok(), ok(), bad(), bad("cmake ..\n")], 40, T, 0.03, B, HARD_RULE_EXPLORATORY) == "hard"
# an old error slides out of the window: errors at episodes 1, 3, 5 of 5 -> 2 of the last 4
assert trigger([bad(), ok(), bad(), ok(), bad()], 50, T, 0.03, B) is None
# 1 error in the window -> not hard
assert trigger([ok(), ok(), ok(), bad()], 40, T, 0.03, B) is None
# rerunning a command that failed earlier (normal debugging) is not a hard step by itself
assert trigger([bad("pip install x\n"), ok(), ok(), Episode(["pip  install x\n"], "ok\n", False)], 40, T, 0.03, B) is None
# echoed command text alone is not an error signal
e = Episode(["grep -n error log.txt\n"], "$ grep -n error log.txt\n12:all good\n", False)
assert e.err is False
# heredoc body echoed with continuation prompts is not an error signal
h = Episode(["cat > t.py <<'EOF'\nraise ValueError('error: bad input')\nEOF\n"],
            "$ cat > t.py <<'EOF'\n> raise ValueError('error: bad input')\n> EOF\n$ \n", False)
assert h.err is False
# a long command wrapped by tmux, then real output
w = Episode(["python3 -c 'import sys; print(\"no error: all fine\")' && echo done\n"],
            "$ python3 -c 'import sys; print(\"no error: all\n fine\")' && echo done\nno error: all fine\ndone\n", False)
assert w.err is False
# a real error after an echoed command still counts
r = Episode(["python3 t.py\n"], "$ python3 t.py\nTraceback (most recent call last):\nValueError: error: bad input\n", False)
assert r.err is True
# at a trigger every arm gets the nudge, never Terminus 2's confirmation question, and the
# pending completion is cleared, so the lineage ends only on two later consecutive claims
# (amendment 2026-11-07); the prompt is the same in A and B, and C and D only add their note
prompts = {arm: first_prompt(arm, "$ ls\nout.txt") for arm in "ABCD"}
for arm, (prompt, pending) in prompts.items():
    assert prompt.endswith(NUDGE) and CONFIRMATION_QUESTION not in prompt and pending is False, arm
assert prompts["A"] == prompts["B"]
assert prompts["C"][0].endswith(prompts["A"][0]) and prompts["D"][0].endswith(prompts["A"][0])


def terminus_claims(claims, pending=False):
    """Terminus 2's completion logic (terminus_2.py:1619-1631, 1745-1748): index of the
    episode that ends the run, or None."""
    for i, c in enumerate(claims):
        if c and pending:
            return i
        pending = bool(c)
    return None


_, pending = first_prompt("A", "")
assert terminus_claims([True], pending) is None            # one claim after the nudge does not end it
assert terminus_claims([True, True], pending) == 1         # two consecutive claims do
assert terminus_claims([True, False, True], pending) is None
assert terminus_claims([True], True) == 0                  # what the harness avoids: Terminus's own pending flag
# cutoffs
assert trigger([ok()] * 5, 0.5 * T, T, 0.01, B) == "cutoff"
assert trigger([ok()] * 5, 10, T, 0.25 * B, B) == "cutoff"
# python traceback counts
assert Episode(["python3 a.py\n"], "Traceback (most recent call last):\n  File x\n", False).err
print("detector: all checks pass")

# ---- amendment 2026-11-07: regression snippets from public Terminus 2 trajectories ----
# (TB2.1 leaderboard Terminus 2 trials on Harbor Hub, and the DCAgent Haiku 4.5 TB2.0 run;
#  task and model given for each, trial id where one snippet is a whole episode)
P = "root@8cdca45d-74b0-4cb8-b8c0-362156cb5fe0:/app# "
# 1. cross-line match: r"\s\d" joined "find: write error" to the next line's "234343" once the
#    echoed command between them was dropped (reshard-c4-data, fable-5-high, 5e0cc305)
cmd = "find /app/c4_sample -type f -printf '%s\\n' | sort -n | tail -5 && which uv && python3 --version\n"
x = Episode(["find /app/c4_sample -maxdepth 2 | head -30\n", cmd],
            "/app/c4_sample/c4-mini-00028-of-10000.jsonl\nfind: \u2018standard output\u2019: Broken pipe\n"
            "find: write error\n" + P + cmd + "234343\n254441\n262108\n", False)
assert x.err is False
# 2. a printed metric is not an error (custom-memory-heap-crash, gemini-3-1-pro-high, ee22518d) ...
assert not is_error_line("Error: 0.000835")
assert not is_error_line("  Total error: 0.21029539")  # distribution-search, claude-haiku-4-5
# ... but the segfault in the same episode is
assert Episode(["/app/release\n"], "Actual \u03c0: 3.141593\nError: 0.000835\nAccuracy: 99.97%\n\n"
               "=== Computation completed successfully ===\nSegmentation fault (core dumped)\n", False).err
assert is_error_line("make: *** [Makefile:696: coldstart] Error 2")  # integers still count
# 3. `... | head` closing the pipe (make-mips-interpreter, fable-5-high and gpt-5-5-xhigh)
assert not is_error_line("grep: write error: Broken pipe")
assert not is_error_line("Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='utf-8'>")
# 4. source code on screen (cat/grep/sed of a file)
for code in ['        except Exception as e:',                                   # schemelike-metacircular-eval
             'class SchemeError(Exception):',                                  # schemelike-metacircular-eval
             '    raise Exception("Exiting due to local failures. ")',         # sanitize-git-repo
             'def retry(func, retries=5, delay=2, backoff=2, exceptions=(Exception,)):',  # mailman
             '        fprintf(stderr, "Error: Invalid plaintext format\\n");',  # feal-linear-cryptanalysis
             '            print(f"Error: {e}")',                               # schemelike-metacircular-eval
             ">     print(f'Error: {e}')",                                    # extract-moves-from-video
             '      console.error(`SYS_read error: ${error.message}`);',      # make-doom-for-mips
             '        print("SOME TESTS FAILED")']:                           # portfolio-optimization
    assert not is_error_line(code), code
# ... and printf templates that `strings` pulls out of a binary (path-tracing, fable-5-high and opus-4-7-max)
assert not is_error_line("Error: Could not open file %s for writing")
assert not is_error_line("Fatal glibc error: %s:%s (%s): assertion failed: %s")  # path-tracing-reverse
assert is_error_line("pip: 100% done, error: 3 packages failed")  # guard (synthetic): a bare % is no template
# ... while apostrophes in real messages do not hide them (circuit-fibsqrt, gemini-3-1-pro-high)
assert is_error_line("python3: can't open file '/app/gen3.py': [Errno 2] No such file or directory")
assert is_error_line("cp: cannot stat '/app/c4_sample/c4-mini-000-of-10000.jsonl': No such file or directory")
# 5. errors the 16 patterns missed
sx = Episode(["python3 -c \"from html.parser import HTMLParser; import sys; class MyHTMLParser(HTMLParser):\\n\"\n"],
             '  File "<string>", line 1\n    from html.parser import HTMLParser; import sys; class MyHTMLParser(HTMLParser):\n'
             '                                                    ^^^^^\nSyntaxError: invalid syntax\n'
             'root@25f88557-621a-4a06-9036-655c50cf5c44:/app#\n', False)
assert sx.err  # build-pov-ray, gemini-3-1-pro-high, 93738194: no "Traceback" line before it
for miss in ["Error in library(RcppEigen) : there is no package called \u2018RcppEigen\u2019",  # mcmc-sampling-stan
             "Error in line 7: '        org start'",                                     # winning-avg-corewars (pMARS)
             "E: Unable to locate package liblmdb-dev",                                   # caffe-cifar-10
             "npm ERR! code E404",                                                        # make-mips-interpreter
             "error[E0601]: `main` function not found in crate `main`",                   # polyglot-rust-c
             "curl: (7) Failed to connect to localhost port 8080 after 0 ms: Couldn't connect to server",  # nginx-request-logging
             "======================== 1 failed, 366 passed in 0.53s =========================",  # fix-code-vulnerability
             "AssertionError: Final states differ by 0.39435689555880465, exceeding tolerance 1e-05"]:  # tune-mjcf
    assert is_error_line(miss), miss
assert not is_error_line("Tests: 12 passed, 0 failed")
assert not is_error_line("E: -1.2345")  # guard (synthetic): a printed energy is not apt
assert not is_error_line("BrokenPipeError: [Errno 32] Broken pipe")
print("detector regressions (2026-11-07): all pass")
