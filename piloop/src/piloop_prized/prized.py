"""``prized``: each worker of the pool is a Prized sandbox, a Firecracker microVM, forked from one warm parent.

```toml
[plugins.executor.pools.agents.provisioner.compute]
provider = "prized"
cpu = 2
memory_mb = 8192
setup = ["npm install -g @openai/codex@0.166.0"]
```

The provider sets up one sandbox first, the **warm parent**: ``piloop`` (the release that runs the daemon, unless
``piloop`` names another) and the lines of ``setup``, then pauses it. Each worker is a fork of it: a live copy that
starts with the parent's files and memory, so no worker runs the setup again. A parent sits on one machine, and its
copies start there: when that machine is full, the provider sets up another parent, up to ``max_parents``. With
``fork = false``, each worker is a fresh sandbox that runs the setup itself before its worker starts.

Sandboxes start from a Prized template, ``prized-base-v1`` or ``prized-agent-v1`` (Claude Code and Codex), as the
user ``prized`` without sudo: what an image would add goes in ``setup``, under ``/home/prized``. There are no custom
images, no GPUs and no egress allowlist yet, and the sandboxes run in us-west-2 (Oregon): a key that asks for any of
these is refused with what to do instead.

Once a sandbox runs, the provider writes the worker's credential to a file in it (mode 0600), and starts
``piloop worker`` in the background with a command that reads the file and deletes it: the credential is never in a
command line, a label or the sandbox's environment. Its labels name its worker, its pool and this server's owner, so
``list`` finds this server's sandboxes alone, and ``stop`` deletes by those labels. A warm parent has the owner's
label too, never a worker's name: ``list`` never shows it, and ``python -m piloop_prized cleanup`` deletes it with the
rest. A paused parent deletes itself ``parent_hours`` after its pause.

The server needs ``PRIZED_API_KEY`` (a key with Sandboxes write), and this package beside piloop (``INSTALL``).
"""

import hashlib
import json
import math
import os
import re
import shlex
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError, distribution
from importlib.util import find_spec
from typing import Any, Literal, Self

import prized
from piloop.capabilities import (
    Check,
    Instance,
    InstanceState,
    Launch,
    ProvisionerConfig,
    ProvisionerHost,
    StartRefused,
)
from piloop.otel import Tracer
from pydantic import Field, model_validator

OWNER, NAME, POOL = 'piloop-owner', 'piloop-name', 'piloop-pool'
"""The labels of a worker's sandbox."""
ROLE, RECIPE = 'piloop-role', 'piloop-recipe'
"""The labels of a warm parent, beside ``OWNER``: ``ROLE`` is ``parent``, ``RECIPE`` the digest of what it holds."""
PARENT = 'parent'
KEY = 'PRIZED_API_KEY'
STATE = '/home/prized/.piloop-prized'
"""Where the provider keeps its files in a sandbox: ``files`` writes only under ``/home/prized``."""
CREDENTIAL = f'{STATE}/worker.env'
"""Where the worker's credential waits until its command reads it and deletes it."""
SETUP = f'{STATE}/setup.sh'
SETUP_LOG = f'{STATE}/setup.log'
WORKER_LOG = f'{STATE}/worker.log'
UV_INSTALLER = 'https://astral.sh/uv/install.sh'
INSTALL = (
    "uv tool install --reinstall piloop --with 'piloop-prized @"
    " git+https://github.com/prized-dev/integrations#subdirectory=piloop'"
)
FULL_SECONDS = 60.0
"""How long a parent whose machine was full is skipped before a fork is tried on it again."""
TAIL = 3000
"""The characters of a failed setup's output that its error keeps."""
ENV_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,63}')

RUNNING = {'running'}
BOOTING = {'starting', 'resizing'}
"""The states of a sandbox that will run: anything else (pausing, paused, deleting, deleted, error) has no worker."""

