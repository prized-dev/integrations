"""Add PrizedSandbox to a pydantic/pydantic-ai checkout: copy files/ in and make the shared edits.

    python3 apply.py /path/to/pydantic-ai

Each shared edit goes in right after the matching boxd line, which is where the boxd PR (#11367) put its
own, so the result reads like the other sandboxes. It stops, changing nothing more, at the first anchor
it can't find. Run `uv lock` afterwards (see README.md).
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# (file, line that must be present, lines to insert after it)
AFTER: list[tuple[str, str, list[str]]] = [
    (
        'src/pydantic_ai_harness/pyproject.toml',
        'boxd = ["boxd>=0.2.20,<0.3"]',
        [
            "# Prized's SDK is on 0.x, where a minor release can change the API, so cap per minor. 0.1.2 is the",
            '# first with the retry and timeout behavior the backend relies on.',
            'prized = ["prized>=0.1.2,<0.2"]',
        ],
    ),
    (
        'pyproject.toml',
        'boxd = "2026-10-27T23:56:00Z"',
        [
            '# prized 0.1.2 is the `prized` floor.',
            '# TODO: Remove after 2026-11-09 02:18 UTC, then run `uv lock` and',
            '# `uv lock --script .github/scripts/pydantic-ai-runner`.',
            'prized = "2026-11-02T02:18:00Z"',
        ],
    ),
    (
        'pyproject.toml',
        '"boxd_live: runs against a real boxd control plane; needs credentials",',
        ['    "prized_live: runs against a real Prized sandbox; needs credentials",'],
    ),
    # The import block, `__all__` and `_LAZY_EXPORTS` are sorted: Prized goes after PostHog.
    (
        'src/pydantic_ai_harness/pydantic_ai_harness/__init__.py',
        '    from .posthog import PostHog',
        ['    from .prized_sandbox import PrizedSandbox, PrizedSandboxBackend'],
    ),
    (
        'src/pydantic_ai_harness/pydantic_ai_harness/__init__.py',
        "    'PostHog',",
        ["    'PrizedSandbox',", "    'PrizedSandboxBackend',"],
    ),
    (
        'src/pydantic_ai_harness/pydantic_ai_harness/__init__.py',
        "    'PostHog': 'posthog',",
        ["    'PrizedSandbox': 'prized_sandbox',", "    'PrizedSandboxBackend': 'prized_sandbox',"],
    ),
    (
        'src/pydantic_ai_harness/pydantic_ai_harness/_workspace.py',
        "    'boxd': 'BoxdSandbox()',",
        ["    'prized': 'PrizedSandbox()',"],
    ),
    (
        'tests/harness/test_capability_combine.py',
        "    'BoxdSandbox': Anonymous(",
        [
            "    'PrizedSandbox': Anonymous('two coexist; the first supplies the run workspace, as core picks the first supplier'),"
        ],
    ),
    (
        'tests/harness/test_docs_parity.py',
        "    'boxd-sandbox.md': ('boxd_sandbox', 'boxd Sandbox'),",
        ["    'prized-sandbox.md': ('prized_sandbox', 'Prized Sandbox'),"],
    ),
    (
        'tests/harness/test_history_append_only.py',
        "    'BoxdSandbox': 'boxd',",
        ["    'PrizedSandbox': 'prized',"],
    ),
    (
        'docs/navigation.yml',
        '            slug: "harness/boxd-sandbox"',
        [
            '          - page: "Prized Sandbox"',
            '            path: "harness/prized-sandbox.md"',
            '            slug: "harness/prized-sandbox"',
        ],
    ),
    (
        'docs/harness/index.md',
        '| [boxd Sandbox](boxd-sandbox.md) |',
        [
            '| [Prized Sandbox](prized-sandbox.md) | Harness | Commands and files in a [Prized](https://prized.dev) '
            'Firecracker microVM that pauses with its memory and forks live |'
        ],
    ),
    (
        'docs/capabilities/overview.md',
        '| [boxd Sandbox](https://pydantic.dev/docs/ai/harness/boxd-sandbox/) |',
        [
            '| [Prized Sandbox](https://pydantic.dev/docs/ai/harness/prized-sandbox/) | Harness | Commands and files '
            'in a [Prized](https://prized.dev) Firecracker microVM that pauses with its memory and forks live |'
        ],
    ),
    (
        'src/pydantic_ai_harness/README.md',
        '| [boxd Sandbox](pydantic_ai_harness/boxd_sandbox/) |',
        [
            '| [Prized Sandbox](pydantic_ai_harness/prized_sandbox/) | Harness | Commands and files in a '
            '[Prized](https://prized.dev) Firecracker microVM that pauses with its memory and forks live |'
        ],
    ),
]

# (file, old, new): in-line list mentions. These are prose, so one whose text has moved on is reported
# and skipped instead of stopping the script.
_OR_BOXD = ('(Modal, E2B, Render, Sprites, Daytona, or boxd)', '(Modal, E2B, Render, Sprites, Daytona, boxd, or Prized)')
_SKILL = 'src/pydantic_ai_harness/pydantic_ai_harness/.agents/skills/pydantic-ai-harness/'
REPLACE: list[tuple[str, str, str]] = [
    ('docs/workspace.md', 'Sprites, Daytona and boxd, a local', 'Sprites, Daytona, boxd and Prized, a local'),
    ('docs/harness/coder.md', *_OR_BOXD),
    ('docs/harness/index.md', *_OR_BOXD),
    ('src/pydantic_ai_harness/README.md', *_OR_BOXD),
    ('src/pydantic_ai_harness/pydantic_ai_harness/coder/README.md', *_OR_BOXD),
    (
        'THREAT_MODEL.md',
        '[boxd](docs/harness/boxd-sandbox.md#security-model), ',
        '[boxd](docs/harness/boxd-sandbox.md#security-model), [Prized](docs/harness/prized-sandbox.md#security-model), ',
    ),
    (_SKILL + 'SKILL.md', 'Daytona, boxd, smolvm', 'Daytona, boxd, Prized, smolvm'),
    (_SKILL + 'SKILL.md', 'Daytona/boxd/smolvm', 'Daytona/boxd/Prized/smolvm'),
    (_SKILL + 'SKILL.md', '`BoxdSandbox`, `SmolSandbox`', '`BoxdSandbox`, `PrizedSandbox`, `SmolSandbox`'),
    (
        _SKILL + 'SKILL.md',
        '`BoxdSandbox` (`.boxd_sandbox`, `[boxd]`); ',
        '`BoxdSandbox` (`.boxd_sandbox`, `[boxd]`); `PrizedSandbox` (`.prized_sandbox`, `[prized]`); ',
    ),
    (_SKILL + 'SKILL.md', '`[boxd]`, `[smol]`', '`[boxd]`, `[prized]`, `[smol]`'),
    (_SKILL + 'references/CODING-AND-WORKSPACES.md', '`BoxdSandbox()`; tool', '`BoxdSandbox()` / `PrizedSandbox()`; tool'),
    (_SKILL + 'references/CODING-AND-WORKSPACES.md', 'Daytona/boxd), use', 'Daytona/boxd/Prized), use'),
]
REQUIRED_REPLACE = {'docs/workspace.md'}


def main(root: Path) -> None:
    edits: dict[Path, str] = {}

    def text(rel: str) -> str:
        path = root / rel
        if path not in edits:
            edits[path] = path.read_text()
        return edits[path]

    for rel, anchor, lines in AFTER:
        content = text(rel)
        if lines[-1] in content:
            continue  # already applied
        matches = [i for i, line in enumerate(content.splitlines(keepends=True)) if line.lstrip().startswith(anchor.lstrip())]
        if len(matches) != 1:
            raise SystemExit(f'{rel}: expected one line starting {anchor!r}, found {len(matches)}')
        parts = content.splitlines(keepends=True)
        parts[matches[0] + 1 : matches[0] + 1] = [line + '\n' for line in lines]
        edits[root / rel] = ''.join(parts)
    for rel, old, new in REPLACE:
        content = text(rel)
        if new in content:
            continue
        if content.count(old) != 1:
            if rel in REQUIRED_REPLACE:
                raise SystemExit(f'{rel}: expected {old!r} once')
            print(f'skipped (text moved on, edit by hand): {rel}: {old!r}')
            continue
        edits[root / rel] = content.replace(old, new)
    for path, content in edits.items():
        path.write_text(content)
    shutil.copytree(HERE / 'files', root, dirs_exist_ok=True)
    print(f'applied: {len(edits)} files edited, files/ copied into {root}')


if __name__ == '__main__':
    main(Path(sys.argv[1]).resolve())
