"""The stand-ins are only as good as their likeness. These tests hold them to the real thing:

- tests/fake_verifiers against the installed verifiers, read as source (parsed, never imported or
  run), so a release that changes the backend contract fails here;
- tests/fake_prized.py against the installed `prized` SDK's signatures, so every call that works
  on the fake works on the SDK.

The real verifiers is a dependency of this package, so it is installed wherever the tests run;
mypy (`mypy src`) checks PrizedBackend against its types as well.
"""

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path
from typing import Any

import fake_prized
import prized
import pytest
from verifiers.v1 import runtimes as fake_runtimes

from verifiers_prized import PrizedBackend, PrizedProcess

HERE = Path(__file__).parent


def real_verifiers() -> Path:
    for entry in sys.path:
        root = Path(entry or ".").resolve()
        if root.is_relative_to(HERE.resolve()):
            continue
        candidate = root / "verifiers" / "v1" / "runtimes" / "base.py"
        if candidate.is_file():
            return root / "verifiers" / "v1"
    pytest.skip("verifiers is not installed")


def classes(path: Path) -> dict[str, ast.ClassDef]:
    tree = ast.parse(path.read_text())
    return {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}


def methods(node: ast.ClassDef) -> dict[str, tuple[bool, list[str], bool]]:
    """name -> (async, argument names, abstract)."""
    found = {}
    for item in node.body:
        if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
            args = [a.arg for a in [*item.args.posonlyargs, *item.args.args, *item.args.kwonlyargs]]
            abstract = any(
                (isinstance(d, ast.Name) and d.id == "abstractmethod")
                or (isinstance(d, ast.Attribute) and d.attr == "abstractmethod")
                for d in item.decorator_list
            )
            found[item.name] = (isinstance(item, ast.AsyncFunctionDef), args, abstract)
    return found


def live_methods(cls: type) -> dict[str, tuple[bool, list[str]]]:
    found = {}
    for name, value in vars(cls).items():
        if inspect.isfunction(value):
            found[name] = (inspect.iscoroutinefunction(value), list(inspect.signature(value).parameters))
    return found


def class_attributes(node: ast.ClassDef) -> set[str]:
    names = set()
    for item in node.body:
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            names.add(item.target.id)
        elif isinstance(item, ast.Assign):
            names.update(t.id for t in item.targets if isinstance(t, ast.Name))
    return names


def test_backend_overrides_match_the_real_contract() -> None:
    real = methods(classes(real_verifiers() / "runtimes" / "base.py")["RuntimeBackend"])
    required = {name for name, (_, _, abstract) in real.items() if abstract}
    assert required == {"start", "exec", "write"}, "verifiers changed what a backend must implement"
    # verifiers constructs a backend as `Backend(config, name)` (runtimes/__init__.py make_backend).
    assert "backend_cls(config)(config, name)" in (real_verifiers() / "runtimes" / "__init__.py").read_text()
    assert live_methods(PrizedBackend)["__init__"] == (False, ["self", "config", "name"])
    for name, (is_async, args) in live_methods(PrizedBackend).items():
        if name.startswith("_"):
            continue
        assert name in real, f"PrizedBackend.{name} overrides nothing in verifiers' RuntimeBackend"
        assert real[name][:2] == (is_async, args), f"PrizedBackend.{name}: {real[name]} != {(is_async, args)}"
    assert required <= set(live_methods(PrizedBackend))
    assert {"teardown", "cleanup", "run", "read", "run_background"} <= set(live_methods(PrizedBackend))


def test_process_implements_the_real_runtime_process() -> None:
    real = methods(classes(real_verifiers() / "runtimes" / "base.py")["RuntimeProcess"])
    abstract = {name: spec[:2] for name, spec in real.items() if spec[2]}
    ours = live_methods(PrizedProcess)
    for name, spec in abstract.items():
        assert ours.get(name) == spec, f"PrizedProcess.{name}: {ours.get(name)} != {spec}"


def test_flags_and_attributes_exist_in_the_real_base() -> None:
    real = class_attributes(classes(real_verifiers() / "runtimes" / "base.py")["RuntimeBackend"])
    for name in [
        "is_local",
        "SUPPORTS_TASK_IMAGES",
        "SUPPORTS_USER_SWITCHING",
        "SUPPORTS_PORT_EXPOSURE",
        "SUPPORTS_COMPOSE",
        "scripts_dir",
    ]:
        assert name in real, name


