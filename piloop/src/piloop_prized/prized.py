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

The setup runs on a thread of its own. A start that finds it running waits ``parent_wait_seconds`` at most, then fails,
and the pool's next pass tries again, so a long setup never holds piloop's scaling pass.

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
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
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
BUILDS_HOUR = 3600.0
"""The window of the cap on setups: at most ``max_parents + 2`` of one recipe in it, so a parent that every fork refuses
is never set up again and again."""
TAIL = 3000
"""The characters of a failed setup's output that its error keeps."""
ENV_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,63}')

RUNNING = {'running'}
BOOTING = {'starting', 'resizing'}
"""The states of a sandbox that will run: anything else (pausing, paused, deleting, deleted, error) has no worker."""
LEAVING = {'deleting', 'deleted'}
SETTLING = {'starting', 'pausing', 'resizing', 'resuming'}
"""The states of a parent on its way to paused or running: a fork waits for it to settle."""

REFUSED = {
    'missing_api_key',
    'invalid_api_key',
    'unauthorized',
    'token_expired',
    'key_access_denied',
    'actor_required',
    'paid_balance_required',
    'insufficient_balance',
    'sandbox_quota_exceeded',
    'template_not_found',
    'template_inactive',
    'template_runtime_incompatible',
    'bad_request',
    'unknown_field',
    'conflicting_fields',
    'label_required',
    'durability_incompatible',
    'persistent_sandbox_unsupported',
    'idempotency_conflict',
    'workspace_held',
    'workspace_closing',
    'sandboxes_disabled',
    'sandbox_create_disabled',
    'sandbox_fork_disabled',
    'parent_not_forkable',
}
"""The codes of a refusal that a retry does not cure (``StartRefused``): the key, the balance, a quota, the template,
the request, the workspace, maintenance. So is any code ``invalid_…``, and any 400, 401, 402, 403 or 422. Anything else
is tried again by the next pass: ``sandbox_capacity`` among them (the SDK waited out ``Retry-After`` already), which the
pool must not escalate as a broken provisioner."""
REFUSED_STATUS = {400, 401, 402, 403, 422}
GONE = {'not_found', 'sandbox_deleted'}
FULL = {'fork_capacity', 'fork_unsupported_host'}
"""A fork refused for its parent's machine: another parent, or this one later, may take it."""
NO_MEMORY = {'resized_while_paused', 'memory_discarded'}
"""Reasons of ``parent_not_forkable`` that a resume and a pause cure: the parent kept its files, not its memory."""

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
OF_THE_PARENT = (
    'template',
    'cpu',
    'memory_mb',
    'memory_limit_mb',
    'timeout_hours',
    'setup',
    'piloop',
    'max_parents',
    'parent_hours',
    'setup_seconds',
    'parent_wait_seconds',
)
"""Keys that a named ``parent`` fixes already, or that only a parent that the provider sets up uses: a fork gets its
parent's template, shape and run time."""


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
    """The vCPUs of each sandbox, whole. Unset: the template's."""
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
    place of one that the provider sets up: ready before the daemon starts, and never deleted by the provider. Its forks
    get its template, shape and run time."""
    max_parents: int = Field(default=4, ge=1)
    """The most warm parents that the provider sets up: one more each time that every parent's machine is full."""
    timeout_hours: float = Field(default=24, gt=0, le=168)
    """The longest that a sandbox runs: Prized deletes it then. Each fork gets the whole of it."""
    parent_hours: float = Field(default=24, ge=0, le=672)
    """How long a warm parent stays paused before Prized deletes it: the next start then sets up another. 0: until
    ``python -m piloop_prized cleanup``."""
    setup_seconds: float = Field(default=3600, gt=0)
    """The longest that the setup may run in a warm parent: one that runs longer fails, and the next start sets up
    another."""
    parent_wait_seconds: float = Field(default=10, ge=0)
    """How long a start waits for a warm parent that is being set up before it fails, so that piloop's next pass tries
    again: short, so a long setup never holds the pass."""
    start_seconds: float = Field(default=600, gt=0)
    """The longest that a start may take: one that takes longer fails, and the pool tries again."""
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
        if isinstance(cpu := data.get('cpu'), float) and not cpu.is_integer():
            raise ValueError(f'cpu: a Prized sandbox has whole vCPUs, not {cpu:g}')
        if data.get('parent') is not None and (fixed := [k for k in OF_THE_PARENT if k in data]):
            raise ValueError(
                f'{", ".join(fixed)}: the parent fixes these already (a fork gets its template, shape and run time),'
                ' or they are for a parent that the provider sets up: set them when you set the parent up'
            )
        return data

    @model_validator(mode='after')
    def _coherent(self) -> Self:
        if (mb := self.memory_mb) is not None and (limit := self.memory_limit_mb) is not None and mb > limit:
            raise ValueError('memory_limit_mb is below memory_mb')
        if self.parent is not None and not self.fork:
            raise ValueError('a parent is forked: fork = false starts each worker as a fresh sandbox')
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
    """The digest of what a warm parent holds, and of how long its setup may take: a parent of another recipe is never
    forked, or deleted as stale, for this one."""
    held = {
        'template': config.template,
        'cpu': config.cpu,
        'memory_mb': config.memory(),
        'timeout_hours': config.timeout_hours,
        'setup_seconds': config.setup_seconds,
        'script': setup_script(config, piloop),
    }
    return hashlib.sha256(json.dumps(held, sort_keys=True).encode()).hexdigest()[:16]


