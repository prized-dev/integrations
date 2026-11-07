"""Pre-registered fork trigger for fork-search (frozen with PREREG.md).

Pure function over a lineage's episode records; no model calls, no hidden tests.
The harness calls `trigger(...)` after every Terminus-2 episode of a lineage.
The first non-None result fires once per lineage:
  - in the fork arms (B, C, D) it pauses the parent and forks it;
  - in best-of-4 (A) it only sends NUDGE to that attempt.
Every arm then continues with `after_trigger(...)`: the same nudge prompt, which at a
claim replaces Terminus 2's confirmation question (amendment 2026-11-07).

Amendment 2026-11-07, before any pilot run: "hard" is 3 of the last 4 episodes (was 2 of
the last 3, kept as HARD_RULE_EXPLORATORY and logged). On 5,981 public Terminus 2
trajectories the old rule was the first trigger in 33% (Haiku 4.5: 41%), and in a
hand-check 18 of 20 of its fires were real errors but only 4 of 20 an agent stuck on a
step; the new rule fires first in 17% (Haiku: 25%) and kept all 4 stuck cases.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

NUDGE = (
    "Checkpoint: before you continue, re-read the task, check the current state of "
    "the machine against every requirement, and if your current approach is not "
    "working, try a different one."
)

# Arm C (fresh context): prepended to Terminus 2's own initial prompt for each child.
FRESH_NOTE = (
    "Note: an earlier attempt already worked on this task on this machine. Its changes "
    "(files, installed packages, running processes) are still in place. Inspect the "
    "current state before acting."
)
# Arm D (files only, context kept): sent with the nudge as the child's next message.
FILES_NOTE = (
    "Note: the machine was reset to the task's starting image. Only the files under {workdir} "
    "were kept from your work so far; installed packages, running processes and files "
    "elsewhere are gone."
)

# Error signals in an episode's new terminal output (case-sensitive, any match), checked
# line by line on the lines that survive the echo filter.
ERROR_PATTERNS = [
    r"Traceback \(most recent call last\)",
    r"\b(Error|ERROR|error)(:|\s\d)",
    r"\bFAILED\b|\bFAIL\b",
    r"command not found",
    r"No such file or directory",
    r"Permission denied",
    r"Segmentation fault|core dumped",
    r"\bfatal:",
    r"\bException\b",
    r"undefined reference",
    r"cannot (open|find|access|stat|import|create)",
    r"ModuleNotFoundError|ImportError",
    r"make(\[\d+\])?: \*\*\*",
    r"exit (status|code) [1-9]",
    r"returned non-zero exit status",
    r"^Killed$",
    # amendment 2026-11-07 (public-trajectory replay): errors the 16 patterns missed
    r"^\s*(?:\w+\.)*\w*(?<!BrokenPipe)(?:Error|Exception): ",  # Python error line with no traceback
    r"^Error in \S",                 # R and pMARS: "Error in library(x) :", "Error in line 7:"
    r"^E: [A-Z]",                     # apt-get / dpkg: "E: Unable to locate package x"
    r"\bnpm ERR!",
    r"\berror\[E\d+\]",             # rustc
    r"^curl: \(\d+\) ",              # curl transport failure
    r"\b[1-9]\d* failed\b",          # pytest summary "1 failed, 3 passed"
]
ERROR_RE = re.compile("|".join(f"(?:{p})" for p in ERROR_PATTERNS), re.M)

# amendment 2026-11-07: lines that match ERROR_RE but report no failure
BENIGN_PATTERNS = [
    # a printed metric: "Error: 0.000835", "Total error: 0.21", "error = 1.2e-05"
    r"\b(?:[Ee]rror|ERROR)\s*[:=]?\s*[-+]?(?:\d+\.\d*|\.\d+|\d+(?:\.\d+)?[eE][-+]?\d+)\s*%?\s*$",
    # a reader closed the pipe early (`... | head`)
    r"write error: Broken pipe|Exception ignored in: <_io\.TextIOWrapper name='<stdout>'|BrokenPipeError",
    # source code on screen: except/raise/throw/catch ... Exception, (Exception), class X(Exception)
    r"\b(?:except|raise|throw|catch)\b.*\bException\b|\(\s*Exception\s*[,)]|class \w+\(\w*Exception\)",
    # a printf template, e.g. from `strings` on a binary: "Error: Could not open file %s for writing"
    r"(?<![\d%])%[-+0#]?\d*(?:\.\d+)?(?:ll|l|h|z)?[sdifgxXcpu]\b",
]
BENIGN_RE = re.compile("|".join(f"(?:{p})" for p in BENIGN_PATTERNS))


def _in_literal(line: str, pos: int) -> bool:
    """Is pos inside a quoted string literal (source code on screen, e.g. print("Error: x"))?
    An apostrophe opens a literal only after one of ( = , [ { : space, or an f/b/r prefix."""
    q = None
    for i, ch in enumerate(line[:pos]):
        if q:
            if ch == q and line[i - 1] != "\\":
                q = None
        elif ch in "\"`":
            q = ch
        elif ch == "'":
            before = line[i - 1] if i else " "
            if before in " (=,[{:" or (before in "fbrFBR" and (i < 2 or line[i - 2] in " (=,[{:")):
                q = ch
    return q is not None


def is_error_line(line: str) -> bool:
    """One output line reports an error: some pattern matches, the line is not benign, and
    not every match sits inside a string literal."""
    ms = list(ERROR_RE.finditer(line))
    if not ms or BENIGN_RE.search(line):
        return False
    return not all(_in_literal(line, m.start()) for m in ms)


MIN_EPISODES_FOR_HARD = 4  # a hard step needs at least 4 completed episodes
WINDOW = 4                 # look at the last 4 episodes
MIN_ERR_IN_WINDOW = 3      # at least 3 of them with an error signal
HARD_RULE = (WINDOW, MIN_ERR_IN_WINDOW)
HARD_RULE_EXPLORATORY = (3, 2)  # the rule registered first (2 of the last 3); logged, never acted on
TIME_CUTOFF = 0.5          # fraction of the task's agent timeout T
SPEND_CUTOFF = 0.25        # fraction of the arm budget B


@dataclass
class Episode:
    commands: list[str]          # keystroke batches Terminus-2 sent this episode
    output: str                  # new terminal output observed after them
    claimed_complete: bool       # the model set task_complete this episode
    err: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        # every line the agent typed (heredoc bodies included)
        typed = [ln.strip() for c in self.commands for ln in c.splitlines() if ln.strip()]
        blob = "\n".join(typed)

        def echoed(line: str) -> bool:
            s = line.strip()
            if not s:
                return False
            for t in typed:  # a prompt ($ # >) followed by a typed line
                if s.endswith(t) and re.search(r"[$#>]\s*$", s[: len(s) - len(t)]):
                    return True
            # a piece of a typed line (tmux wraps long ones), or text the agent itself
            # printed; a leading shell prompt is ignored
            s = re.sub(r"^(\S*[$#>]|>)\s+", "", s, count=1)
            return len(s) >= 8 and s in blob

        # per line (amendment 2026-11-07): joining lines let r"\s\d" match across a newline
        self.err = any(is_error_line(line) for line in self.output.splitlines() if not echoed(line))

    def batch_key(self) -> str:
        return "\n".join(" ".join(c.split()) for c in self.commands if c.strip())


def is_hard(episodes: list[Episode], rule: tuple[int, int] = HARD_RULE) -> bool:
    """From episode MIN_EPISODES_FOR_HARD, at least rule[1] of the last rule[0] episodes erred."""
    window, min_err = rule
    return len(episodes) >= MIN_EPISODES_FOR_HARD and sum(e.err for e in episodes[-window:]) >= min_err


def trigger(episodes: list[Episode], elapsed_s: float, timeout_s: float,
            spend_usd: float, budget_usd: float, hard_rule: tuple[int, int] = HARD_RULE) -> str | None:
    """Return 'claim', 'hard', 'cutoff' or None after the latest episode. The harness acts on
    the default rule only; it also logs the first episode where HARD_RULE_EXPLORATORY holds."""
    if not episodes:
        return None
    if episodes[-1].claimed_complete:
        return "claim"
    if is_hard(episodes, hard_rule):
        return "hard"
    if elapsed_s >= TIME_CUTOFF * timeout_s or spend_usd >= SPEND_CUTOFF * budget_usd:
        return "cutoff"
    return None


# Terminus 2 answers a lineage's first task_complete with this question and sets
# _pending_completion (terminus_2.py:716-732, 1619-1631); a second claim ends the run
# (terminus_2.py:1745-1748). The harness never sends it at a trigger.
CONFIRMATION_QUESTION = "Are you sure you want to mark the task as complete?"


def after_trigger(terminal_output: str) -> tuple[str, bool]:
    """The next prompt at a lineage's first trigger, identical in every arm and for every
    trigger kind, and the value the harness sets Terminus 2's _pending_completion to.
    At a claim it replaces the confirmation question; clearing the flag means the lineage
    then ends only on two consecutive claims (Terminus 2's own confirmation). Arm C's
    children get FRESH_NOTE with Terminus 2's initial prompt before it, arm D's FILES_NOTE."""
    return f"Current terminal state:\n{terminal_output}\n\n{NUDGE}", False


def first_prompt(arm: str, terminal_output: str, workdir: str = "/app") -> tuple[str, bool]:
    """after_trigger() with the arm's fixed note: A and B none, C FRESH_NOTE, D FILES_NOTE."""
    prompt, pending = after_trigger(terminal_output)
    note = {"A": "", "B": "", "C": FRESH_NOTE, "D": FILES_NOTE.format(workdir=workdir)}[arm]
    return (f"{note}\n\n{prompt}" if note else prompt), pending
