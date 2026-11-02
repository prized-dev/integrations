"""The runtime's config: every field next to `type = "custom"` and `import_path`."""

from __future__ import annotations

from typing import Self

from pydantic import Field, field_validator, model_validator
from verifiers.v1.runtimes import CustomRuntimeConfig

HOME = "/home/prized"
"""The sandbox user's home. Commands run as the user `prized`, without root."""
DEFAULT_WORKDIR = f"{HOME}/work"
RESERVED_LABELS = ("verifiers-prized", "vfp-run")
MAX_USER_LABELS = 32 - len(RESERVED_LABELS)


class PrizedConfig(CustomRuntimeConfig):
    """A Prized Sandboxes runtime. The environment is built by `setup` in one sandbox, which is
    then paused and forked into one copy per rollout: each copy starts with the parent's disk,
    memory and running processes."""

    template: str = "prized-base-v1"
    """Sandbox template: `prized-base-v1` (Ubuntu 24.04, Python 3, Node.js 22, git, build tools)
    or `prized-agent-v1` (the same plus Claude Code and Codex)."""
    cpu: float = 1
    """vCPUs, a whole number from 1 to 32. A task's `resources.cpu` applies when this is left at
    its default. Copies keep their parent's shape, so each shape gets its own parent."""
    memory: float = 2
    """Memory in GB, 1 to 128. A task's `resources.memory` applies when left at the default."""
    disk: float = 10
    """Disk in GB, 10 to 128. A task's `resources.disk` applies when left at the default."""
    workdir: str | None = None
    """Where commands run and relative paths resolve: the task's `workdir`, else
    /home/prized/work. Only /home/prized and /tmp are writable (no root)."""
    setup: list[str] = Field(default_factory=list)
    """Shell commands that build the environment, run once in order in the parent sandbox before
    it is paused (no root: `pip` into a venv or `--user`, `uv`, `npm` with a user prefix, git
    clones, downloads). A process started here in the background (`nohup ... &`) keeps running
    in every copy."""
    setup_env: list[str] = Field(default_factory=list)
    """Names of variables in this process's environment passed to the `setup` commands (the names
    are saved with the config, the values are not). A process `setup` leaves running keeps them in
    its environment, in every copy, where the rollout's commands (the agent's too) can read them.
    Never the Prized API key."""
    setup_timeout: float = Field(default=1800, ge=0)
    """Seconds each `setup` command may run; 0 for no limit."""
    parent: str | None = None
    """Fork from this sandbox instead of building one: a sandbox you set up yourself (or with
    `python -m verifiers_prized build`), running or paused. It is never paused, resumed or
    deleted here, and `template`, `cpu`, `memory`, `disk` and `setup` do not apply to it."""
    fork: bool = True
    """False: give each rollout a new sandbox that runs `setup` itself, with nothing shared.
    Slower; for comparison, or when no state may be shared between rollouts."""
    pause_parent: bool = True
    """Pause the parent after `setup`: it then bills for storage only, and every copy starts
    from the same paused state."""
    max_parents: int = Field(default=4, ge=1, le=64)
    """Copies start on their parent's machine, which holds about 31 copies of a 1 vCPU sandbox.
    When every parent's machine is full, another parent is built (running `setup` again), up to
    this many per shape and process."""
    capacity_wait: float = Field(default=600, ge=0)
    """Seconds a rollout keeps trying once it has been refused room (a full machine, a workspace
    quota); 0 fails it at the first refusal. The time a parent's `setup` takes does not count."""
    max_runtime_min: int | None = Field(default=None, ge=1)
    """Minutes each sandbox may run (Prized's default is 24 hours, and 60 minutes, the most, on the
    free tier; paused time does not count).
    Copies get their parent's."""
    blocking_run_timeout: float | None = Field(default=None, gt=0, le=240)
    """If set, `run` sends each command as one blocking call that is killed after this many
    seconds (240 at most): faster for many short commands, wrong for long ones. Unset, every
    command runs detached and is polled, with no time limit of its own."""
    labels: dict[str, str] = Field(default_factory=dict)
    """Labels put on every sandbox this runtime makes, to find them with `?label=key=value`."""
    api_key_env: str = "PRIZED_API_KEY"
    """The environment variable that holds the Prized API key."""
    base_url: str | None = None
    """The Prized API's origin; unset is https://api.prized.dev."""

    @field_validator("cpu")
    @classmethod
    def _whole_vcpus(cls, value: float) -> float:
        if value != int(value) or not 1 <= value <= 32:
            raise ValueError("cpu must be a whole number of vCPUs from 1 to 32")
        return value

    @field_validator("memory")
    @classmethod
    def _memory_range(cls, value: float) -> float:
        if not 1 <= value <= 128:
            raise ValueError("memory must be 1 to 128 GB")
        return value

    @field_validator("disk")
    @classmethod
    def _disk_range(cls, value: float) -> float:
        if not 10 <= value <= 128:
            raise ValueError("disk must be 10 to 128 GB")
        return value

    @field_validator("workdir")
    @classmethod
    def _absolute_workdir(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("/"):
            raise ValueError("workdir must be an absolute path")
        return value

    @field_validator("labels")
    @classmethod
    def _label_rules(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > MAX_USER_LABELS:
            raise ValueError(f"at most {MAX_USER_LABELS} labels")
        for key, item in value.items():
            if key in RESERVED_LABELS:
                raise ValueError(f"label {key!r} is set by verifiers-prized")
            if not 1 <= len(key) <= 64 or "=" in key or len(item) > 256:
                raise ValueError(f"label {key!r}: keys 1 to 64 characters without '=', values up to 256")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.parent is not None and not self.fork:
            raise ValueError("parent is a sandbox to fork from, so it needs fork = true")
        key = next((n for n in self.setup_env if n in (self.api_key_env, "PRIZED_API_KEY")), None)
        if key is not None:
            raise ValueError(
                f"setup_env may not pass the Prized API key (${key}) into the sandbox: the parent, "
                "every copy and the agent in it could read it"
            )
        return self

    @property
    def resolved_workdir(self) -> str:
        return self.workdir or DEFAULT_WORKDIR
