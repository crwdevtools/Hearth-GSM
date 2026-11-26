"""Minecraft Java server-list ping: player count without a console command (so no log lines)."""

from __future__ import annotations

import asyncio
import json
import struct


def _varint(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


async def _read_varint(reader: asyncio.StreamReader) -> int:
    n = 0
    for i in range(5):
        b = (await reader.readexactly(1))[0]
        n |= (b & 0x7F) << (7 * i)
        if not b & 0x80:
            return n
    raise ValueError("varint too long")


def _packet(body: bytes) -> bytes:
    return _varint(len(body)) + body


async def ping(host: str, port: int, timeout: float = 3.0) -> tuple[int, int] | None:
    """(online, max) from the server's status reply, or None when it can't be reached or read."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        addr = host.encode()
        writer.write(_packet(b"\x00" + _varint(767) + _varint(len(addr)) + addr
                             + struct.pack(">H", port) + _varint(1)))
        writer.write(_packet(b"\x00"))
        await writer.drain()

        async def read() -> tuple[int, int] | None:
            await _read_varint(reader)  # packet length
            if await _read_varint(reader) != 0:
                return None
            size = await _read_varint(reader)
            if size > 1_000_000:
                return None
            data = json.loads((await reader.readexactly(size)).decode("utf-8", "replace"))
            players = data.get("players") or {}
            return int(players["online"]), int(players["max"])

        return await asyncio.wait_for(read(), timeout)
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError, KeyError, TypeError):
        return None
    finally:
        writer.close()
