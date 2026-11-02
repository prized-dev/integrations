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

"""PrizedRuntime against the fake sandbox: the runner script really runs, with python3.

No ``from __future__ import annotations`` here: CAMEL builds each tool's schema from its real
annotations, as it would in a user's module."""

import datetime
import functools
from typing import Dict, List, Optional  # noqa: UP035 (the point: typing names in annotations)

import pytest
from camel.runtimes import BaseRuntime
from camel.toolkits import FunctionTool
from prized import ExecResult, PrizedError
from pydantic import BaseModel

from camel_prized import PrizedRuntime

from .fakes import timeout_error


def add(x: int, y: int) -> int:
    r"""Adds two numbers.

    Args:
        x (int): The first number.
        y (int): The second number.

    Returns:
        int: The sum.
    """
    return x + y


def chatty(n: int) -> dict:
    r"""Prints, then returns a dict.

    Args:
        n (int): How many lines to print.

    Returns:
        dict: A summary.
    """
    for i in range(n):
        print("line", i)
    return {"lines": n, "where": __name__}


def echo(text: str) -> str:
    r"""Echoes text.

    Args:
        text (str): The text.

    Returns:
        str: The same text.
    """
    return text


def boom(reason: str) -> int:
    r"""Raises.

    Args:
        reason (str): Why.

    Returns:
        int: Never.
    """
    raise ValueError(reason)


def uses_math(x: float) -> float:
    r"""Imports inside its body, as a sandboxed function must.

    Args:
        x (float): A number.

    Returns:
        float: Its square root.
    """
    import math

    return math.sqrt(x)


def logged(func):
    @functools.wraps(func)
    def inner(*args, **kwargs):
        return func(*args, **kwargs)

    return inner


@logged
def decorated(a: int) -> int:
    r"""A decorated function: the decorator is dropped in the sandbox.

    Args:
        a (int): A number.

    Returns:
        int: Twice the number.
    """
    return a * 2


def odd_result() -> object:
    r"""Returns something JSON can't hold as is.

    Returns:
        object: A set.
    """
    return {1, 2}


def with_option(x: int, scale: int = 1) -> int:
    r"""Scales x.

    Args:
        x (int): A number.
        scale (int): The factor.

    Returns:
        int: x times scale.
    """
    return x * scale


class Point(BaseModel):
    x: int
    y: int


def norm1(p: dict) -> int:
    r"""Manhattan norm.

    Args:
        p (dict): A point with x and y.

    Returns:
        int: |x| + |y|.
    """
    return abs(p["x"]) + abs(p["y"])


def total(xs: List[int], label: Optional[str] = None) -> Dict[str, int]:  # noqa: UP006, UP045
    r"""Sums numbers, with typing annotations the sandbox doesn't know.

    Args:
        xs (List[int]): The numbers.
        label (Optional[str]): The key.

    Returns:
        Dict[str, int]: {label: sum}.
    """
    return {label or "sum": sum(xs)}


def norm(p: Point, others: list[Point] | None = None, when: datetime.date | None = None) -> int:
    r"""Manhattan norms of points given as pydantic models.

    Args:
        p (Point): A point.
        others (Optional[List[Point]]): More points.
        when (Optional[datetime.date]): A date, ignored.

    Returns:
        int: The sum of |x| + |y|.
    """
    points = [p, *(others or [])]
    return sum(abs(q["x"]) + abs(q["y"]) for q in points) + (0 if when is None else len(when))


def test_is_a_camel_runtime():
    assert issubclass(PrizedRuntime, BaseRuntime)


def test_tool_runs_in_the_sandbox(cloud):
    runtime = PrizedRuntime().build()
    tool = FunctionTool(add)
    runtime.add(tool, "add_entry")
    assert tool(2, 3) == 5
    assert tool.func(x=4, y=5) == 9
    assert runtime.get_tools() == [tool]
    assert runtime.entrypoint == {"add": "add_entry"}
    (sandbox,) = cloud.created
    assert len(sandbox.commands) == 2 and all(" python3 -c " in c for c in sandbox.commands)
    # The schema the model sees is still the original function's.
    assert tool.get_openai_tool_schema()["function"]["parameters"]["required"] == ["x", "y"]


def test_sandbox_is_made_on_first_call_without_build(cloud):
    runtime = PrizedRuntime()
    tool = FunctionTool(add)
    runtime.add(tool)
    assert cloud.create_calls == []
    assert tool(1, 1) == 2
    assert len(cloud.create_calls) == 1


def test_prints_do_not_mix_into_the_result(cloud):
    tool = FunctionTool(chatty)
    PrizedRuntime().add(tool)
    assert tool(3) == {"lines": 3, "where": "__camel_prized__"}


@pytest.mark.parametrize(
    "text", ["it's", 'say "hi"\\n', "back\\slash", "line1\nline2", "ünïcødé ✓", "{}", "@@camel-prized-result-@@"]
)
def test_arguments_round_trip_exactly(cloud, text):
    tool = FunctionTool(echo)
    PrizedRuntime().add(tool)
    assert tool(text) == text


def test_function_errors_come_back_as_an_error_dict(cloud):
    tool = FunctionTool(boom)
    PrizedRuntime().add(tool)
    out = tool("nope")
    assert out["error"] == "ValueError: nope"
    assert "Traceback" in out["traceback"]


def test_imports_inside_the_body_work(cloud):
    tool = FunctionTool(uses_math)
    PrizedRuntime().add(tool)
    assert tool(16.0) == 4.0


