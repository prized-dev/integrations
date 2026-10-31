# Copyright 2026 Prized
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""run_command and the code_exec tool."""

import os
import shlex

import pytest
from prized import PrizedError
from stirrup.tools.code_backends.base import CodeExecutionParams

from stirrup_prized import PrizedCodeExecToolProvider
from stirrup_prized._provider import _quote_argv_for_shell


@pytest.fixture
async def provider(world):
    p = PrizedCodeExecToolProvider(shell_timeout=30)
    async with p:
        yield p


def last_exec(world):
    return [c for c in world.only().calls if c[0] == "exec"][-1][1]


async def test_runs_in_the_working_directory(provider, world):
    result = await provider.run_command("pwd; echo out; echo err >&2")
    assert (result.exit_code, result.stdout, result.stderr, result.error_kind) == (
        0,
        "/home/prized/work\nout\n",
        "err\n",
        None,
    )
    call = last_exec(world)
    assert call["timeout"] == 30  # shell_timeout when the call names none
    assert call["cwd"] == "/"


@pytest.mark.parametrize(("cmd", "code"), [("false", 1), ("exit 3", 3), ("sh -c 'exit 7'", 7), ("true", 0)])
async def test_exit_codes_are_results(provider, cmd, code):
    result = await provider.run_command(cmd)
    assert result.exit_code == code
    assert result.error_kind is None


async def test_timeouts_pass_through(provider, world):
    # Over 240 s the SDK runs the command detached and polls it; 0 is no limit.
    await provider.run_command("true", timeout=900)
    assert last_exec(world)["timeout"] == 900
    await provider.run_command("true", timeout=0)
    assert last_exec(world)["timeout"] == 0


async def test_a_command_past_its_timeout(provider):
    result = await provider.run_command("echo started; echo oops >&2; sleep 5", timeout=1)
    assert result.error_kind == "timeout"
    assert result.exit_code == 1
    assert result.stdout == "started\n"
    assert "ran past its 1s timeout" in result.stderr
    assert "started" in result.stderr and "oops" in result.stderr  # the model sees what it printed
    assert "background" in result.advice


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (PrizedError("sandbox_unreachable", status=502), "sandbox_unreachable"),
        (PrizedError("network_error"), "sandbox_unreachable"),
        (PrizedError("rate_limited", status=429), "sandbox_unreachable"),
        (PrizedError("http_error", status=503), "sandbox_unreachable"),
        (PrizedError("timeout"), "timeout"),
        (PrizedError("bad_env", status=400), "invalid_argument"),
        (PrizedError("spawn_failed", status=400), "invalid_argument"),
        # A detached start that failed (a full disk), a run whose files a command removed,
        # anything unknown: the sandbox still stands, so the model hears about it.
        (PrizedError("command_failed", "Could not start the background command"), "sandbox_error"),
        (PrizedError("not_found", "No background command x in this sandbox."), "sandbox_error"),
        (PrizedError("http_error", status=418), "sandbox_error"),
        (PrizedError("permission_denied", status=403), "sandbox_error"),
    ],
)
async def test_errors_the_model_can_work_around(provider, world, error, kind):
    world.fail("exec", error)
    result = await provider.run_command("echo hi")
    assert result.error_kind == kind
    assert error.code in result.stderr
    assert result.advice


@pytest.mark.parametrize(
    "error",
    [
        PrizedError("not_found", status=404),
        PrizedError("sandbox_deleted"),
        PrizedError("unauthorized", status=401),
        PrizedError("insufficient_balance", status=402),
        PrizedError("sandbox_quota_exceeded", status=429),
        PrizedError("workspace_held", status=403),
    ],
)
async def test_errors_that_end_the_run_are_raised(provider, world, error):
    world.fail("exec", error)
    with pytest.raises(PrizedError) as info:
        await provider.run_command("echo hi")
    assert info.value is error


async def test_nul_byte_is_refused_before_sending(provider, world):
    before = len(world.only().calls)
    result = await provider.run_command("echo a\x00b")
    assert result.error_kind == "invalid_argument"
    assert len(world.only().calls) == before


async def test_allowlist_runs_exactly_the_parsed_arguments(world):
    provider = PrizedCodeExecToolProvider(allowed_commands=[r"^echo", r"^python"])
    async with provider:
        result = await provider.run_command("echo '$(whoami)' ok")
        assert result.stdout == "$(whoami) ok\n"
        assert last_exec(world)["cmd"].endswith("'echo' '$(whoami)' ok")
        for cmd in ("rm -rf /", "echo hi; rm -rf /", "echo $(id)x | sh"):
            calls = len(world.only().calls)
            rejected = await provider.run_command(cmd)
            assert rejected.error_kind == "command_not_allowed", cmd
            assert len(world.only().calls) == calls


@pytest.mark.parametrize(
    "argv", [["echo", "$(whoami)"], ["echo", "line1\nline2"], ["echo", "it's"], ["echo", ""], ["time"], ["A=1"]]
)
def test_quote_argv_round_trips(argv):
    quoted = _quote_argv_for_shell(argv)
    assert shlex.split(quoted) == argv
    assert quoted.startswith("'")


async def test_the_working_directory_comes_back(provider, world):
    result = await provider.run_command("cd /home/prized && rm -rf work && echo gone")
    assert result.stdout == "gone\n"
    assert os.path.isdir(world.only().local("/home/prized/work")), "put back as the command ended"
    result = await provider.run_command("pwd")
    assert result.stdout == "/home/prized/work\n"


async def test_a_long_command_is_staged_as_a_script(provider, world):
    payload = "x" * 200_000
    result = await provider.run_command(f"cat > big.txt <<'EOF'\n{payload}\nEOF\nwc -c < big.txt; exit 4")
    assert result.exit_code == 4
    assert result.stdout.strip() == str(len(payload) + 1)
    sent = last_exec(world)["cmd"]
    assert len(sent) < 1_000 and "bash -- /home/prized/.stirrup-prized/cmd-" in sent
    assert os.listdir(world.only().local("/home/prized/.stirrup-prized")) == [], "the script is removed"


async def test_a_staged_command_is_removed_if_the_call_fails(provider, world):
    world.fail("exec", PrizedError("sandbox_unreachable", status=502))
    result = await provider.run_command("echo " + "y" * 100_000)
    assert result.error_kind == "sandbox_unreachable"
    assert os.listdir(world.only().local("/home/prized/.stirrup-prized")) == []


async def test_the_code_exec_tool(world):
    provider = PrizedCodeExecToolProvider()
    tool = await provider.__aenter__()
    try:
        result = await tool.executor(CodeExecutionParams(cmd="echo hello"))
        assert "<exit_code>0</exit_code>" in result.content
        assert "<stdout>hello\n</stdout>" in result.content
        assert last_exec(world)["timeout"] == 300  # Stirrup's SHELL_TIMEOUT
    finally:
        await provider.__aexit__(None, None, None)


async def test_the_wrapper_keeps_exit_codes_and_line_numbers(provider, world):
    result = await provider.run_command("echo $LINENO\necho $LINENO")
    assert result.stdout == "1\n2\n"
    result = await provider.run_command("echo ok\nif then")
    assert "line 2" in result.stderr
    # The working directory can't come back (its parent is read-only): still the command's own code.
    home = world.only().local("/home/prized")
    try:
        result = await provider.run_command("set -e; cd /home/prized; rm -rf work; chmod 555 /home/prized; exit 7")
        assert result.exit_code == 7
    finally:
        os.chmod(home, 0o755)
        os.makedirs(home + "/work", exist_ok=True)


async def test_quotes_count_toward_the_staging_limit(provider, world):
    cmd = "echo " + "''" * 12_000 + "x"  # 24 KB as typed, 120 KB once quoted for the SDK's detached start
    assert len(cmd) < 32 * 1024 < 96 * 1024 < len(shlex.quote(cmd))
    result = await provider.run_command(cmd)
    assert result.stdout == "x\n"
    assert "bash -- /home/prized/.stirrup-prized/cmd-" in last_exec(world)["cmd"]
