"""A stand-in for the Prized SDK's ``Sandbox``: sandboxes as rows of a ``FakePrized``, which a test reads and breaks.

It answers as Prized does where a provider could go wrong:

- each call of the SDK returns a handle of its own, and a handle that was closed refuses every call;
- a deleted sandbox is ``deleting``, and still listed, until the list after next; a command, a file, a pause, a resume
  or a fork sent to it is refused with ``sandbox_deleted`` (410), and ``connect`` finds it until it is gone (404);
- a fork of a parent that is neither running nor paused is refused with ``parent_not_running`` (409), and one past what
  its machine holds with ``fork_capacity`` (409).
"""

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import count
from typing import Any

import prized

IDS = count(1)
GONE = ('deleting', 'deleted')


@dataclass
class Box:
    """One sandbox as Prized holds it."""

    id: str
    template: str
    labels: dict[str, str]
    options: dict[str, Any]
    state: str = 'running'
    parent: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    files: dict[str, tuple[str | bytes, int | None]] = field(default_factory=dict)
    commands: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    listed_deleting: bool = False
    """Listed once already while ``deleting``: the next list finds it gone."""

    @property
    def gone(self) -> bool:
        return self.state in GONE


class Handle:
    """What the SDK hands back: a view of one ``Box``."""

    def __init__(self, api: FakePrized, box: Box) -> None:
        self._api, self._box, self.closed = api, box, False
        self.files = Files(self)

    def _live(self) -> Box:
        if self.closed:
            raise prized.HandleClosedError(f'the handle of {self._box.id} is closed', sandbox_id=self._box.id)
        return self._box

    def _running(self) -> Box:
        """The box, for a call that needs it there: a deleted one answers 410."""
        box = self._live()
        self._api.answer()
        if box.gone:
            raise prized.PrizedError('sandbox_deleted', status=410, sandbox_id=box.id)
        return box

    @property
    def id(self) -> str:
        return self._box.id

    @property
    def state(self) -> str:
        return self._box.state

    @property
    def labels(self) -> dict[str, str]:
        return dict(self._box.labels)

    @property
    def created_at(self) -> datetime:
        return self._box.created_at

    def refresh(self) -> Handle:
        self._live()
        self._api.answer()
        return self

    def exec(self, cmd: str, *, timeout: float | None = None, background: bool = False) -> Any:
        box = self._running()
        box.commands.append((cmd, {'timeout': timeout, 'background': background}))
        if background:
            if self._api.start_fails is not None:
                raise self._api.start_fails
            return object()
        if self._api.setup_gate is not None:
            self._api.setup_gate.wait()
        if box.gone:  # deleted while its setup ran
            raise prized.PrizedError('sandbox_deleted', status=410, sandbox_id=box.id)
        code, out = self._api.setup_result
        if self._api.setup_times_out:
            result = prized.ExecResult(stdout=out, stderr='', exit_code=-1, timed_out=True)
            raise prized.CommandTimeoutError(result, timeout, box.id)
        self._api.setups.append(box.id)
        return prized.ExecResult(stdout=out, stderr='', exit_code=code)

    def pause(self, **_: Any) -> None:
        self._running().state = 'paused'
        self._api.calls.append(('pause', self.id))

    def resume(self, **_: Any) -> None:
        self._running().state = 'running'
        self._api.calls.append(('resume', self.id))

    def fork(self, count: int = 1, *, labels: Mapping[str, str] | None = None, timeout: float = 180) -> list[Handle]:
        box = self._running()
        self._api.calls.append(('fork', box.id))
        if self._api.fork_gate is not None:
            self._api.fork_gate.wait()
        if (error := self._api.fork_errors.pop(box.id, None)) is not None:
            raise error
        if self._api.fork_always is not None:
            raise self._api.fork_always
        if box.state not in ('running', 'paused'):
            raise prized.PrizedError('parent_not_running', status=409, sandbox_id=box.id, body={'state': box.state})
        fits = self._api.fits.get(box.id)
        if fits is not None:
            if fits < count:
                raise prized.PrizedError('fork_capacity', status=409, sandbox_id=box.id, body={'fits': fits})
            self._api.fits[box.id] = fits - count
        copies = []
        for _ in range(count):
            child = self._api.make(box.template, dict(labels) if labels is not None else dict(box.labels), box.options)
            child.parent, child.files = box.id, dict(box.files)
            copies.append(Handle(self._api, child))
        return copies

    def delete(self, **_: Any) -> None:
        box = self._live()
        self._api.answer()
        if not box.gone:
            box.state = 'deleting'
            self._api.calls.append(('delete', self.id))

    def close(self) -> None:
        self.closed = True


