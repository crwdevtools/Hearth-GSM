"""Stand-in for a sleeping Minecraft: Java server.

Listens on the server's port. The multiplayer list shows it as sleeping; trying to join shows
"waking up" and asks the agent to start the real server. Runs as its own small container:
    python -m hearth_agent.sleeper   (env: LISTEN_PORT, WAKE_URL, MOTD)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
import urllib.request

log = logging.getLogger("hearth.sleeper")

WAKING = "The server is waking up. Rejoin in about a minute!"


def encode_varint(value: int) -> bytes:
    value &= 0xFFFFFFFF
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


async def read_varint(reader: asyncio.StreamReader) -> int:
    result = 0
    for shift in range(0, 35, 7):
        byte = (await reader.readexactly(1))[0]
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result - (1 << 32) if result & (1 << 31) else result
    raise ValueError("VarInt too long")


def decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    for shift in range(0, 35, 7):
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
    raise ValueError("VarInt too long")


def packet(packet_id: int, payload: bytes) -> bytes:
    body = encode_varint(packet_id) + payload
    return encode_varint(len(body)) + body


def mc_string(text: str) -> bytes:
    raw = text.encode("utf-8")
    return encode_varint(len(raw)) + raw


async def read_packet(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    length = await read_varint(reader)
    if not 0 < length < 32768:
        raise ValueError("bad packet length")
    data = await reader.readexactly(length)
    packet_id, pos = decode_varint(data, 0)
    return packet_id, data[pos:]


class Sleeper:
    def __init__(self, motd: str, wake_url: str | None) -> None:
        self.motd = motd
        self.wake_url = wake_url
        self.woken = asyncio.Event()

    def status_json(self, protocol: int) -> str:
        return json.dumps({
            "version": {"name": "Sleeping", "protocol": protocol},
            "players": {"max": 0, "online": 0},
            "description": {"text": self.motd},
        })

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            first = await asyncio.wait_for(reader.read(1), 10)
            if not first or first == b"\xfe":  # legacy (pre-1.7) ping: just hang up
                return
            length = first[0] & 0x7F
            if first[0] & 0x80:  # multi-byte length: finish reading the VarInt
                shift, byte = 7, first[0]
                while byte & 0x80:
                    byte = (await reader.readexactly(1))[0]
                    length |= (byte & 0x7F) << shift
                    shift += 7
            data = await asyncio.wait_for(reader.readexactly(length), 10)
            packet_id, pos = decode_varint(data, 0)
            if packet_id != 0x00:
                return
            protocol, pos = decode_varint(data, pos)
            addr_len, pos = decode_varint(data, pos)
            pos += addr_len + 2  # server address + port
            next_state, _ = decode_varint(data, pos)
            if next_state == 1:
                await self._status(reader, writer, protocol)
            elif next_state in (2, 3):
                writer.write(packet(0x00, mc_string(json.dumps({"text": WAKING, "color": "yellow"}))))
                await writer.drain()
                await self.wake()
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ValueError, ConnectionError, IndexError):
            pass
        finally:
            writer.close()

    async def _status(self, reader, writer, protocol: int) -> None:
        packet_id, _ = await asyncio.wait_for(read_packet(reader), 10)
        if packet_id != 0x00:
            return
        writer.write(packet(0x00, mc_string(self.status_json(protocol))))
        await writer.drain()
        try:
            packet_id, payload = await asyncio.wait_for(read_packet(reader), 5)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            return
        if packet_id == 0x01 and len(payload) == 8:
            writer.write(packet(0x01, payload))
            await writer.drain()

    async def wake(self) -> None:
        if self.woken.is_set():
            return
        self.woken.set()
        if not self.wake_url:
            return
        log.info("player joined; waking the server")

        def call() -> None:
            req = urllib.request.Request(self.wake_url, method="POST", data=b"")
            context = None
            if self.wake_url.startswith("https://"):
                # The agent's certificate names its public host, not the Docker-network IP used
                # here. The one-off key in the URL is what authenticates this call.
                context = ssl.create_default_context()
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            urllib.request.urlopen(req, timeout=10, context=context).read()  # noqa: S310 (internal agent URL)

        try:
            await asyncio.to_thread(call)
        except Exception as exc:  # the agent replaces this container; a failure is retried on next join
            log.warning("wake request failed: %s", exc)
            self.woken.clear()


async def serve(port: int, sleeper: Sleeper, host: str = "0.0.0.0") -> asyncio.base_events.Server:
    return await asyncio.start_server(sleeper.handle, host, port)


def main() -> None:
    logging.basicConfig(level="INFO")
    sleeper = Sleeper(os.environ.get("MOTD", "Sleeping - join to wake me up"), os.environ.get("WAKE_URL"))

    async def run() -> None:
        server = await serve(int(os.environ.get("LISTEN_PORT", "25565")), sleeper)
        async with server:
            await server.serve_forever()

    asyncio.run(run())


if __name__ == "__main__":
    main()

