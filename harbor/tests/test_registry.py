import base64
import json
import shutil
import sys

import pytest

from harbor_prized._registry import (
    HUB,
    RegistryAuth,
    docker_config,
    image_registry,
)

FAKE_TOKEN = "dckr_pat_000000fake"  # obvious fakes, never real credentials
FAKE_GHCR = "ghp_000000fake"


def _basic(user: str, secret: str) -> dict[str, str]:
    return {"auth": base64.b64encode(f"{user}:{secret}".encode()).decode()}


def _config(tmp_path, document: dict, name: str = "docker"):
    directory = tmp_path / name
    directory.mkdir()
    (directory / "config.json").write_text(json.dumps(document))
    return directory


@pytest.mark.parametrize(
    ("image", "registry"),
    [
        ("python:3.12", HUB),
        ("python@sha256:" + "0" * 64, HUB),
        ("alexgshaw/fix-git:20251031", HUB),
        ("docker.io/alexgshaw/fix-git:20251031", HUB),
        ("registry-1.docker.io/library/python", HUB),
        ("ghcr.io/org/task:1", "ghcr.io"),
        ("GHCR.io/org/task", "ghcr.io"),
        ("public.ecr.aws/docker/library/python:3.12-slim", "public.ecr.aws"),
        ("localhost:5000/task", "localhost:5000"),
        ("localhost/task", "localhost"),
    ],
)
def test_image_registry(image, registry):
    assert image_registry(image) == registry


def test_env_gives_docker_hub_the_token(monkeypatch):
    monkeypatch.setenv("DOCKERHUB_USERNAME", "evals")
    monkeypatch.setenv("DOCKERHUB_TOKEN", FAKE_TOKEN)
    auth = RegistryAuth("env")
    assert auth.for_registry(HUB) == _basic("evals", FAKE_TOKEN)
    assert auth.for_registry("ghcr.io") is None  # a Docker Hub token only
    assert FAKE_TOKEN not in auth.describe()


def test_docker_reads_docker_config_first(tmp_path, monkeypatch):
    directory = _config(
        tmp_path,
        {
            "auths": {
                "https://index.docker.io/v1/": _basic("evals", FAKE_TOKEN),
                "ghcr.io": {"username": "evals", "password": FAKE_GHCR},
                "quay.io": {},
            }
        },
    )
    monkeypatch.setenv("DOCKER_CONFIG", str(directory))
    monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))
    auth = RegistryAuth("docker")
    assert auth.for_registry(HUB) == _basic("evals", FAKE_TOKEN)
    assert auth.for_registry("ghcr.io") == _basic("evals", FAKE_GHCR)
    assert auth.for_registry("quay.io") is None  # an entry with nothing in it
    assert auth.for_registry("registry.example.com") is None


def test_docker_without_docker_config_reads_the_home_one(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    _config(home, {"auths": {"docker.io": {"identitytoken": "id-000fake"}}}, ".docker")
    monkeypatch.delenv("DOCKER_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(home))
    assert RegistryAuth("docker").for_registry(HUB) == {"identitytoken": "id-000fake"}


def test_a_path_is_a_config_or_its_directory(tmp_path):
    directory = _config(
        tmp_path, {"auths": {"registry-1.docker.io": {"registrytoken": "rt"}}}
    )
    for source in (str(directory), str(directory / "config.json")):
        assert RegistryAuth(source).for_registry(HUB) == {"registrytoken": "rt"}


def test_missing_sources_are_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path / "none"))
    with pytest.raises(ValueError, match="docker login"):
        RegistryAuth("docker")
    with pytest.raises(ValueError, match="no Docker config"):
        RegistryAuth(str(tmp_path / "config.json"))
    monkeypatch.delenv("DOCKERHUB_USERNAME", raising=False)
    monkeypatch.delenv("DOCKERHUB_TOKEN", raising=False)
    with pytest.raises(ValueError, match="DOCKERHUB_USERNAME and DOCKERHUB_TOKEN"):
        RegistryAuth("env")


def test_an_unreadable_config_names_the_file_not_its_contents(tmp_path):
    directory = tmp_path / "docker"
    directory.mkdir()
    (directory / "config.json").write_text('{"auths": {"x": ' + FAKE_TOKEN)
    with pytest.raises(ValueError, match="can't read") as info:
        RegistryAuth(str(directory)).for_registry(HUB)
    assert FAKE_TOKEN not in str(info.value)


