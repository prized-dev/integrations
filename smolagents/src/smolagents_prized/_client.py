# Copyright 2026 Prized
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Hand one code cell to the kernel and print its JSON reply.

The executor runs this inside the sandbox, once per step: ``python3 _client.py <request-file>``. The
cell's code is read from the request file (uploaded by the executor, so there is no command-line
length limit and no quoting). The reply is written verbatim to stdout for the executor to parse.

No third-party dependencies: the sandbox's own Python runs it.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import sys


def _recv_all(conn: socket.socket, n: int) -> bytes:
    chunks = []
    got = 0
    while got < n:
        chunk = conn.recv(n - got)
        if not chunk:
            raise EOFError("kernel closed the connection mid-message")
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def main() -> int:
    sock_path = os.environ["SMOL_KERNEL_SOCK"]
    with open(sys.argv[1], encoding="utf-8") as handle:
        code = handle.read()

    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.connect(sock_path)
    try:
        payload = json.dumps({"op": "run", "code": code}).encode("utf-8")
        conn.sendall(struct.pack(">I", len(payload)) + payload)
        (length,) = struct.unpack(">I", _recv_all(conn, 4))
        sys.stdout.write(_recv_all(conn, length).decode("utf-8"))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