REFUSED = {
    'missing_api_key',
    'invalid_api_key',
    'unauthorized',
    'token_expired',
    'key_access_denied',
    'paid_balance_required',
    'insufficient_balance',
    'sandbox_quota_exceeded',
    'template_not_found',
    'invalid_resources',
    'sandbox_capacity',
    'sandboxes_disabled',
    'sandbox_create_disabled',
    'sandbox_fork_disabled',
    'parent_not_forkable',
}
"""The codes of a refusal that a retry does not cure (``StartRefused``): the key, the balance, a quota, the shape, no
capacity (the SDK waited out ``Retry-After`` already), maintenance. Anything else is tried again by the next pass."""
GONE = {'not_found', 'sandbox_deleted'}

NO_IMAGE = (
    'Prized sandboxes start from a template (prized-base-v1, or prized-agent-v1 with Claude Code and Codex), not an'
    ' image: custom images come with Docker mode. Put what the image adds in `setup` (shell lines run as the user'
    ' prized, without sudo, under /home/prized): it runs once, in the warm parent that every worker forks from.'
)
FOREIGN = {
    'image': NO_IMAGE,
    'image_id': NO_IMAGE,
    'registry': NO_IMAGE,
    'snapshot': NO_IMAGE,
    'gpu': 'Prized sandboxes have no GPU (a GPU machine is a Prized box, another product): keep a GPU pool on Modal.',
    'outbound_domains': (
        'Prized sandboxes have no egress allowlist yet: every sandbox reaches the internet. Leave the key out, or keep'
        ' a pool whose network must be fenced on Modal.'
    ),
    'secrets': (
        "Prized has no named secrets: list variables of the daemon's environment in `secret_env`, and each worker gets"
        ' their values in its credential file, never in its environment or a command line.'
    ),
    'app': "a Modal key: the sandboxes are in the workspace of the daemon's PRIZED_API_KEY",
    'environment': "a Modal key: the sandboxes are in the workspace of the daemon's PRIZED_API_KEY",
}
"""Keys of the other providers' tables that a Prized sandbox cannot honour, and what to do instead."""
REGION, CLOUD = 'us-west-2', 'aws'


def within[T](seconds: float, call: Callable[[], T], what: str) -> T:
    """``call()``, on a thread of its own, or ``TimeoutError`` after ``seconds``. A call that times out goes on.

    piloop's own ``kit.deadline.within``, which piloop 0.5.0 lacks: a verb of a provisioner never holds the pass.
    """
    values: list[T] = []
    errors: list[BaseException] = []
    done = threading.Event()

    def run() -> None:
        try:
            values.append(call())
        except BaseException as e:  # handed to the caller, which raises it
            errors.append(e)
        finally:
            done.set()

    threading.Thread(target=run, name=f'within: {what}', daemon=True).start()
    if not done.wait(seconds):
        raise TimeoutError(f'{what} did not answer in {seconds:g} s')
    if errors:
        raise errors[0]
    return values[0]