def test_the_sandbox_config_holds_only_the_one_entry():
    entry = _basic("evals", FAKE_TOKEN)
    assert json.loads(docker_config(HUB, entry)) == {
        "auths": {"https://index.docker.io/v1/": entry}
    }
    assert json.loads(docker_config("ghcr.io", entry)) == {"auths": {"ghcr.io": entry}}


# -- credential helpers, as Docker Desktop keeps them -------------------------

helpers_run = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("sh") is None,
    reason="the fake helpers are shell scripts",
)


def _helper(bin_dir, name: str, answers: dict[str, str], log) -> None:
    """A fake docker-credential-<name>: answers by server, logs what it was asked."""
    bin_dir.mkdir(exist_ok=True)
    cases = "".join(
        f"  {json.dumps(server)}) echo {json.dumps(json.dumps(answer))} ;;\n"
        for server, answer in answers.items()
    )
    script = bin_dir / f"docker-credential-{name}"
    script.write_text(
        "#!/bin/sh\n"
        '[ "$1" = get ] || exit 2\n'
        "read -r server\n"
        f'echo "{name} $server" >> {log}\n'
        'case "$server" in\n'
        f"{cases}"
        "  *) echo 'credentials not found in native keychain'; exit 1 ;;\n"
        "esac\n"
    )
    script.chmod(0o755)


@helpers_run
def test_a_credential_store_is_read_through_its_helper(tmp_path, monkeypatch):
    log = tmp_path / "asked"
    _helper(
        tmp_path / "bin",
        "desktop",
        {
            "https://index.docker.io/v1/": {"Username": "evals", "Secret": FAKE_TOKEN},
            "ghcr.io": {"Username": "<token>", "Secret": FAKE_GHCR},
        },
        log,
    )
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
    directory = _config(
        tmp_path,
        {"auths": {"https://index.docker.io/v1/": {}}, "credsStore": "desktop"},
    )
    auth = RegistryAuth(str(directory))

    assert auth.for_registry(HUB) == _basic("evals", FAKE_TOKEN)
    assert auth.for_registry("ghcr.io") == {"identitytoken": FAKE_GHCR}
    assert auth.for_registry("quay.io") is None  # the helper has none
    assert log.read_text().splitlines() == [
        "desktop https://index.docker.io/v1/",
        "desktop ghcr.io",
        "desktop quay.io",
    ]


@helpers_run
def test_a_registrys_own_helper_comes_first(tmp_path, monkeypatch):
    log = tmp_path / "asked"
    _helper(tmp_path / "bin", "desktop", {}, log)
    _helper(
        tmp_path / "bin",
        "gh",
        {"ghcr.io": {"Username": "evals", "Secret": FAKE_GHCR}},
        log,
    )
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
    directory = _config(
        tmp_path,
        {
            "auths": {"https://index.docker.io/v1/": _basic("evals", FAKE_TOKEN)},
            "credsStore": "desktop",
            "credHelpers": {"ghcr.io": "gh"},
        },
    )
    auth = RegistryAuth(str(directory))

    assert auth.for_registry("ghcr.io") == _basic("evals", FAKE_GHCR)
    # The store has nothing for Docker Hub: the file's own entry is used.
    assert auth.for_registry(HUB) == _basic("evals", FAKE_TOKEN)
    assert log.read_text().splitlines() == [
        "gh ghcr.io",
        "desktop https://index.docker.io/v1/",
    ]


@helpers_run
def test_a_missing_or_failing_helper_is_named(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    broken = bin_dir / "docker-credential-broken"
    broken.write_text("#!/bin/sh\necho 'keychain is locked' >&2\nexit 1\n")
    broken.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")

    absent = _config(tmp_path, {"credsStore": "absent"}, "absent")
    with pytest.raises(
        RuntimeError, match="docker-credential-absent, which is not on PATH"
    ):
        RegistryAuth(str(absent)).for_registry(HUB)
    failing = _config(tmp_path, {"credsStore": "broken"}, "failing")
    with pytest.raises(RuntimeError, match="keychain is locked"):
        RegistryAuth(str(failing)).for_registry(HUB)
