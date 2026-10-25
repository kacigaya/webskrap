"""Answer proxy authentication for a persistent session's browser.

Session actions attach over CDP and disconnect, so nobody would be there
when the proxy asks for credentials. :func:`webskrap.browser_session.open_session`
starts this module as a detached process instead::

    python -m webskrap.proxy_auth <port> <user-data-dir>

It reads ``{"username", "password"}`` from stdin, so the credentials never
reach a command line, an environment or a file. It enables ``Fetch`` on the
browser target, which covers every page, frame and worker, prints ``ready``,
and exits when the browser closes its DevTools socket.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from webskrap import cdp_setup
from webskrap.cdp import CDPConnection, WebSocketTransport
from webskrap.cdp_session import devtools_path

READY = "ready"


async def serve(port: int, user_data_dir: Path, credentials: tuple[str, str]) -> None:
    """Answer proxy challenges until the browser closes the connection."""
    path = devtools_path(user_data_dir, port)
    connection = CDPConnection(await WebSocketTransport.connect("127.0.0.1", port, path))
    connection.start()
    await cdp_setup.intercept(connection, None, credentials=credentials)
    print(READY, flush=True)
    sys.stdout.close()
    await connection.wait_closed()


def main(argv: list[str]) -> int:
    """Run the helper; see the module docstring for the protocol."""
    port, user_data_dir = int(argv[0]), Path(argv[1])
    login = json.loads(sys.stdin.readline())
    sys.stdin.close()
    asyncio.run(serve(port, user_data_dir, (login["username"], login["password"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