def test_fake_base_matches_the_real_base() -> None:
    base = real_verifiers() / "runtimes" / "base.py"
    for cls_name, fake_cls in [
        ("RuntimeBackend", fake_runtimes.RuntimeBackend),
        ("RuntimeProcess", fake_runtimes.RuntimeProcess),
    ]:
        real = methods(classes(base)[cls_name])
        for name, spec in live_methods(fake_cls).items():
            if name.startswith("_") and name != "__init__":
                continue
            assert real[name][:2] == spec, f"fake {cls_name}.{name}: {spec} != real {real[name][:2]}"


def test_fake_runtime_view_matches_the_real_one() -> None:
    real = methods(classes(real_verifiers() / "runtimes" / "runtime.py")["Runtime"])
    for name, spec in live_methods(fake_runtimes.Runtime).items():
        if name.startswith("_"):
            continue
        assert real[name][:2] == spec, f"fake Runtime.{name}: {spec} != real {real[name][:2]}"


def test_imported_names_exist_where_we_import_them() -> None:
    v1 = real_verifiers()
    exported = ast.literal_eval(
        next(
            node.value
            for node in ast.parse((v1 / "runtimes" / "__init__.py").read_text()).body
            if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "__all__" for t in node.targets)
        )
    )
    for name in ["CustomRuntimeConfig", "CustomRuntimeInfo", "ProgramResult", "Runtime", "RuntimeBackend",
                 "RuntimeProcess"]:  # fmt: skip
        assert name in exported, name
    assert "SandboxError" in classes(v1 / "errors.py")
    custom = classes(v1 / "configs" / "runtime.py")["CustomRuntimeConfig"]
    assert {"type", "import_path"} <= class_attributes(custom)
    program = classes(v1 / "runtimes" / "base.py")["ProgramResult"]
    assert [n.target.id for n in program.body if isinstance(n, ast.AnnAssign)] == [
        "exit_code",
        "stdout",
        "stderr",
    ]  # type: ignore[union-attr]


# --- the fake SDK against the real one -----------------------------------------------------------


def _check_signature(fake: Any, real: Any, where: str) -> None:
    assert inspect.iscoroutinefunction(fake) == inspect.iscoroutinefunction(real), where
    real_params = inspect.signature(real).parameters
    for name, param in inspect.signature(fake).parameters.items():
        assert name in real_params, f"{where}: the SDK has no parameter {name!r}"
        assert param.kind == real_params[name].kind, (
            f"{where}({name}): {param.kind} != {real_params[name].kind}"
        )
        if (
            param.default is not inspect.Parameter.empty
            and real_params[name].default is not inspect.Parameter.empty
        ):
            assert param.default == real_params[name].default, f"{where}({name}) default"


@pytest.mark.parametrize(
    ("fake", "real"),
    [
        (fake_prized.AsyncSandbox, prized.AsyncSandbox),
        (fake_prized.AsyncFiles, prized.AsyncFiles),
        (fake_prized.FakeCommandHandle, prized.AsyncCommandHandle),
        (fake_prized.Sandbox, prized.Sandbox),
    ],
)
def test_fake_sdk_calls_exist_in_the_sdk(fake: type, real: type) -> None:
    for name, value in vars(fake).items():
        if name.startswith("_"):
            continue
        func = value.__func__ if isinstance(value, classmethod) else value
        if not inspect.isfunction(func):
            continue
        real_value = inspect.getattr_static(real, name)
        real_func = real_value.__func__ if isinstance(real_value, classmethod) else real_value
        assert isinstance(value, classmethod) == isinstance(real_value, classmethod), (
            f"{fake.__name__}.{name}"
        )
        _check_signature(func, real_func, f"{fake.__name__}.{name}")


def test_sdk_errors_the_pool_reads() -> None:
    err = prized.PrizedError("fork_capacity", status=409, body={"fits": 2}, retry_after=1.0, reason="x")
    assert (err.code, err.status, err.body, err.retry_after, err.reason) == (
        "fork_capacity",
        409,
        {"fits": 2},
        1.0,
        "x",
    )
    assert issubclass(prized.CommandTimeoutError, prized.PrizedError)


def test_logs_go_through_verifiers_own_logger() -> None:
    # vf-run and vf-validate route only verifiers' library logger (utils/logging.py) to the
    # console and the run's log file: the parent's id and the run's label are logged under it.
    from verifiers_prized import pool

    tree = ast.parse((real_verifiers() / "utils" / "logging.py").read_text())
    library = next(
        node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "LIBRARY_LOGGER" for t in node.targets)
        and isinstance(node.value, ast.Constant)
    )
    assert pool.logger.name.startswith(f"{library}."), (pool.logger.name, library)
