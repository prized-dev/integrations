"""The package as Dify's plugin runtime and the Marketplace review read it."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import pytest
import yaml
from conftest import ROOT, StubFile, invoke, outputs, tool
from dify_plugin.config.config import DifyPluginEnv
from dify_plugin.core.plugin_registration import PluginRegistration

TOOLS = [
    "create_sandbox",
    "run_command",
    "run_code",
    "write_file",
    "upload_file",
    "read_file",
    "get_sandbox",
    "pause_sandbox",
    "resume_sandbox",
    "delete_sandbox",
]


@pytest.fixture(scope="module")
def registration():
    import os

    here = os.getcwd()
    os.chdir(ROOT)
    try:
        return PluginRegistration(DifyPluginEnv())
    finally:
        os.chdir(here)


def test_the_sdk_loads_the_manifest_the_provider_and_every_tool(registration):
    manifest = registration.configuration
    assert (manifest.author, manifest.name, manifest.version) == ("prized", "prized_sandboxes", "0.1.0")
    (provider,) = registration.tools_configuration
    assert provider.identity.name == "prized_sandboxes"
    assert [t.identity.name for t in provider.tools] == TOOLS
    assert [c.name for c in provider.credentials_schema] == ["api_key"]
    assert provider.credentials_schema[0].type.value == "secret-input"
    _, provider_cls, tools = registration.tools_mapping["prized_sandboxes"]
    assert provider_cls.__name__ == "PrizedProvider"
    assert sorted(tools) == sorted(TOOLS)


def test_marketplace_metadata():
    manifest = yaml.safe_load((ROOT / "manifest.yaml").read_text())
    assert manifest["repo"].startswith("https://github.com/")
    assert manifest["privacy"] == "PRIVACY.md" and (ROOT / "PRIVACY.md").is_file()
    assert re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]+", manifest["contact"])
    assert "dify" not in manifest["author"] and "langgenius" not in manifest["author"]
    assert (ROOT / "_assets" / manifest["icon"]).is_file()
    assert manifest["network"]["domains"] == ["api.prized.dev", "term.prized.dev"]
    readme = (ROOT / "README.md").read_text()
    assert "https://github.com/prized-dev/integrations" in readme


def test_requirements_are_bounded():
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        assert "==" in line, line  # exact pins: the Marketplace checks those for known vulnerabilities
    assert "dify-plugin==" in (ROOT / "requirements.txt").read_text()


def test_only_runtime_files_ship():
    ignored = [p.strip() for p in (ROOT / ".difyignore").read_text().splitlines() if p.strip() and p[0] != "#"]
    for name in ("tests", "pyproject.toml", ".env", "__pycache__", "*.difypkg"):
        assert name in ignored


@pytest.mark.parametrize("name", [*TOOLS, "run_command_one_off"])
def test_outputs_match_the_declared_schema(fake, files, name):
    """Every variable a tool returns is declared in its output_schema, so workflows can reference it."""
    one_off = name == "run_command_one_off"
    name = "run_command" if one_off else name
    schema = yaml.safe_load((ROOT / "tools" / f"{name}.yaml").read_text())["output_schema"]["properties"]
    fake.add()
    sid = "sbx_00000000000000aa"
    fake.files[(sid, "/home/prized/work/a.txt")] = b"text"
    files.content["https://files.dify.example/d.csv"] = b"data"
    params = {
        "create_sandbox": {},
        "run_command": {"sandbox_id": sid, "command": "true"},
        "run_code": {"sandbox_id": sid, "language": "bash", "code": "true"},
        "write_file": {"sandbox_id": sid, "path": "a.txt", "content": "text"},
        "upload_file": {"sandbox_id": sid, "file": StubFile("https://files.dify.example/d.csv", "d.csv")},
        "read_file": {"sandbox_id": sid, "path": "a.txt"},
    }.get(name, {"sandbox_id": sid})
    if one_off:
        params = {"command": "true"}
    variables, data, _ = outputs(invoke(tool(name), params))
    assert set(variables) <= set(schema), set(variables) - set(schema)
    assert set(data) <= set(schema), set(data) - set(schema)
    kinds = {"string": str, "number": (int, float), "boolean": bool}
    for key, value in variables.items():
        assert isinstance(value, kinds[schema[key]["type"]]), (key, value)


def test_no_cjk_or_template_text_in_user_facing_files():
    for path in [ROOT / "README.md", ROOT / "PRIVACY.md", *Path(ROOT, "tools").glob("*.yaml")]:
        text = path.read_text()
        assert not re.search(r"[㐀-䶿一-鿿]", text), path
        assert "please fill in" not in text.lower()


def test_no_secrets_in_the_source():
    pattern = re.compile(r"dcp_[A-Za-z0-9]{20,}")
    for path in ROOT.rglob("*"):
        if path.is_file() and not any(fnmatch.fnmatch(part, "__pycache__") for part in path.parts):
            if path.suffix in {".py", ".yaml", ".md", ".txt", ".toml"} and "tests" not in path.parts:
                assert not pattern.search(path.read_text()), path