def refused(e: prized.PrizedError) -> bool:
    """Whether a retry does not cure ``e``."""
    return e.code in REFUSED or e.code.startswith('invalid_') or e.status in REFUSED_STATUS


def refusal(e: prized.PrizedError, what: str) -> Exception:
    """``StartRefused`` for a refusal that a retry does not cure, else ``e`` itself, which the next pass retries."""
    if refused(e):
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
    """A warm parent is still being set up, or settling: the start fails, and the next one tries again."""


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
        self._named = config.parent is not None
        """A parent that the table names: the provider never sets one up, nor deletes it."""
        self.labels = {OWNER: owner, ROLE: PARENT, RECIPE: recipe(config, piloop)}
        self._lock = threading.Lock()
        self._ready: list[str] = [config.parent] if config.parent is not None else []
        self._swept = self._named
        """Whether the workspace was searched for this recipe's parents, which a daemon before a restart set up."""
        self._full: dict[str, float] = {}
        self._build: Build | None = None
        self._builds: deque[float] = deque()
        """When each setup of the last ``BUILDS_HOUR`` began."""

    def fork(self, labels: Mapping[str, str], deadline: float) -> prized.Sandbox:
        """A running fork of a warm parent with room on its machine, labelled ``labels`` alone.

        Raises:
            ParentNotReady: The parent is still being set up after ``parent_wait_seconds``, or settling.
            StartRefused: Prized refused the fork, every parent's machine is full and no more may be set up, or the
                setup failed.
        """
        tried: set[str] = set()
        """The parents whose machine this start found full: it does not try them again."""
        mended: set[str] = set()
        """The parents that this start resumed and paused again, or found settled: it tries each once more."""
        built = False
        while True:
            parent, built = self._pick(deadline, tried, built)
            try:
                handle = prized.Sandbox.connect(parent)
            except prized.PrizedError as e:
                if e.code not in GONE:
                    raise refusal(e, f'the warm parent {parent}') from e
                self._drop(parent, None, e)
                continue
            try:
                return handle.fork(1, labels=dict(labels), timeout=max(deadline - time.monotonic(), 1.0))[0]
            except prized.PrizedError as e:
                self._refused(handle, e, tried, mended)
            finally:
                handle.close()

    def _refused(self, handle: prized.Sandbox, e: prized.PrizedError, tried: set[str], mended: set[str]) -> None:
        """Make way for the next try of a fork that Prized refused, or raise why there is none."""
        parent, reason = handle.id, e.reason or ''
        self._tracer.event('prized.fork_refused', parent=parent, code=e.code, reason=reason)
        if e.code in FULL:
            tried.add(parent)
            with self._lock:
                self._full[parent] = time.monotonic()
            return
        if e.code in GONE:
            self._drop(parent, None, e)
            return
        if e.code == 'parent_not_forkable' and reason in NO_MEMORY and parent not in mended:
            mended.add(parent)  # it kept its files, not its memory: running it and pausing it again gives it both
            try:
                handle.resume()
                handle.pause()
            except prized.PrizedError as again:
                raise refusal(again, f'a resume and a pause of the warm parent {parent}') from again
            return
        if e.code == 'parent_not_forkable':
            if self._named:
                raise StartRefused(f'Prized cannot fork the parent {parent}: {e}') from e
            self._drop(parent, handle, e)
            raise StartRefused(f'Prized cannot fork the warm parent {parent}, which is deleted: {e}') from e
        if e.code == 'parent_not_running':
            handle.refresh()
            if handle.state in SETTLING or (handle.state in ('paused', 'running') and parent in mended):
                raise ParentNotReady(f'the warm parent {parent} is {handle.state}: the next start tries again') from e
            if handle.state in ('paused', 'running'):
                mended.add(parent)  # it settled since the fork was refused
                return
            self._drop(parent, handle, e)  # error, deleting or deleted: it never runs again
            return
        raise refusal(e, f'a fork of the warm parent {parent}') from e

    def _drop(self, parent: str, handle: prized.Sandbox | None, e: prized.PrizedError) -> None:
        """Fork ``parent`` no more, and delete it if it is still there and the provider set it up."""
        if self._named:
            raise StartRefused(
                f'the parent {parent} cannot be forked ({e.code}): set up another with `python -m piloop_prized'
                ' parent`, and name it in `parent`'
            ) from e
        self._tracer.event('prized.parent_dropped', parent=parent, code=e.code)
        with self._lock:
            if parent in self._ready:
                self._ready.remove(parent)
        if handle is not None and handle.state not in LEAVING:
            with suppress(Exception):
                handle.delete()

    def _pick(self, deadline: float, tried: set[str], built: bool) -> tuple[str, bool]:
        """A parent to fork: a ready one whose machine had room, else the one being set up, once it is.

        ``built``: this start set up a parent already. It sets up one at most, so a parent that its fork refuses is
        never set up again within one start.
        """
        while True:
            with self._lock:
                if not self._swept:
                    self._sweep()
                now = time.monotonic()
                if (parent := self._room(now, tried)) is not None:
                    return parent, built
                if self._build is None:
                    if self._named:
                        raise StartRefused(
                            f'the machine of the parent {self._config.parent} is full: the pool tries again later.'
                            ' Fewer workers, or a second parent from `python -m piloop_prized parent`, fit more.'
                        )
                    if len(self._ready) >= self._config.max_parents:
                        raise StartRefused(
                            f'the machine of every warm parent ({len(self._ready)}) is full: the pool tries again'
                            ' later. Fewer workers, a smaller shape, or more max_parents fit more.'
                        )
                    if built:
                        raise ParentNotReady(
                            'the warm parent that this start set up is gone: the next start tries again'
                        )
                    self._sweep()  # parents that a setup left behind since, before another one
                    if (parent := self._room(now, tried)) is not None:
                        return parent, built  # one that another pool of this recipe set up meanwhile
                    self._admit(now)
                    self._build, built = self._begin(), True
                build = self._build
            if not build.done.wait(max(min(deadline - time.monotonic(), self._config.parent_wait_seconds), 0.0)):
                raise ParentNotReady(
                    f'a warm parent is being set up, {time.monotonic() - build.started:.0f} s in (at most'
                    f' setup_seconds = {self._config.setup_seconds:g}): the next start tries again'
                )
            with self._lock:
                if self._build is build:
                    self._build = None
            if build.error is not None:
                raise build.error

    def _room(self, now: float, tried: set[str]) -> str | None:
        """The oldest ready parent that no start found full lately, this one ever. Holds the lock."""
        for parent in self._ready:
            if parent not in tried and now - self._full.get(parent, -math.inf) >= FULL_SECONDS:
                return parent
        return None

    def _admit(self, now: float) -> None:
        """Count one more setup, or refuse it past ``max_parents + 2`` in ``BUILDS_HOUR``. Holds the lock."""
        while self._builds and now - self._builds[0] > BUILDS_HOUR:
            self._builds.popleft()
        if len(self._builds) >= (most := self._config.max_parents + 2):
            raise StartRefused(
                f'{most} warm parents were set up in the last hour, and none is left to fork: the pool waits before'
                ' it sets up another. The errors before this one say why.'
            )
        self._builds.append(now)

    def _sweep(self) -> None:
        """Take this recipe's paused parents, oldest first, and delete those that a setup left behind. Holds the lock.

        A parent that is not paused and older than a setup can take was left by a daemon that is gone, or by a setup
        whose delete failed: it is deleted. A younger one may be another pool's of this recipe, still being set up.
        """
        config = self._config
        stale = timedelta(seconds=config.setup_seconds + max(config.start_seconds, 600) + 600)
        now = datetime.now(UTC)
        ready = []
        for sandbox in reversed(prized.Sandbox.list(labels=self.labels)):
            created = sandbox.created_at
            if created is not None and created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if sandbox.state == 'paused':
                ready.append(sandbox.id)
            elif sandbox.state not in LEAVING and created is not None and now - created > stale:
                self._tracer.event('prized.parent_stale', parent=sandbox.id, state=sandbox.state)
                _quietly_delete(sandbox)
                continue
            sandbox.close()
        self._ready, self._swept = ready, True

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


