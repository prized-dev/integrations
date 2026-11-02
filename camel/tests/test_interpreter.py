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

"""PrizedInterpreter against the fake sandbox: commands really run, with bash, in a temp directory."""

from __future__ import annotations

import gc
import shutil

import pytest
from camel.interpreters import BaseInterpreter, InterpreterError
from prized import ExecResult, PrizedError

from camel_prized import PrizedInterpreter
from camel_prized._sandbox import GONE_NOTE, MAX_INLINE_COMMAND

from .fakes import timeout_error


def test_is_a_camel_interpreter():
    assert issubclass(PrizedInterpreter, BaseInterpreter)


def test_sandbox_is_made_on_first_run_with_safe_defaults(cloud):
    interp = PrizedInterpreter(labels={"team": "x"}, env_vars={"FOO": "1"}, vcpu=2)
    assert cloud.create_calls == [] and interp.sandbox_id is None
    assert interp.run("print(6 * 7)") == "42\n"
    (call,) = cloud.create_calls
    assert call["template"] == "prized-base-v1"
    assert call["labels"] == {"created-by": "camel", "team": "x"}
    assert call["env_vars"] == {"PIP_BREAK_SYSTEM_PACKAGES": "1", "FOO": "1"}
    assert (call["auto_pause_min"], call["auto_delete_min"], call["vcpu"]) == (10, 60, 2)
    assert call["api_key"] == "dcp_offline_unit_test_key"
    assert interp.sandbox_id == cloud.created[0].id
    interp.run("print(1)")
    assert len(cloud.create_calls) == 1  # one sandbox, reused


def test_python_runs_inline_in_one_command(cloud):
    interp = PrizedInterpreter()
    assert interp.run("import sys\nprint('it' + \"'s\", sys.argv[0])", "python3") == "it's -c\n"
    (cmd,) = cloud.created[0].commands
    assert cmd.startswith('PATH="$HOME/.local/bin:$PATH" python3 -c ')
    assert cloud.created[0].writes == []
    assert cloud.created[0].timeouts == [120.0]


def test_failure_shows_stderr_and_return_code(cloud):
    out = PrizedInterpreter().run("import sys\nprint('partial')\nsys.exit('boom')")
    assert out == "partial\n(stderr: boom\n)(Execution failed with return code 1)"


def test_traceback_reaches_the_model(cloud):
    out = PrizedInterpreter().run("1/0")
    assert "ZeroDivisionError" in out and "(Execution failed with return code 1)" in out


def test_no_output(cloud):
    assert PrizedInterpreter().run("x = 1") == "Code executed successfully (no output)."


@pytest.mark.parametrize("code_type", ["bash", "sh", "shell", "SHELL"])
def test_bash(cloud, code_type):
    assert PrizedInterpreter().run("echo $((2 + 3)) && pwd", code_type).splitlines()[0] == "5"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed here")
@pytest.mark.parametrize("code_type", ["javascript", "js", "node"])
def test_javascript(cloud, code_type):
    assert PrizedInterpreter().run("console.log([1, 2, 3].map(x => x * 2).join(','))", code_type) == "2,4,6\n"


def test_unsupported_code_type(cloud):
    interp = PrizedInterpreter()
    with pytest.raises(InterpreterError, match="Unsupported code type ruby"):
        interp.run("puts 1", "ruby")
    assert cloud.create_calls == []
    assert set(interp.supported_code_types()) >= {"python", "bash", "javascript"}


