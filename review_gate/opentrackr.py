"""Read public swarm statistics over HTTP or BEP 15 UDP."""

import asyncio
import secrets
import socket
import struct
from urllib.parse import quote_from_bytes

from .bencode import decode_dict

HOST = "tracker.opentrackr.org"
PORT = 1337
BATCH = 60  # Fits in one 1280-byte IPv6 packet.


async def scrape_http(hashes):
    from module.network.request_url import get_shared_client

    if not 1 <= len(hashes) <= BATCH:
        raise ValueError("invalid scrape size")
    query = "&".join("info_hash=" + quote_from_bytes(bytes.fromhex(h), safe="") for h in hashes)
    url = f"http://{HOST}:{PORT}/scrape?{query}"
    client = await get_shared_client()
    data = bytearray()
    async with asyncio.timeout(4), client.stream("GET", url, follow_redirects=False) as response:
        response.raise_for_status()
        async for chunk in response.aiter_bytes(chunk_size=8192):
            if len(data) + len(chunk) > 32768:
                raise ValueError("tracker response too large")
            data.extend(chunk)
    files = decode_dict(bytes(data)).get(b"files")
    if not isinstance(files, dict):
        raise ValueError("missing tracker statistics")
    results = {}
    for h in hashes:
        row = files.get(bytes.fromhex(h))
        if not isinstance(row, dict):
            continue
        seeds, peers = row.get(b"complete"), row.get(b"incomplete")
        if type(seeds) is int and 0 <= seeds <= 0xFFFFFFFF:
            results[h] = {
                "seeds": seeds,
                "leechers": peers if type(peers) is int and 0 <= peers <= 0xFFFFFFFF else None,
            }
    return results


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
