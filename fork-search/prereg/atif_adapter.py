"""Terminus 2 trajectory -> detector Episodes (frozen with PREREG.md and detector.py).

One function, `episode_from_turn`, turns one Terminus 2 episode into a `detector.Episode`.
It is defined on fields Terminus 2 itself has (Harbor 0.25.0,
src/harbor/agents/terminus_2/terminus_2.py, cited as T2:<line>):

  keystrokes     [c.keystrokes for c in commands]   Command, T2:71-74; parsed in
                 _handle_llm_interaction T2:1395-1402; ATIF tool_calls
                 function_name "bash_command", arguments.keystrokes, T2:1655-1667
  observation    what Terminus 2 sends back to the model after running them, T2:1622-1638
                 (ATIF observation.results[].content, T2:1671-1678; the next user
                 message in Harbor's episode-N/prompt.txt logs)
  task_complete  is_task_complete, T2:1406 (ATIF tool call "mark_task_complete", T2:1681-1689)

The observation is unwrapped back to the terminal output (`split_observation`):
  - "Current terminal state:\\n...\\n\\nAre you sure you want to mark the task as complete?..."
    (first, unconfirmed task_complete, T2:1622-1629, text at T2:716-733)
  - "Previous response had warnings:\\nWARNINGS: ...\\n\\n" prefix (T2:1632-1636)
  - the timeout template "Previous command:\\n...Here is the current state of the terminal:\\n\\n"
    (T2:1434-1441, templates/timeout.txt)
  - the tmux header "New Terminal Output:\\n" / "Current Terminal Screen:\\n"
    (TmuxSession.get_incremental_output). A "Current Terminal Screen" capture (Harbor's
    fallback when it cannot diff the scrollback) is the visible pane: lines already shown,
    then any new ones. `new_output` keeps only the new ones, so an old error still on screen
    while the agent waits on a long job is not counted again.
A turn whose response failed to parse (T2:1569-1611: "Previous response had parsing
errors", no commands run, no terminal read) is not an episode; Terminus 2 does not dump
the trajectory or reach the end of the loop body for it.

Claim (Terminus 2 double confirmation, T2:1618-1629 and T2:1745-1752): the first episode with
task_complete=True gets the confirmation question instead of a plain observation; only a
second consecutive True ends the run. `claimed_complete` is the raw per-episode flag, so the
detector's "claim" fires on the FIRST, unconfirmed claim. Live, the hook therefore runs
before Terminus 2's `return` at T2:1748 can happen, and the nudge replaces the
confirmation question (detector.after_trigger, amendment 2026-11-07). `confirmed` marks
the episode that would have ended the run.

Live use (harness Terminus 2 subclass): in an override of `_dump_trajectory` (called at
T2:1743 once per executed episode, before the next LLM call), take the newest
`self._trajectory_steps[-1]`, skip a step_id already seen (run() dumps again in `finally`,
T2:1869), and call `episode_from_step(step.model_dump(), prev)` with the lineage's previous
record; then `detector.trigger([r.episode ...], elapsed, T, spend, B)`. Elapsed and spend come
from the harness clock and meter. The nudge has to ride on the next prompt, e.g. via an
override of `_handle_llm_interaction(chat, prompt, ...)` that appends it. A forked child
inherits the parent's record list (its `seen` history and claim state) with the context.
Offline: `from_atif` (trajectory.json), `from_messages` (Harbor episode-N/prompt.txt +
response.txt pairs, or a traces export's `conversations`), `from_episode_dir`.

Stdlib only. No model calls.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from detector import Episode  # noqa: E402

NEW = "New Terminal Output:\n"
SCREEN = "Current Terminal Screen:\n"
CONFIRM_PREFIX = "Current terminal state:\n"
CONFIRM_SUFFIX = "\n\nAre you sure you want to mark the task as complete?"
WARN_PREFIX = "Previous response had warnings:\n"
PARSE_ERROR_PREFIX = "Previous response had parsing errors:\n"
TIMEOUT_PREFIX = "Previous command:\n"
TIMEOUT_STATE = "\n\nHere is the current state of the terminal:\n\n"


@dataclass
class EpisodeRecord:
    episode: Episode
    index: int                    # Terminus 2 loop counter of this episode (0-based)
    mode: str                     # 'new' | 'screen' | 'repaint' | 'raw'
    terminal: str                 # unwrapped terminal text before de-duplication
    confirmed: bool = False       # second consecutive task_complete (the run would end)
    elapsed_s: float | None = None    # seconds from the agent's first step to this episode's end
    tokens: int | None = None         # cumulative prompt (cache reads included) + completion tokens
    cost_usd: float | None = None     # cumulative cost as recorded by Harbor (the model's own prices)
    haiku_usd: float | None = None    # cumulative cost re-priced at Claude Haiku 4.5 list prices
    ref: int | None = None            # position of the source ATIF step / assistant message
    seen: tuple[str, ...] = ()        # last HISTORY non-blank terminal lines shown up to this episode


HISTORY = 400  # terminal lines remembered for de-duplicating pane captures


def split_observation(obs: str) -> tuple[str, str] | None:
    """Observation sent to the model -> (mode, terminal text); None for a parse-error turn."""
    s = obs or ""
    if s.startswith(PARSE_ERROR_PREFIX):
        return None
    if s.startswith(CONFIRM_PREFIX):
        s = s[len(CONFIRM_PREFIX):]
        k = s.rfind(CONFIRM_SUFFIX)
        if k >= 0:
            s = s[:k]
    elif s.startswith(WARN_PREFIX):
        cut = [k for k in (s.find("\n\n" + h) for h in (NEW, SCREEN, TIMEOUT_PREFIX)) if k >= 0]
        s = s[min(cut) + 2:] if cut else ""
    if s.startswith(TIMEOUT_PREFIX):
        k = s.find(TIMEOUT_STATE)
        s = s[k + len(TIMEOUT_STATE):] if k >= 0 else ""
    if s.startswith(NEW):
        return "new", s[len(NEW):]
    if s.startswith(SCREEN):
        return "screen", s[len(SCREEN):]
    return "raw", s


def _lines(text: str) -> list[str]:
    return [ln.rstrip() for ln in text.splitlines() if ln.strip()]


def _occurs(block: list[str], hist: tuple[str, ...]) -> bool:
    n = len(block)
    return any(list(hist[i:i + n]) == block for i in range(len(hist) - n + 1))


def new_output(mode: str, text: str, seen: tuple[str, ...]) -> tuple[str, str]:
    """Drop the part of a pane capture that was already shown.

    A "Current Terminal Screen" capture is the bottom of the terminal: lines already shown in
    earlier episodes, then whatever is new. The longest leading block of the capture that
    occurs, contiguously, in the earlier terminal text is old; the rest is the new output.
    Nothing new left is a 'repaint'. "New Terminal Output" is already incremental.
    """
    if mode != "screen" or not seen:
        return mode, text
    cur = _lines(text)
    j = len(cur)
    while j and not _occurs(cur[:j], seen):
        j -= 1
    rest = cur[j:]
    return ("repaint", "") if not rest else ("screen", "\n".join(rest))


def episode_from_turn(keystrokes: list[str], observation: str, task_complete: bool,
                      prev: "EpisodeRecord | None" = None,
                      index: int | None = None) -> EpisodeRecord | None:
    """One Terminus 2 episode -> EpisodeRecord (None for a parse-error turn: not an episode).

    prev is the record of the previous executed episode of the same lineage (None at the
    start); it carries the terminal history and the pending-completion state.
    """
    split = split_observation(observation)
    if split is None:
        return None
    mode, terminal = split
    seen = prev.seen if prev else ()
    mode, out = new_output(mode, terminal, seen)
    if mode == "raw":
        out = ""  # no tmux header: a handoff/summary or harness prompt, not terminal output
    ep = Episode([k for k in keystrokes if isinstance(k, str)], out, bool(task_complete))
    pending = bool(prev and prev.episode.claimed_complete and not prev.confirmed)
    if index is None:
        index = prev.index + 1 if prev else 0
    return EpisodeRecord(ep, index, mode, terminal,
                         confirmed=bool(task_complete and pending),
                         seen=_extend(seen, _lines(terminal)))


def _extend(seen: tuple[str, ...], lines: list[str]) -> tuple[str, ...]:
    """Append an episode's terminal lines; a bare prompt that has since been typed on is
    replaced by its typed form (the next capture starts with that prompt line)."""
    base = list(seen)
    if base and lines and lines[0].startswith(base[-1]):
        base.pop()
    return tuple(base + lines)[-HISTORY:]


# Claude Haiku 4.5 list prices, USD per token: input, 5-minute cache write, cache read, output.
HAIKU45 = {"input": 1.0e-6, "cache_write": 1.25e-6, "cache_read": 0.10e-6, "output": 5.0e-6}


def haiku_price(prompt: int, cached: int, completion: int) -> float:
    """Re-price one call at Haiku 4.5 rates; uncached input is assumed written to the cache
    (Terminus 2 marks the conversation for Anthropic caching)."""
    return (max(prompt - cached, 0) * HAIKU45["cache_write"] + cached * HAIKU45["cache_read"]
            + completion * HAIKU45["output"])


def _ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _episode_index(step: dict, fallback: int) -> int:
    for tc in step.get("tool_calls") or []:
        m = re.match(r"call_(\d+)_", str(tc.get("tool_call_id", "")))
        if m:
            return int(m.group(1))
    return fallback  # a turn with no commands and no claim carries no call id


def turn_from_step(step: dict) -> tuple[list[str], str, bool] | None:
    """ATIF agent step -> (keystrokes, observation, task_complete); None if not an agent turn."""
    if step.get("source") != "agent" or step.get("is_copied_context"):
        return None
    keys, done = [], False
    for tc in step.get("tool_calls") or []:
        fn = tc.get("function_name")
        if fn == "bash_command":
            keys.append(str((tc.get("arguments") or {}).get("keystrokes", "")))
        elif fn == "mark_task_complete":
            done = True
    contents = [r.get("content") for r in (step.get("observation") or {}).get("results") or []
                if isinstance(r.get("content"), str)]
    if not contents:
        return None
    return keys, contents[0], done


def episode_from_step(step: dict, prev: EpisodeRecord | None = None) -> EpisodeRecord | None:
    """ATIF step (or a live Step.model_dump()) -> EpisodeRecord; shares episode_from_turn."""
    turn = turn_from_step(step)
    if turn is None:
        return None
    keys, obs, done = turn
    return episode_from_turn(keys, obs, done, prev,
                             _episode_index(step, prev.index + 1 if prev else 0))


def from_atif(traj: dict) -> list[EpisodeRecord]:
    """A Harbor ATIF trajectory.json (Terminus 2) -> one EpisodeRecord per executed episode."""
    steps = traj.get("steps") or []
    t0 = _ts(steps[0].get("timestamp")) if steps else None
    recs: list[EpisodeRecord] = []
    tokens = 0
    cost = haiku = 0.0
    have_tok = have_cost = False
    for pos, step in enumerate(steps):
        m = step.get("metrics") or {}
        if step.get("source") == "agent" and not step.get("is_copied_context"):
            p, c, k = (int(m.get("prompt_tokens") or 0), int(m.get("completion_tokens") or 0),
                       int(m.get("cached_tokens") or 0))
            if m.get("prompt_tokens") is not None:
                have_tok = True
            tokens += p + c
            haiku += haiku_price(p, k, c)
            if m.get("cost_usd") is not None:
                have_cost = True
                cost += float(m["cost_usd"])
        rec = episode_from_step(step, recs[-1] if recs else None)
        if rec is None:
            continue
        rec.ref = pos
        t = _ts(step.get("timestamp"))
        rec.elapsed_s = (t - t0) if (t is not None and t0 is not None) else None
        rec.tokens = tokens if have_tok else None
        rec.cost_usd = cost if have_cost else None
        rec.haiku_usd = haiku if have_tok else None
        recs.append(rec)
    return recs


def _first_json_object(text: str) -> dict | None:
    """First balanced {...} in a response, string-aware (as Terminus 2's JSON parser finds it)."""
    start, depth, in_str, esc = -1, 0, False, False
    for i, ch in enumerate(text):
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start:i + 1])
                    return obj if isinstance(obj, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


def parse_response(text: str) -> tuple[list[str], bool] | None:
    """Raw Terminus 2 JSON response -> (keystrokes, task_complete); None if unparseable."""
    obj = _first_json_object(text or "")
    if obj is None:
        # Terminus 2 auto-fixes some malformed JSON; recover the keystroke strings directly
        keys = []
        for raw in re.findall(r'"keystrokes"\s*:\s*"((?:[^"\\]|\\.)*)"', text or ""):
            try:
                keys.append(json.loads('"' + raw + '"'))
            except json.JSONDecodeError:
                keys.append(raw)
        if not keys:
            return None
        return keys, bool(re.search(r'"task_complete"\s*:\s*(true|"true")', text or ""))
    done = obj.get("task_complete", False)
    if isinstance(done, str):
        done = done.lower() in ("true", "1", "yes")
    keys = [c.get("keystrokes") for c in obj.get("commands") or [] if isinstance(c, dict)]
    return [k for k in keys if isinstance(k, str)], bool(done)


def from_messages(messages: list[dict]) -> list[EpisodeRecord]:
    """Chat messages [user prompt, assistant, user observation, assistant, ...] -> records.

    This is Harbor's per-episode log (episode-N/prompt.txt is the user message that
    episode N answered, response.txt its answer) and a traces export's `conversations`.
    The last assistant turn has no observation yet, so it yields no record unless it
    claims completion (then its output is unknown and left empty).
    """
    msgs = [m for m in messages if m.get("role") in ("user", "assistant")]
    recs: list[EpisodeRecord] = []
    turn = -1  # Terminus 2 loop counter: one assistant message per loop iteration
    for i, m in enumerate(msgs):
        if m["role"] != "assistant":
            continue
        turn += 1
        parsed = parse_response(m.get("content") or "")
        nxt = msgs[i + 1]["content"] if i + 1 < len(msgs) and msgs[i + 1]["role"] == "user" else None
        prev = recs[-1] if recs else None
        if nxt is None:
            if parsed and parsed[1]:
                pending = bool(prev and prev.episode.claimed_complete and not prev.confirmed)
                recs.append(EpisodeRecord(Episode(parsed[0], "", True), turn, "raw", "",
                                          confirmed=pending, ref=i, seen=prev.seen if prev else ()))
            break
        if parsed is None:
            continue  # Terminus 2 answers an unparseable response with a parse-error prompt
        rec = episode_from_turn(parsed[0], nxt, parsed[1], prev, turn)
        if rec is None:
            continue
        rec.ref = i
        recs.append(rec)
    return recs


def from_episode_dir(agent_dir: str | Path) -> list[EpisodeRecord]:
    """Harbor's agent/ dir with episode-N/{prompt.txt,response.txt} -> records."""
    d = Path(agent_dir)
    eps = sorted((p for p in d.glob("episode-*") if p.is_dir()), key=lambda p: int(p.name.split("-")[1]))
    msgs: list[dict] = []
    for p in eps:
        pr, rs = p / "prompt.txt", p / "response.txt"
        if pr.exists():
            msgs.append({"role": "user", "content": pr.read_text(errors="replace")})
        if rs.exists():
            msgs.append({"role": "assistant", "content": rs.read_text(errors="replace")})
    return from_messages(msgs)