def test_decorators_are_dropped(cloud):
    tool = FunctionTool(decorated)
    PrizedRuntime().add(tool)
    assert tool(21) == 42


def test_non_json_results_become_strings(cloud):
    tool = FunctionTool(odd_result)
    PrizedRuntime().add(tool)
    assert tool() == "{1, 2}"


def test_fixed_arguments_and_model_arguments(cloud):
    tool = FunctionTool(with_option)
    PrizedRuntime().add(tool, arguments={"scale": 10})
    assert tool(3) == 30
    assert tool(3, scale=2) == 6  # the model's own argument wins


def test_pydantic_arguments_are_dumped(cloud):
    tool = FunctionTool(norm1)
    PrizedRuntime().add(tool)
    assert tool(p=Point(x=-2, y=3)) == 5


def test_typing_annotations_are_dropped_in_the_sandbox(cloud):
    tool = FunctionTool(total)
    PrizedRuntime().add(tool)
    assert tool([1, 2, 3], label="n") == {"n": 6}
    assert "xs" in tool.get_openai_tool_schema()["function"]["parameters"]["properties"]


def test_nested_models_and_dates_go_as_json(cloud):
    tool = FunctionTool(norm)
    PrizedRuntime().add(tool)
    when = datetime.date(2026, 11, 2)
    assert tool(p=Point(x=1, y=-1), others=[Point(x=2, y=2)], when=when) == 6 + len("2026-11-02")


def test_unserializable_arguments_are_an_error(cloud):
    tool = FunctionTool(echo)
    PrizedRuntime().add(tool)
    assert tool(object())["error"].startswith("Argument serialization failed")


def test_a_cut_off_result_says_why(cloud):
    runtime = PrizedRuntime().build()
    tool = FunctionTool(add)
    runtime.add(tool)
    cloud.created[0].exec_hook = lambda cmd: ExecResult("x" * 10, "", 0, truncated=True)
    assert "over 4 MiB" in tool(1, 2)["error"]


def test_several_tools_one_sandbox(cloud):
    tools = [FunctionTool(add), FunctionTool(echo)]
    runtime = PrizedRuntime().add(tools)
    assert [t.get_function_name() for t in runtime.get_tools()] == ["add", "echo"]
    assert tools[0](1, 2) == 3 and tools[1]("x") == "x"
    assert len(cloud.created) == 1


def test_methods_and_lambdas_are_refused(cloud):
    class Kit:
        def triple(self, x: int) -> int:
            r"""Triples.

            Args:
                x (int): A number.

            Returns:
                int: 3x.
            """
            return 3 * x

    runtime = PrizedRuntime()
    with pytest.raises(ValueError, match="plain functions"):
        runtime.add(FunctionTool(Kit().triple))
    square = lambda x: x * x  # noqa: E731
    square.__doc__ = "Squares.\n\nArgs:\n    x (int): A number.\n\nReturns:\n    int: x*x.\n"
    with pytest.raises(ValueError, match="plain"):
        runtime.add(FunctionTool(square))


def test_timeout_comes_back_as_an_error(cloud):
    runtime = PrizedRuntime(timeout=3).build()
    tool = FunctionTool(add)
    runtime.add(tool)

    def hook(cmd):
        raise timeout_error("partial", 3)

    cloud.created[0].exec_hook = hook
    assert tool(1, 2) == {"error": "Timed out after 3 seconds", "stdout": "partial"}


def test_sandbox_errors_come_back_as_an_error(cloud):
    runtime = PrizedRuntime().build()
    tool = FunctionTool(add)
    runtime.add(tool)

    def hook(cmd):
        raise PrizedError("rate_limited", status=429)

    cloud.created[0].exec_hook = hook
    assert tool(1, 2)["error"].startswith("Execution failed:")


def test_build_failure_is_a_runtime_error(cloud):
    cloud.fail_create = PrizedError("quota_exceeded", status=402)
    with pytest.raises(RuntimeError, match="creation failed"):
        PrizedRuntime().build()


def test_cleanup_reset_and_with(cloud):
    runtime = PrizedRuntime().build()
    first = cloud.created[0]
    runtime.reset()
    assert first.deletes == 1 and len(cloud.created) == 2
    assert runtime.sandbox_id == cloud.created[1].id
    with runtime:
        pass
    assert cloud.created[1].deletes == 1
    runtime.stop()  # again: nothing to delete
    assert runtime.build().sandbox_id == cloud.created[2].id  # build reopens
    runtime.cleanup()


def test_reset_raises_when_the_old_sandbox_cannot_be_deleted(cloud):
    runtime = PrizedRuntime().build()
    first = cloud.created[0]
    first.fail_delete = 1
    with pytest.raises(PrizedError):
        runtime.reset()
    assert len(cloud.created) == 1
    runtime.reset()  # retried: deleted now, and a fresh one made
    assert first.deletes == 1 and runtime.sandbox_id == cloud.created[1].id
    runtime.cleanup()


def test_info(cloud):
    runtime = PrizedRuntime()
    with pytest.raises(RuntimeError):
        runtime.info()
    runtime.build()
    assert "2 vCPU, 4 GB RAM, 10 GB disk" in runtime.info()
    assert runtime.docs.startswith("https://prized.dev/docs/sandboxes")


def test_attached_sandbox_is_kept(cloud):
    existing = cloud.add_existing()
    runtime = PrizedRuntime(sandbox_id=existing.id).build()
    tool = FunctionTool(add)
    runtime.add(tool)
    assert tool(1, 2) == 3
    runtime.cleanup()
    assert existing.deletes == 0 and cloud.create_calls == []
