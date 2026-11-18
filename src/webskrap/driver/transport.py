"""Message transports for Chrome DevTools Protocol."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import os
import struct
from typing import Protocol

# Pipe messages can be large: a full-page screenshot arrives as one base64
# string. The reader buffers up to this many bytes for a single message.
MAX_MESSAGE_BYTES = 256 * 1024 * 1024


class Transport(Protocol):
    """Carries whole CDP messages; the pipe and a WebSocket both qualify."""

    async def read_message(self) -> bytes:
        """Return the next message; raise ``ConnectionError`` once closed."""
        ...

    def write_message(self, data: bytes) -> None:
        """Queue one message for sending."""
        ...

    async def close(self) -> None:
        """Release the transport. Idempotent."""
        ...


class PipeTransport:
    """Chromium's debugging pipe: NUL-terminated JSON in each direction."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.WriteTransport,
        read_transport: asyncio.BaseTransport | None = None,
    ) -> None:
        """Wrap the reader and writer :func:`launch` connected to fds 3 and 4."""
        self._reader = reader
        self._writer = writer
        self._read_transport = read_transport

    async def read_message(self) -> bytes:
        """Return the next message without its NUL terminator."""
        try:
            raw = await self._reader.readuntil(b"\0")
        except asyncio.IncompleteReadError as exc:
            raise ConnectionError("the browser closed the CDP pipe") from exc
        return raw[:-1]

    def write_message(self, data: bytes) -> None:
        """Write ``data`` followed by the NUL terminator."""
        self._writer.write(data + b"\0")

    async def close(self) -> None:
        """Close both pipe ends."""
        self._writer.close()
        if self._read_transport is not None:
            self._read_transport.close()


#: Fixed by RFC 6455 for the Sec-WebSocket-Accept proof.
_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_OPCODE_TEXT, _OPCODE_CLOSE, _OPCODE_PING, _OPCODE_PONG = 0x1, 0x8, 0x9, 0xA


def _mask(payload: bytes, key: bytes) -> bytes:
    """XOR ``payload`` with the 4-byte ``key``, as RFC 6455 masking requires."""
    if not payload:
        return payload
    repeated = (key * (len(payload) // 4 + 1))[: len(payload)]
    value = int.from_bytes(payload, "big") ^ int.from_bytes(repeated, "big")
    return value.to_bytes(len(payload), "big")


class WebSocketTransport:
    """A minimal RFC 6455 client for a loopback DevTools endpoint.

    Persistent sessions need it: each CLI command is a new process, and only
    the browser's debugging port can be reattached, not its pipe. Text frames
    carry the JSON messages; fragments are joined, pings answered, and a
    close frame or EOF ends the stream. No ``Origin`` header is sent, which
    Chromium accepts without ``--remote-allow-origins``.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Wrap a stream that has completed the upgrade; see :meth:`connect`."""
        self._reader = reader
        self._writer = writer

    @classmethod
    async def connect(
        cls, host: str, port: int, path: str, *, timeout_s: float = 10.0
    ) -> WebSocketTransport:
        """Open ``ws://host:port/path`` and verify the server's handshake.

        Raises:
            ConnectionError: If the server refuses the upgrade or answers with
                a wrong ``Sec-WebSocket-Accept``.
            OSError: If nothing listens on the port.
        """
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, limit=MAX_MESSAGE_BYTES), timeout_s
        )
        key = base64.b64encode(os.urandom(16)).decode()
        writer.write(
            (
                f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout_s)
            lines = head.decode("latin-1").split("\r\n")
            headers = {
                name.strip().lower(): value.strip()
                for name, _, value in (line.partition(":") for line in lines[1:] if line)
            }
            # SHA-1 is what RFC 6455 specifies for this proof; it is not a
            # security boundary here.
            digest = hashlib.sha1((key + _WEBSOCKET_GUID).encode(), usedforsecurity=False)
            expected = base64.b64encode(digest.digest()).decode()
            if not lines[0].startswith("HTTP/1.1 101"):
                raise ConnectionError(f"DevTools refused the WebSocket upgrade: {lines[0]}")
            if headers.get("sec-websocket-accept") != expected:
                raise ConnectionError("DevTools answered the upgrade with a wrong accept key")
        except BaseException:
            writer.close()
            raise
        return cls(reader, writer)

    async def read_message(self) -> bytes:
        """Return the next complete message, answering pings on the way."""
        parts: list[bytes] = []
        size = 0
        while True:
            try:
                first, second = await self._reader.readexactly(2)
                length = second & 0x7F
                if length == 126:
                    (length,) = struct.unpack(">H", await self._reader.readexactly(2))
                elif length == 127:
                    (length,) = struct.unpack(">Q", await self._reader.readexactly(8))
                key = await self._reader.readexactly(4) if second & 0x80 else b""
                if size + length > MAX_MESSAGE_BYTES:
                    raise ConnectionError("DevTools sent a message over the size limit")
                payload = await self._reader.readexactly(length)
            except asyncio.IncompleteReadError as exc:
                raise ConnectionError("DevTools closed the WebSocket") from exc
            if key:
                payload = _mask(payload, key)
            opcode = first & 0x0F
            if opcode == _OPCODE_CLOSE:
                raise ConnectionError("DevTools closed the WebSocket")
            if opcode == _OPCODE_PING:
                self._send_frame(_OPCODE_PONG, payload)
                continue
            if opcode == _OPCODE_PONG:
                continue
            parts.append(payload)
            size += length
            if first & 0x80:
                return b"".join(parts)

    def write_message(self, data: bytes) -> None:
        """Send ``data`` as one text frame."""
        self._send_frame(_OPCODE_TEXT, data)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        # Clients must mask every frame (RFC 6455 section 5.3).
        key = os.urandom(4)
        length = len(payload)
        if length < 126:
            header = struct.pack(">BB", 0x80 | opcode, 0x80 | length)
        elif length < 1 << 16:
            header = struct.pack(">BBH", 0x80 | opcode, 0x80 | 126, length)
        else:
            header = struct.pack(">BBQ", 0x80 | opcode, 0x80 | 127, length)
        self._writer.write(header + key + _mask(payload, key))

    async def close(self) -> None:
        """Send a close frame and close the socket. Idempotent."""
        if self._writer.is_closing():
            return
        with contextlib.suppress(ConnectionError, RuntimeError):
            self._send_frame(_OPCODE_CLOSE, b"")
        self._writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await self._writer.wait_closed()
