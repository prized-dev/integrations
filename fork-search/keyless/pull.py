"""pull.py REMOTE LOCAL: copy one file from the keyless runner to this box."""
import asyncio
import pathlib
import sys

import broker


async def main() -> None:
    runner = await broker._runner()
    pathlib.Path(sys.argv[2]).write_bytes(await runner.files.read_bytes(sys.argv[1]))


asyncio.run(main())
