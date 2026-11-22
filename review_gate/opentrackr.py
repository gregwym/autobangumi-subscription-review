"""BEP 15 read-only swarm statistics for public hashes."""

import asyncio
import secrets
import socket
import struct

HOST = "tracker.opentrackr.org"
PORT = 1337
BATCH = 60  # Fits in one 1280-byte IPv6 packet.


def parse_response(packet, action, transaction, length):
    if len(packet) < 8 + length or struct.unpack_from("!II", packet) != (action, transaction):
        raise ValueError("invalid tracker response")
    return packet[8 : 8 + length]


async def scrape(hashes):
    if not 1 <= len(hashes) <= BATCH:
        raise ValueError("invalid scrape size")
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(5):
        addrs = await loop.getaddrinfo(HOST, PORT, type=socket.SOCK_DGRAM)
        # Prefer IPv4 when both are available (container networks often lack IPv6).
        family, socktype, proto, _, address = min(addrs, key=lambda a: a[0] != socket.AF_INET)
        with socket.socket(family, socktype, proto) as sock:
            sock.setblocking(False)
            await loop.sock_connect(sock, address)

            async def request(connection, action, body, length):
                tx = secrets.randbits(32)
                await loop.sock_sendall(sock, struct.pack("!QII", connection, action, tx) + body)
                return parse_response(await loop.sock_recv(sock, 2048), action, tx, length)

            response = await request(0x41727101980, 0, b"", 8)
            connection = struct.unpack("!Q", response)[0]
            body = b"".join(bytes.fromhex(h) for h in hashes)
            response = await request(connection, 2, body, 12 * len(hashes))
            return {
                h: {"seeds": seeds, "leechers": leechers}
                for h, (seeds, _, leechers) in zip(
                    hashes, struct.iter_unpack("!III", response), strict=True
                )
            }
