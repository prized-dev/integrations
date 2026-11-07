"""python3 -I test_atif_adapter.py  (stdlib only)"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atif_adapter as A  # noqa: E402
from detector import trigger  # noqa: E402

P = "root@9ce67eb0-75f6-46cb-96c1-4c64dd5f25bb:/app# "
CONFIRM = ("Current terminal state:\n{t}\n\nAre you sure you want to mark the task as complete? "
           "This will trigger your solution to be graded and you won't be able to make any further "
           'corrections. If so, include "task_complete": true in your JSON response again.')

# ---- unwrapping Terminus 2 observations (terminus_2.py 1618-1638, 716-733, 1434-1441) ----
assert A.split_observation("New Terminal Output:\n" + P + "ls\na b\n") == ("new", P + "ls\na b\n")
assert A.split_observation("Current Terminal Screen:\n" + P + "\n") == ("screen", P + "\n")
assert A.split_observation(CONFIRM.format(t="New Terminal Output:\n" + P + "\n")) == ("new", P + "\n")
w = ("Previous response had warnings:\nWARNINGS: - Extra text detected before JSON object\n"
     "- Extra text detected after JSON object\n\nNew Terminal Output:\n" + P + "which R\n")
assert A.split_observation(w) == ("new", P + "which R\n")
t = ("Previous command:\nmake\n\nThe previous command timed out after 60.0 seconds\n\nIt is possible "
     "that the command is not yet finished executing.\n\nHere is the current state of the terminal:\n\n"
     "New Terminal Output:\nmake: *** [all] Error 2\n")
assert A.split_observation(t) == ("new", "make: *** [all] Error 2\n")
pe = ("Previous response had parsing errors:\nERROR: No valid JSON found in response\n\n"
      "Please fix these issues and provide a proper JSON response.")
assert A.split_observation(pe) is None  # not an episode; its "ERROR:" is not terminal output
# a summarization handoff in the user slot is not terminal output
r = A.episode_from_turn([], "Here are the answers the other agent provided.\n\nThe build failed with error: x", False)
assert r.mode == "raw" and r.episode.output == "" and not r.episode.err


# ---- a small ATIF trajectory shaped like Harbor 0.25.0's (terminus_2.py 1717-1740) ----
def step(i, ts, keys, obs, done=False, cost=0.01, p=1000, c=100, k=900):
    tcs = [{"tool_call_id": f"call_{i}_{j + 1}", "function_name": "bash_command",
            "arguments": {"keystrokes": x, "duration": 1.0}} for j, x in enumerate(keys)]
    if done:
        tcs.append({"tool_call_id": f"call_{i}_task_complete", "function_name": "mark_task_complete",
                    "arguments": {}})
    return {"step_id": i + 2, "timestamp": f"2026-04-30T08:00:{ts:02d}+00:00", "source": "agent",
            "message": "Analysis: x\nPlan: y", "tool_calls": tcs or None,
            "observation": {"results": [{"content": obs}]},
            "metrics": {"prompt_tokens": p, "completion_tokens": c, "cached_tokens": k, "cost_usd": cost}}


traj = {"schema_version": "ATIF-v1.7", "agent": {"name": "terminus-2"}, "steps": [
    {"step_id": 1, "timestamp": "2026-04-30T08:00:00+00:00", "source": "user", "message": "task..."},
    step(0, 5, ["make\n"], "New Terminal Output:\n" + P + "make\nmake: *** [all] Error 2\n" + P + "\n"),
    # parse-error turn: recorded by Terminus 2 with no tool calls (1569-1611); costs money, is no episode
    {"step_id": 3, "timestamp": "2026-04-30T08:00:07+00:00", "source": "agent", "message": "{bad",
     "observation": {"results": [{"content": pe}]},
     "metrics": {"prompt_tokens": 1100, "completion_tokens": 10, "cost_usd": 0.002}},
    step(2, 9, ["make CC=gcc\n"], w.replace("which R\n", "make CC=gcc\nmake: *** [all] Error 1\n")),
    step(3, 12, ["ls\n"], "New Terminal Output:\n" + P + "ls\na.out\n"),
    step(4, 14, ["./a.out\n"], CONFIRM.format(t="New Terminal Output:\n" + P + "./a.out\nok\n"), done=True),
    step(5, 15, [], "New Terminal Output:\n" + P + "\n", done=True),
]}
recs = A.from_atif(traj)
assert [r.index for r in recs] == [0, 2, 3, 4, 5]          # loop counter from call ids; parse error skipped
assert [r.episode.err for r in recs] == [True, True, False, False, False]
assert [r.episode.claimed_complete for r in recs] == [False, False, False, True, True]
assert [r.confirmed for r in recs] == [False, False, False, False, True]
assert recs[0].elapsed_s == 5 and recs[-1].elapsed_s == 15
assert abs(recs[-1].cost_usd - (5 * 0.01 + 0.002)) < 1e-9   # the parse-error call's cost counts
assert recs[-1].tokens == 5 * 1100 + 1110
assert abs(recs[0].haiku_usd - (100 * 1.25e-6 + 900 * 0.10e-6 + 100 * 5e-6)) < 1e-12
eps = [r.episode for r in recs]
assert trigger(eps[:3], 12, 900, 0.03, 1.5) is None          # 2 errors, but only 3 episodes
assert trigger(eps[:4], 14, 900, 0.04, 1.5) == "claim"       # claim fires at the FIRST task_complete

# ---- the same episodes from Harbor's per-episode logs / chat messages share episode_from_turn ----
def resp(keys, done=False):
    import json
    return "```json\n" + json.dumps({"analysis": "a", "plan": "p", "commands": [
        {"keystrokes": k, "duration": 1.0} for k in keys], "task_complete": done}) + "\n```"


msgs = [{"role": "user", "content": "task...\n\nCurrent terminal state:\nCurrent Terminal Screen:\n" + P},
        {"role": "assistant", "content": resp(["make\n"])},
        {"role": "user", "content": traj["steps"][1]["observation"]["results"][0]["content"]},
        {"role": "assistant", "content": "{bad"},
        {"role": "user", "content": pe},
        {"role": "assistant", "content": resp(["make CC=gcc\n"])},
        {"role": "user", "content": traj["steps"][3]["observation"]["results"][0]["content"]},
        {"role": "assistant", "content": resp(["ls\n"])},
        {"role": "user", "content": traj["steps"][4]["observation"]["results"][0]["content"]},
        {"role": "assistant", "content": resp(["./a.out\n"], True)},
        {"role": "user", "content": traj["steps"][5]["observation"]["results"][0]["content"]},
        {"role": "assistant", "content": resp([], True)}]   # last response: no observation yet
m = A.from_messages(msgs)
assert [(r.index, r.episode.err, r.episode.claimed_complete, r.confirmed) for r in m] == \
       [(r.index, r.episode.err, r.episode.claimed_complete, r.confirmed) for r in recs]
assert A.parse_response('{"commands": [{"keystrokes": "ls\\n"}], "task_complete": "true"}') == (["ls\n"], True)
assert A.parse_response('{"analysis": "x", "commands": [{"keystrokes": "cat a\\n", "duration": 1}') == (["cat a\n"], False)

# ---- regression: a pane capture repeats old errors while the agent waits on a long job ----
# (sam-cell-seg, fable-5-high, TB2.1 Hub trial bef10914): the ModuleNotFoundError from an
# earlier run stayed on the visible screen through four "wait" episodes, so the old adapter
# saw an error in each and "hard" fired at episode 13 while the job was running fine.
tb = ["Traceback (most recent call last):",
      '  File "/app/convert_masks.py", line 5, in <module>',
      "    from mobile_sam import sam_model_registry, SamPredictor",
      "ModuleNotFoundError: No module named 'timm'"]
pip = [P + "pip install timm 2>&1 | tail -3", "", "[notice] A new release of pip is available: 24.0 -> 26.1.2",
       "[notice] To update, run: pip install --upgrade pip", P.rstrip()]
run = [P + "python3 convert_masks.py --weights_path /app/mobile_sam.pt --output_path /app/out.csv",
       "/usr/local/lib/python3.11/site-packages/mobile_sam/modeling/tiny_vit_sam.py:656: UserWarning: "
       "Overwriting tiny_vit_21m_512 in registry with mobile_sam.modeling.",
       "  return register_model(fn_wrapper)"]
bar = "SAM predict:  {}%|#####          | {}/48 [01:20<03:02,  5.52s/it]"
seq = [(["python3 convert_masks.py\n"], "New Terminal Output:\n" + P + "python3 convert_masks.py\n" + "\n".join(tb) + "\n" + P),
       (["pip install timm 2>&1 | tail -3\n"], "New Terminal Output:\n" + "\n".join(pip) + "\n"),
       (["python3 convert_masks.py --weights_path /app/mobile_sam.pt --output_path /app/out.csv\n"],
        "New Terminal Output:\n" + "\n".join(run) + "\n" + bar.format(4, 2) + "\n"),
       ([""], "Current Terminal Screen:\n" + "\n".join(tb[1:] + pip + run) + "\n" + bar.format(31, 15) + "\n\n\n"),
       ([""], "Current Terminal Screen:\n" + "\n".join(tb[2:] + pip + run) + "\n" + bar.format(60, 29) + "\n\n"),
       ([""], "Current Terminal Screen:\n" + "\n".join(tb[3:] + pip + run) + "\n" + bar.format(90, 43) + "\n")]
prev, rs = None, []
for keys, obs in seq:
    prev = A.episode_from_turn(keys, obs, False, prev)
    rs.append(prev)
assert [r.episode.err for r in rs] == [True, False, False, False, False, False]
assert [r.mode for r in rs] == ["new", "new", "new", "screen", "screen", "screen"]
assert rs[4].episode.output == bar.format(60, 29)
assert trigger([r.episode for r in rs], 100, 900, 0.1, 1.5) is None
# an unchanged screen is a repaint with no output
again = A.episode_from_turn([""], seq[-1][1], False, rs[-1])
assert again.mode == "repaint" and again.episode.output == ""
# ... but new errors that scroll onto the screen still count
fresh = A.episode_from_turn([""], "Current Terminal Screen:\n" + "\n".join(pip + run) + "\n" + bar.format(98, 47)
                            + "\nRuntimeError: CUDA out of memory\n", False, rs[-1])
assert fresh.episode.err and fresh.episode.output.endswith("RuntimeError: CUDA out of memory")

# ---- regression: a bare prompt that was typed on later still lines up (build-pov-ray, fable-5-high) ----
Q = "root@58169484-c487-41d7-928b-670c04e90176:/app/povray-2.2/source#"
e9 = A.episode_from_turn(["make -f unix.mak CC=gcc\n"], "New Terminal Output:\n" + Q + " make -f unix.mak CC=gcc\n"
                         "bash: make: command not found\n" + Q + "\n", False)
e10 = A.episode_from_turn(["gcc --version\n", "apt-get update && apt-get install -y make gcc\n"],
                          "New Terminal Output:\n" + Q + " gcc --version\nbash: gcc: command not found\n" + Q +
                          " apt-get update && apt-get install -y make gcc\n0% [Waiting for headers]\n", False, e9)
e11 = A.episode_from_turn([""], "Current Terminal Screen:\n" + Q + " make -f unix.mak CC=gcc\nbash: make: command not found\n"
                          + Q + " gcc --version\nbash: gcc: command not found\n" + Q +
                          " apt-get update && apt-get install -y make gcc\nHit:1 http://security.ubuntu.com/ubuntu noble-security InRelease\n"
                          "0% [Waiting for headers]\n", False, e10)
assert e9.episode.err and e10.episode.err and not e11.episode.err
assert e11.episode.output.startswith("Hit:1")
print("atif_adapter: all checks pass")