class Files:
    def __init__(self, handle: Handle) -> None:
        self._handle = handle

    def write(self, path: str, data: str | bytes, mode: int | None = None) -> None:
        box = self._handle._running()
        if self._handle._api.write_fails is not None:
            raise self._handle._api.write_fails
        box.files[path] = (data, mode)


@dataclass
class FakePrized:
    """The workspace: its sandboxes, every call, and what the next calls do."""

    boxes: dict[str, Box] = field(default_factory=dict)
    calls: list[tuple[str, Any]] = field(default_factory=list)
    setups: list[str] = field(default_factory=list)
    """Each sandbox whose setup ran to its end."""
    setup_result: tuple[int, str] = (0, 'piloop 0.5.0\n')
    setup_gate: threading.Event | None = None
    setup_times_out: bool = False
    fork_gate: threading.Event | None = None
    fork_errors: dict[str, Exception] = field(default_factory=dict)
    """What the next fork of each parent raises, once."""
    fork_always: Exception | None = None
    """What every fork raises."""
    fits: dict[str, int] = field(default_factory=dict)
    """How many more copies the machine of each parent holds. A parent not in it holds any number."""
    create_fails: Exception | None = None
    write_fails: Exception | None = None
    start_fails: Exception | None = None
    down: bool = False
    list_gate: threading.Event | None = None

    def answer(self) -> None:
        if self.down:
            raise prized.PrizedError('network_error', 'Prized could not be reached')

    def make(self, template: str, labels: dict[str, str], options: dict[str, Any]) -> Box:
        box = Box(id=f'sbx_{next(IDS):04d}', template=template, labels=labels, options=dict(options))
        self.boxes[box.id] = box
        return box

    def create(self, template: str = 'prized-base-v1', **options: Any) -> Handle:
        self.answer()
        self.calls.append(('create', template))
        if self.create_fails is not None:
            raise self.create_fails
        labels = dict(options.pop('labels', None) or {})
        return Handle(self, self.make(template, labels, options))

    def list(self, *, labels: Mapping[str, str] | None = None, state: Any = None) -> list[Handle]:
        if self.list_gate is not None:
            self.list_gate.wait()
        self.answer()
        self.calls.append(('list', dict(labels or {})))
        for box in self.boxes.values():  # listed once while deleting: gone now
            if box.state == 'deleting' and box.listed_deleting:
                box.state = 'deleted'
        wanted = (labels or {}).items()
        found = [b for b in self.boxes.values() if b.state != 'deleted' and wanted <= b.labels.items()]
        for box in found:
            box.listed_deleting = box.state == 'deleting'
        return [Handle(self, b) for b in reversed(found)]  # newest first, as Prized lists them

    def connect(self, sandbox_id: str) -> Handle:
        self.answer()
        box = self.boxes.get(sandbox_id)
        if box is None or box.state == 'deleted':
            raise prized.PrizedError('not_found', status=404, sandbox_id=sandbox_id)
        return Handle(self, box)

    def delete_by_labels(self, labels: Mapping[str, str]) -> list[str]:
        if self.list_gate is not None:
            self.list_gate.wait()
        self.answer()
        assert labels, 'delete_by_labels needs at least one label'
        self.calls.append(('delete_by_labels', dict(labels)))
        doomed = [b for b in self.boxes.values() if not b.gone and labels.items() <= b.labels.items()]
        for box in doomed:
            box.state = 'deleting'
        return [b.id for b in doomed]

    def by_labels(self, labels: Mapping[str, str]) -> list[Box]:
        return [b for b in self.boxes.values() if labels.items() <= b.labels.items()]
