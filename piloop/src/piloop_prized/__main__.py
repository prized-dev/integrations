"""``python -m piloop_prized``: set up a warm parent ahead of the daemon, or delete a server's sandboxes.

    python -m piloop_prized parent compute.toml    # a paused parent from a compute table: prints its id
    python -m piloop_prized cleanup --owner <id>    # every sandbox of that piloop server, parents too
    python -m piloop_prized cleanup --home ~/.piloop

``parent`` sets the parent up before the daemon starts, so that the first workers start at once, however long the
setup takes: name the id it prints in the table's ``parent``, and drop the table's shape and setup keys there (the
parent fixes them). It is kept until you delete it (``prized sandbox rm <id>``), unless the table sets ``parent_hours``.
"""

import argparse
import sys
import tomllib
from pathlib import Path

from piloop_prized.prized import PARENT, RECIPE, ROLE, Prized, build_parent, cleanup, installed_piloop, recipe

PREBUILT = 'piloop-prebuilt'
"""The label of a parent that ``parent`` set up: no server owns it, and no server's cleanup deletes it."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='python -m piloop_prized', description=__doc__.split('\n', 1)[0])
    verbs = parser.add_subparsers(dest='verb', required=True)
    made = verbs.add_parser('parent', help='set up a warm parent from a compute table, pause it, print its id')
    made.add_argument('table', type=Path, help='a TOML file of the keys of the compute table (provider optional)')
    gone = verbs.add_parser('cleanup', help='delete every sandbox of a piloop server: its workers and warm parents')
    whose = gone.add_mutually_exclusive_group(required=True)
    whose.add_argument('--owner', help="the server's id: the file `owner` in its PILOOP_HOME")
    whose.add_argument('--home', type=Path, help="the server's PILOOP_HOME, whose file `owner` holds its id")
    args = parser.parse_args(argv)
    if args.verb == 'parent':
        keys = {k: v for k, v in tomllib.loads(args.table.read_text()).items() if k != 'provider'}
        keys.setdefault('parent_hours', 0)  # a parent that a table names is kept until it is deleted
        config = Prized.model_validate(keys)
        if config.parent is not None or not config.fork:
            parser.error('the table sets up a parent: it names no parent, and keeps fork = true')
        piloop = config.piloop or installed_piloop()
        labels = {ROLE: PARENT, RECIPE: recipe(config, piloop), PREBUILT: 'true'}
        print(build_parent(config, labels, piloop))
        return 0
    owner = args.owner or (args.home / 'owner').read_text().strip()
    deleted = cleanup(owner)
    print(f'deleted {len(deleted)} sandbox(es) of the piloop server {owner}', file=sys.stderr)
    for sandbox in deleted:
        print(sandbox)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