class Prized(ProvisionerConfig):
    """``provider = "prized"``: a Prized sandbox for each worker, forked from a warm parent."""

    template: str = 'prized-agent-v1'
    """The template of each sandbox: ``prized-base-v1`` (Ubuntu, Python 3.12, Node 22, git) or ``prized-agent-v1``
    (that, with Claude Code and Codex)."""
    cpu: int | None = Field(default=None, gt=0, le=32)
    """The vCPUs of each sandbox. Unset: the template's."""
    memory_mb: int | None = Field(default=None, gt=0)
    """The MiB of memory of each sandbox: the microVM's whole memory, so a hard limit. Unset: the template's, or
    ``memory_limit_mb``."""
    memory_limit_mb: int | None = Field(default=None, gt=0)
    """Taken for a table written for Modal: the microVM gets this much memory, which is its hard limit."""
    setup: list[str] = []
    """Shell lines that make a sandbox ready for the pool's jobs, after ``piloop`` is installed: run in order, in one
    bash script with ``set -euo pipefail``, from ``/home/prized/work``, as ``prized`` without sudo."""
    piloop: str | None = None
    """What ``uv tool install`` installs as piloop in each sandbox. Unset: the release that runs the daemon, or the git
    commit that it was installed from."""
    fork: bool = True
    """Fork every worker from a warm parent that ran the setup once. False: each worker is a fresh sandbox that runs the
    setup itself before its worker starts."""
    parent: str | None = None
    """A paused sandbox that holds the setup already (``sbx_…``, from ``python -m piloop_prized parent``), to fork in
    place of one that the provider sets up: for a setup that takes longer than the pool's ``register_seconds``."""
    max_parents: int = Field(default=4, ge=1)
    """The most warm parents that the provider sets up: one more each time that every parent's machine is full."""
    timeout_hours: float = Field(default=24, gt=0, le=168)
    """The longest that a sandbox runs: Prized deletes it then. Each fork gets the whole of it."""
    parent_hours: float = Field(default=24, ge=0, le=672)
    """How long a warm parent stays paused before Prized deletes it: the next start then sets up another. 0: until
    ``python -m piloop_prized cleanup``."""
    setup_seconds: float = Field(default=3600, gt=0)
    """The longest that the setup may run in a warm parent: one that runs longer fails the starts that wait for it."""
    start_seconds: float = Field(default=600, gt=0)
    """The longest that a start may take: one that takes longer fails, and the pool tries again. A start that waits for
    a warm parent's setup fails in this time too, and the next one waits for the same setup."""
    call_seconds: float = Field(default=60, gt=0)
    """The longest that a list or a stop may take: one that hangs fails, and the next pass tries again."""
    env: dict[str, str] = {}
    """More environment of each worker, such as ``PILOOP_PLUGINS``: never a secret."""
    secret_env: list[str] = []
    """Variables of the daemon's environment whose values each worker gets beside its credential, in the same file:
    what the worker process itself needs, such as its CAS's keys. A job's secrets come from piloop's ``secrets``."""
    region: Literal['us-west-2'] | None = None
    """Taken for a table that names it: Prized sandboxes run in us-west-2 (Oregon) only."""
    cloud: Literal['aws'] | None = None
    """Taken for a table that names it: Prized sandboxes run on AWS."""

    @model_validator(mode='before')
    @classmethod
    def _foreign(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return data
        for key, why in FOREIGN.items():
            if data.get(key) not in (None, [], {}):
                raise ValueError(f'{key}: {why}')
        if (region := data.get('region')) not in (None, REGION):
            raise ValueError(f'region: Prized sandboxes run in {REGION} (Oregon) only, not {region!r}')
        if (cloud := data.get('cloud')) not in (None, CLOUD):
            raise ValueError(f'cloud: Prized sandboxes run on AWS in {REGION}: a pin to {cloud!r} cannot hold')
        return data

    @model_validator(mode='after')
    def _coherent(self) -> Self:
        if (mb := self.memory_mb) is not None and (limit := self.memory_limit_mb) is not None and mb > limit:
            raise ValueError('memory_limit_mb is below memory_mb')
        if self.parent is not None and not self.fork:
            raise ValueError('a parent is forked: fork = false starts each worker as a fresh sandbox')
        if self.parent is not None and (self.setup or self.piloop is not None):
            raise ValueError('a parent holds its setup already: setup and piloop are for one that the provider sets up')
        for name in [*self.env, *self.secret_env]:
            if not ENV_NAME.fullmatch(name):
                raise ValueError(f'{name!r} is no variable name: [A-Za-z_][A-Za-z0-9_]*, up to 64 characters')
        return self

    def memory(self) -> int | None:
        """The MiB of each microVM."""
        return self.memory_limit_mb or self.memory_mb

    def open(self, host: ProvisionerHost) -> PrizedProvisioning:
        return PrizedProvisioning(self, host.owner, host.tracer)

    def checks(self) -> list[Check]:
        """The ``prized`` SDK, and the daemon's ``PRIZED_API_KEY``."""
        if find_spec('prized') is not None:
            sdk = Check('prized', 'prized is installed')
        else:
            sdk = Check('prized', 'prized is not installed', f'`{INSTALL}`')
        if os.environ.get(KEY):
            login = Check('prized login', f'{KEY} is set')
        else:
            login = Check(
                'prized login', f'{KEY} is not set', f'set {KEY} (a key with Sandboxes write) where the daemon runs'
            )
        return [sdk, login]


def installed_piloop() -> str:
    """What ``uv tool install`` takes for the piloop that runs here: its git commit, else its release."""
    try:
        dist = distribution('piloop')
    except PackageNotFoundError:
        return 'piloop'
    raw = dist.read_text('direct_url.json')
    if raw:
        found = json.loads(raw)
        vcs, url = found.get('vcs_info') or {}, found.get('url')
        if vcs.get('vcs') == 'git' and vcs.get('commit_id') and url:
            return f'git+{url}@{vcs["commit_id"]}'
    return f'piloop=={dist.version}'


def setup_script(config: Prized, piloop: str) -> str:
    """The script that makes a sandbox ready: uv, piloop, then the lines of ``setup``."""
    lines = [
        '#!/usr/bin/env bash',
        '# piloop-prized: the setup of a worker sandbox.',
        'set -euo pipefail',
        'export PATH="$HOME/.local/bin:$PATH"',
        'mkdir -p "$HOME/work" && cd "$HOME/work"',
        f'command -v uv >/dev/null 2>&1 || curl -LsSf {UV_INSTALLER} | sh',
        f'uv tool install --quiet {shlex.quote(piloop)}',
        *config.setup,
        'piloop --version',
    ]
    return '\n'.join(lines) + '\n'


def recipe(config: Prized, piloop: str) -> str:
    """The digest of what a warm parent holds: a parent of another recipe is not forked for this one."""
    held = {
        'template': config.template,
        'cpu': config.cpu,
        'memory_mb': config.memory(),
        'timeout_hours': config.timeout_hours,
        'script': setup_script(config, piloop),
    }
    return hashlib.sha256(json.dumps(held, sort_keys=True).encode()).hexdigest()[:16]


def refusal(e: prized.PrizedError, what: str) -> Exception:
    """``StartRefused`` for a refusal that a retry does not cure, else ``e`` itself, which the next pass retries."""
    if e.code in REFUSED:
        return StartRefused(f'Prized refused {what}: {e}')
    return e


def _state(state: str) -> InstanceState:
    if state in RUNNING:
        return InstanceState.RUNNING
    return InstanceState.PENDING if state in BOOTING else InstanceState.ENDED


def _quietly_delete(sandbox: prized.Sandbox) -> None:
    """Delete ``sandbox``, and release it: best effort, as a sandbox that the caller cannot use any more."""
    with suppress(Exception):
        sandbox.delete()
    with suppress(Exception):
        sandbox.close()


class SetupFailed(StartRefused):
    """The setup exited non-zero, or ran past ``setup_seconds``: a bad recipe, as a bad image is."""


class ParentNotReady(RuntimeError):
    """A warm parent is still being set up: the start fails, and the next one waits for the same setup."""


@dataclass
class Build:
    """One warm parent being set up, on a thread of its own."""

    started: float = field(default_factory=time.monotonic)
    done: threading.Event = field(default_factory=threading.Event)
    parent: str | None = None
    error: BaseException | None = None


def build_parent(config: Prized, labels: Mapping[str, str], piloop: str, tracer: Tracer | None = None) -> str:
    """Set up a warm parent and pause it: its id.

    Raises:
        SetupFailed: The setup exited non-zero, or ran past ``setup_seconds``. The sandbox is deleted.
        StartRefused: Prized refused the sandbox.
    """
    began = time.monotonic()
    try:
        parent = prized.Sandbox.create(
            config.template,
            vcpu=config.cpu,
            memory_mb=config.memory(),
            labels=dict(labels),
            max_runtime_min=math.ceil(config.timeout_hours * 60),
            auto_pause_min=0,  # paused by the provider once its setup is done, never by idleness
            auto_delete_min=math.ceil(config.parent_hours * 60),
            timeout=config.start_seconds,
        )
    except prized.PrizedError as e:
        raise refusal(e, 'a warm parent') from e
    try:
        parent.files.write(SETUP, setup_script(config, piloop), mode=0o700)
        run = f'bash {SETUP} > {SETUP_LOG} 2>&1; code=$?; tail -c {TAIL} {SETUP_LOG}; exit $code'
        try:
            result = parent.exec(run, timeout=config.setup_seconds)
        except prized.CommandTimeoutError as e:
            raise SetupFailed(
                f'the setup of the warm parent {parent.id} ran past setup_seconds = {config.setup_seconds:g}:'
                f' {e.result.stdout[-TAIL:]}'
            ) from e
        if result.exit_code != 0:
            raise SetupFailed(
                f'the setup of the warm parent {parent.id} exited {result.exit_code}: {result.stdout[-TAIL:]}'
            )
        parent.pause()
    except BaseException:
        _quietly_delete(parent)
        raise
    parent.close()
    if tracer is not None:
        tracer.event('prized.parent_ready', parent=parent.id, seconds=round(time.monotonic() - began))
    return parent.id


class Warm:
    """The warm parents of one recipe and owner: paused sandboxes that workers fork. One is set up at a time."""

    def __init__(self, config: Prized, owner: str, piloop: str, tracer: Tracer) -> None:
        self._config, self._owner, self._piloop, self._tracer = config, owner, piloop, tracer
        self.labels = {OWNER: owner, ROLE: PARENT, RECIPE: recipe(config, piloop)}
        self._lock = threading.Lock()
        self._ready: list[str] = [] if config.parent is None else [config.parent]
        self._found = config.parent is not None
        """Whether the workspace was searched for this recipe's parents, which a daemon before a restart set up."""
        self._full: dict[str, float] = {}
        self._build: Build | None = None

    def fork(self, labels: Mapping[str, str], deadline: float) -> prized.Sandbox:
        """A running fork of a warm parent with room on its machine, labelled ``labels`` alone.

        Raises:
            ParentNotReady: The parent that the fork waits for is still being set up at ``deadline``.
            StartRefused: Prized refused the fork, every parent's machine is full and no more may be set up, or the
                setup failed.
        """
        full: set[str] = set()
        while True:
            parent = self._pick(deadline, full)
            try:
                handle = prized.Sandbox.connect(parent)
                try:
                    [child] = handle.fork(1, labels=dict(labels), timeout=max(deadline - time.monotonic(), 1.0))
                finally:
                    handle.close()
                return child
            except prized.PrizedError as e:
                if e.code == 'fork_capacity':
                    self._tracer.event('prized.parent_full', parent=parent)
                    full.add(parent)
                    with self._lock:
                        self._full[parent] = time.monotonic()
                    continue
                if e.code in GONE or (e.code == 'parent_not_forkable' and self._config.parent is None):
                    self._tracer.event('prized.parent_gone', parent=parent, code=e.code)
                    self._drop(parent, e)
                    continue
                raise refusal(e, f'a fork of the warm parent {parent}') from e

    def _drop(self, parent: str, e: prized.PrizedError) -> None:
        if self._config.parent is not None:
            raise StartRefused(
                f'the parent {parent} cannot be forked ({e.code}): set up another with `python -m piloop_prized'
                ' parent`, and name it in `parent`'
            ) from e
        with self._lock:
            if parent in self._ready:
                self._ready.remove(parent)

    def _pick(self, deadline: float, full: set[str]) -> str:
        """A parent to fork: a ready one whose machine had room, else the one being set up, once it is.

        ``full``: the parents whose machine this start found full already, which it does not try again.
        """
        while True:
            with self._lock:
                if not self._found:
                    self._ready, self._found = self._search(), True
                now = time.monotonic()
                for parent in self._ready:
                    if parent not in full and now - self._full.get(parent, -math.inf) >= FULL_SECONDS:
                        return parent
                if self._build is None:
                    if self._config.parent is not None:
                        raise StartRefused(
                            f'the machine of the parent {self._config.parent} is full: the pool tries again later.'
                            ' Fewer workers, or a second parent from `python -m piloop_prized parent`, fit more.'
                        )
                    if len(self._ready) >= self._config.max_parents:
                        raise StartRefused(
                            f'the machine of every warm parent ({len(self._ready)}) is full: the pool tries again'
                            ' later. Fewer workers, a smaller shape, or more max_parents fit more.'
                        )
                    self._build = self._begin()
                build = self._build
            if not build.done.wait(max(deadline - time.monotonic(), 0.0)):
                raise ParentNotReady(
                    f'a warm parent is still being set up, {time.monotonic() - build.started:.0f} s in (at most'
                    f' setup_seconds = {self._config.setup_seconds:g}): the next start waits for the same one'
                )
            with self._lock:
                if self._build is build:
                    self._build = None
            if build.error is not None:
                raise build.error

    def _search(self) -> list[str]:
        """This recipe's paused parents, oldest first.

        One that is not paused and older than a setup can take was being set up by a daemon that is gone: it is
        deleted. A younger one may be another pool's of the same recipe, still being set up: it is left.
        """
        ready = []
        stale = timedelta(seconds=self._config.start_seconds + self._config.setup_seconds)
        now = datetime.now(UTC)
        for sandbox in reversed(prized.Sandbox.list(labels=self.labels)):
            created = sandbox.created_at
            if created is not None and created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if sandbox.state == 'paused':
                ready.append(sandbox.id)
            elif sandbox.state not in ('deleting', 'deleted') and created is not None and now - created > stale:
                _quietly_delete(sandbox)
                continue
            sandbox.close()
        return ready

    def _begin(self) -> Build:
        build = Build()

        def run() -> None:
            try:
                parent = build_parent(self._config, self.labels, self._piloop, self._tracer)
                with self._lock:
                    self._ready.append(parent)
                build.parent = parent
            except BaseException as e:  # handed to the starts that wait for it
                self._tracer.event('prized.parent_failed', error=f'{type(e).__name__}: {e}'[:500])
                build.error = e
            finally:
                build.done.set()

        threading.Thread(target=run, name='piloop-prized: warm parent', daemon=True).start()
        return build


class PrizedProvisioning:
    """``Provisioning``: the sandboxes labelled with this server's owner."""

    def __init__(self, config: Prized, owner: str, tracer: Tracer) -> None:
        self._config, self._owner, self._tracer = config, owner, tracer
        self._piloop = config.piloop or installed_piloop()
        self._warm = Warm(config, owner, self._piloop, tracer) if config.fork else None
        self._lock = threading.Lock()
        self._names: dict[str, threading.Lock] = {}
        """A lock for each name that a verb is on: a start or a stop that a deadline left running still holds it, so the
        next verb on that name waits for it, and a repeated start never makes a second sandbox."""

    def _of(self, name: str) -> threading.Lock:
        with self._lock:
            return self._names.setdefault(name, threading.Lock())

    def list(self) -> Sequence[Instance]:
        return within(self._config.call_seconds, self._list, f'{type(self).__name__}.list')

    def _list(self) -> Sequence[Instance]:
        found = []
        for sandbox in prized.Sandbox.list(labels={OWNER: self._owner}):
            labels = sandbox.labels
            if NAME in labels and POOL in labels:  # a warm parent has neither
                found.append(
                    Instance(
                        name=labels[NAME], pool=labels[POOL], state=_state(sandbox.state), handle={'id': sandbox.id}
                    )
                )
            sandbox.close()
        return found

    def start(self, launch: Launch) -> None:
        return within(self._config.start_seconds, lambda: self._start(launch), f'{type(self).__name__}.start')

    def _start(self, launch: Launch) -> None:
        """Fork or create the sandbox, write the credential, and start the worker. One of its name that boots or runs
        is the start that came before.

        A sandbox whose worker did not start is deleted, and the error raised: the next start makes another one.

        Raises:
            StartRefused: Prized refused what was asked (the key, the balance, a quota, the shape), or the setup failed.
            ParentNotReady: The warm parent is still being set up.
        """
        seconds = self._config.start_seconds
        deadline = time.monotonic() + max(seconds - 5, seconds * 0.9)  # before ``within`` gives up, to say why
        with self._of(launch.name):
            mine = prized.Sandbox.list(labels={OWNER: self._owner, NAME: launch.name})
            live = any(_state(s.state) is not InstanceState.ENDED for s in mine)
            for sandbox in mine:
                sandbox.close()
            if live:
                return
            exports = self._exports(launch)
            labels = {OWNER: self._owner, NAME: launch.name, POOL: launch.pool}
            sandbox = self._warm.fork(labels, deadline) if self._warm is not None else self._fresh(labels)
            try:
                sandbox.files.write(CREDENTIAL, exports, mode=0o600)
                if self._warm is None:
                    sandbox.files.write(SETUP, setup_script(self._config, self._piloop), mode=0o700)
                sandbox.exec(self._command(launch), background=True)
            except BaseException:
                _quietly_delete(sandbox)
                raise
            sandbox.close()

    def _fresh(self, labels: Mapping[str, str]) -> prized.Sandbox:
        config = self._config
        try:
            return prized.Sandbox.create(
                config.template,
                vcpu=config.cpu,
                memory_mb=config.memory(),
                labels=dict(labels),
                max_runtime_min=math.ceil(config.timeout_hours * 60),
                auto_pause_min=0,  # Prized counts API calls as activity, not a worker that runs a job
                timeout=config.start_seconds,
            )
        except prized.PrizedError as e:
            raise refusal(e, f'the sandbox {labels[NAME]}') from e

    def _exports(self, launch: Launch) -> str:
        """The worker's environment as the lines of a shell script that exports it: its credential, ``secret_env`` and
        ``env``.

        Raises:
            StartRefused: A variable of ``secret_env`` is not set where the daemon runs.
        """
        missing = [name for name in self._config.secret_env if name not in os.environ]
        if missing:
            raise StartRefused(f"secret_env names {', '.join(missing)}, which the daemon's environment does not set")
        values = {
            **self._config.env,
            **{name: os.environ[name] for name in self._config.secret_env},
            **{k: v.get_secret_value() for k, v in launch.env.items()},
        }
        return ''.join(f'export {k}={shlex.quote(v)}\n' for k, v in values.items())

    def _command(self, launch: Launch) -> str:
        """Read the credential and delete it, then run the worker, after the setup when the sandbox is not a fork."""
        worker = (
            f'. {CREDENTIAL} && rm -f {CREDENTIAL} && export PATH="$HOME/.local/bin:$PATH"'
            f' && exec {shlex.join(launch.argv)} >> {WORKER_LOG} 2>&1'
        )
        if self._warm is not None:
            return worker
        return f'bash {SETUP} > {SETUP_LOG} 2>&1 || {{ rm -f {CREDENTIAL}; exit 1; }}; {worker}'

    def stop(self, name: str) -> None:
        return within(self._config.call_seconds, lambda: self._stop(name), f'{type(self).__name__}.stop')

    def _stop(self, name: str) -> None:
        with self._of(name):
            prized.Sandbox.delete_by_labels({OWNER: self._owner, NAME: name})


def cleanup(owner: str) -> list[str]:
    """Delete every sandbox of the piloop server ``owner``: its workers and its warm parents. The ids deleted."""
    return prized.Sandbox.delete_by_labels({OWNER: owner})