@dataclass
class Name:
    """What the provider holds for one worker's name while a verb is on it."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    users: int = 0
    latest: Launch | None = None
    """The newest launch of the name: a start that a deadline left running writes its credential, the one that the
    server enrolled last, in place of the one that it began with."""


class PrizedProvisioning:
    """``Provisioning``: the sandboxes labelled with this server's owner."""

    def __init__(self, config: Prized, owner: str, tracer: Tracer) -> None:
        self._config, self._owner, self._tracer = config, owner, tracer
        self._piloop = config.piloop or installed_piloop()
        self._warm = Warm(config, owner, self._piloop, tracer) if config.fork else None
        self._lock = threading.Lock()
        self._names: dict[str, Name] = {}
        """The names that a verb is on: a start or a stop that a deadline left running still holds its lock, so the
        next verb on that name waits for it, and a repeated start never makes a second sandbox. Gone with its verbs."""
        self._given: dict[str, Launch] = {}
        """The launch whose credential the live sandbox of each name got, by name, until its stop."""

    @contextmanager
    def _on(self, name: str, launch: Launch | None = None) -> Iterator[Name]:
        with self._lock:
            held = self._names.setdefault(name, Name())
            held.users += 1
            if launch is not None:
                held.latest = launch
        try:
            with held.lock:
                yield held
        finally:
            with self._lock:
                held.users -= 1
                if held.users == 0:
                    del self._names[name]

    def list(self) -> Sequence[Instance]:
        return within(self._config.call_seconds, self._list, f'{type(self).__name__}.list')

    def _list(self) -> Sequence[Instance]:
        """One instance for each name: the newest that boots or runs, else the newest, as Prized lists newest first."""
        found: dict[str, Instance] = {}
        for sandbox in prized.Sandbox.list(labels={OWNER: self._owner}):
            labels = sandbox.labels
            if NAME in labels and POOL in labels:  # a warm parent has neither
                name, state = labels[NAME], _state(sandbox.state)
                held = found.get(name)
                if held is None or (held.state is InstanceState.ENDED and state is not InstanceState.ENDED):
                    found[name] = Instance(name=name, pool=labels[POOL], state=state, handle={'id': sandbox.id})
            sandbox.close()
        return list(found.values())

    def start(self, launch: Launch) -> None:
        return within(self._config.start_seconds, lambda: self._start(launch), f'{type(self).__name__}.start')

    def _start(self, launch: Launch) -> None:
        """Fork or create the sandbox, write the credential, and start the worker. One of its name that boots or runs
        with the newest credential is the start that came before.

        A sandbox whose worker did not start is deleted, and the error raised: the next start makes another one.

        Raises:
            StartRefused: Prized refused what was asked (the key, the balance, a quota, the shape), or the setup failed.
            ParentNotReady: The warm parent is still being set up.
        """
        seconds = self._config.start_seconds
        deadline = time.monotonic() + max(seconds - 5, seconds * 0.9)  # before ``within`` gives up, to say why
        name = launch.name
        with self._on(name, launch) as held:
            mine = prized.Sandbox.list(labels={OWNER: self._owner, NAME: name})
            live = [s for s in mine if _state(s.state) is not InstanceState.ENDED]
            given = self._given.get(name)
            stale = bool(live) and given is not None and given != held.latest
            for sandbox in mine:
                if stale or (sandbox not in live and sandbox.state not in LEAVING):
                    _quietly_delete(sandbox)  # a credential that the server revoked since, or a leftover that ended
                else:
                    sandbox.close()
            if live and not stale:
                return
            labels = {OWNER: self._owner, NAME: name, POOL: launch.pool}
            self._exports(launch)  # a missing secret_env is refused before any sandbox is made
            sandbox = self._warm.fork(labels, deadline) if self._warm is not None else self._fresh(labels)
            try:
                newest = held.latest or launch
                sandbox.files.write(CREDENTIAL, self._exports(newest), mode=0o600)
                if self._warm is None:
                    sandbox.files.write(SETUP, setup_script(self._config, self._piloop), mode=0o700)
                sandbox.exec(self._command(newest), background=True)
            except BaseException:
                _quietly_delete(sandbox)
                raise
            self._given[name] = newest
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
        if self._warm is None:
            return f'bash {SETUP} > {SETUP_LOG} 2>&1 || {{ rm -f {CREDENTIAL}; exit 1; }}; {worker}'
        return worker

    def stop(self, name: str) -> None:
        return within(self._config.call_seconds, lambda: self._stop(name), f'{type(self).__name__}.stop')

    def _stop(self, name: str) -> None:
        with self._on(name):
            prized.Sandbox.delete_by_labels({OWNER: self._owner, NAME: name})
            self._given.pop(name, None)


def cleanup(owner: str) -> list[str]:
    """Delete every sandbox of the piloop server ``owner``: its workers and its warm parents. The ids deleted."""
    return prized.Sandbox.delete_by_labels({OWNER: owner})
