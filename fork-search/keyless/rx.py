"""rx.py 'CMD' [timeout]: run a shell command in the keyless runner and print its output."""
import asyncio
import sys

import broker


async def main() -> None:
    runner = await broker._runner()
    r = await runner.exec(sys.argv[1], timeout=float(sys.argv[2]) if len(sys.argv) > 2 else 600, full_output=True)
    print(r.stdout[-12000:])
    if r.stderr:
        print("--stderr--", r.stderr[-4000:])
    print("exit", r.exit_code)


asyncio.run(main())