def test_large_code_goes_up_as_a_file_and_is_removed(cloud):
    filler = "# " + "x" * 200 + "\n"
    code = filler * (MAX_INLINE_COMMAND // len(filler) + 10) + "print('big ok')"
    assert PrizedInterpreter().run(code) == "big ok\n"
    sandbox = cloud.created[0]
    (path,) = sandbox.writes
    assert path.startswith("/home/prized/.camel/run-") and path.endswith(".py")
    assert len(sandbox.commands[0]) < 1000  # the code itself did not go on the command line
    assert not sandbox.local(path).exists()


def test_large_code_keeps_the_exit_code(cloud):
    code = "#" + "y" * MAX_INLINE_COMMAND + "\nimport sys; sys.exit(4)"
    assert PrizedInterpreter().run(code).endswith("(Execution failed with return code 4)")


def test_timeout_returns_what_it_printed(cloud):
    interp = PrizedInterpreter(timeout=5)
    interp.start()

    def hook(cmd):
        raise timeout_error("step 1\n", 5)

    cloud.created[0].exec_hook = hook
    assert interp.run("while True: pass") == "step 1\n(Execution timed out after 5 seconds and was stopped.)"


def test_truncated_output_says_so(cloud):
    interp = PrizedInterpreter().start()
    cloud.created[0].exec_hook = lambda cmd: ExecResult("a" * 10, "", 0, truncated=True)
    assert "cut off at 4 MiB" in interp.run("print('a' * 10**8)")


def test_execute_command(cloud):
    interp = PrizedInterpreter()
    assert interp.execute_command("echo $PIP_BREAK_SYSTEM_PACKAGES; echo err >&2") == "1\n(stderr: err\n)"
    assert cloud.created[0].commands == ["echo $PIP_BREAK_SYSTEM_PACKAGES; echo err >&2"]


def test_files_persist_between_runs(cloud):
    interp = PrizedInterpreter()
    interp.execute_command("echo hello > note.txt")
    assert interp.run("print(open('note.txt').read().strip())") == "hello\n"


def test_gone_sandbox_is_replaced_once(cloud):
    interp = PrizedInterpreter().start()
    first = cloud.created[0]
    first.state = "deleted"  # its run time ended
    assert interp.run("print('again')") == GONE_NOTE + "again\n"
    assert len(cloud.created) == 2 and first.closed
    assert interp.sandbox_id == cloud.created[1].id


def test_not_running_but_still_there_is_an_error_not_a_new_sandbox(cloud):
    interp = PrizedInterpreter().start()

    def hook(cmd):
        raise PrizedError("sandbox_not_running", status=409)

    cloud.created[0].exec_hook = hook
    with pytest.raises(InterpreterError, match="sandbox_not_running"):
        interp.run("print(1)")
    assert len(cloud.created) == 1


def test_other_errors_become_interpreter_errors(cloud):
    interp = PrizedInterpreter().start()

    def hook(cmd):
        raise PrizedError("rate_limited", status=429)

    cloud.created[0].exec_hook = hook
    with pytest.raises(InterpreterError, match="rate_limited"):
        interp.execute_command("true")


def test_create_failure_is_an_interpreter_error(cloud):
    cloud.fail_create = PrizedError("quota_exceeded", status=402)
    with pytest.raises(InterpreterError, match="quota_exceeded"):
        PrizedInterpreter().run("print(1)")


def test_attach_to_an_existing_sandbox(cloud):
    existing = cloud.add_existing()
    interp = PrizedInterpreter(sandbox_id=existing.id)
    assert interp.sandbox_id == existing.id
    assert interp.run("print('hi')") == "hi\n"
    assert cloud.create_calls == [] and cloud.connect_calls[0]["sandbox_id"] == existing.id
    interp.close()
    assert existing.deletes == 0 and existing.closed


def test_attached_sandbox_that_is_gone_is_not_replaced(cloud):
    existing = cloud.add_existing()
    interp = PrizedInterpreter(sandbox_id=existing.id).start()
    existing.state = "deleted"
    with pytest.raises(InterpreterError, match="not_found"):
        interp.run("print(1)")
    assert cloud.create_calls == []


def test_close_deletes_once_and_ends_use(cloud):
    interp = PrizedInterpreter()
    interp.run("print(1)")
    interp.close()
    interp.close()
    assert cloud.created[0].deletes == 1 and cloud.created[0].closed
    with pytest.raises(RuntimeError, match="closed"):
        interp.run("print(1)")


def test_close_before_any_run_creates_nothing(cloud):
    PrizedInterpreter().close()
    assert cloud.create_calls == []


def test_with_block_deletes(cloud):
    with PrizedInterpreter() as interp:
        interp.run("print(1)")
    assert cloud.created[0].deletes == 1


def test_garbage_collection_deletes(cloud):
    interp = PrizedInterpreter()
    interp.run("print(1)")
    del interp
    gc.collect()
    assert cloud.created[0].deletes == 1


def test_needs_an_api_key(cloud, monkeypatch):
    monkeypatch.delenv("PRIZED_API_KEY")
    with pytest.raises(ValueError, match="api-keys"):
        PrizedInterpreter()
    PrizedInterpreter(api_key="dcp_given")  # an explicit key is enough


def test_bad_timeout():
    with pytest.raises(ValueError):
        PrizedInterpreter(timeout=0)


def test_confirmation(cloud, monkeypatch):
    answers = iter(["maybe", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    interp = PrizedInterpreter(require_confirm=True)
    with pytest.raises(InterpreterError, match="opted not to run"):
        interp.run("print(1)")
    assert cloud.create_calls == []
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert interp.run("print(1)") == "1\n"


def test_action_space_is_not_supported(cloud):
    with pytest.raises(RuntimeError):
        PrizedInterpreter().update_action_space({"x": 1})
