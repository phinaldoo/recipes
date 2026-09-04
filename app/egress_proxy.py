from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)
MAX_HEADER_BYTES = 64 * 1024
MAX_HEADER_COUNT = 100
MAX_STREAM_BYTES = 100 * 1024 * 1024
HOP_BY_HOP_HEADERS = {
    b"connection",
    b"keep-alive",
    b"proxy-authenticate",
    b"proxy-authorization",
    b"proxy-connection",
    b"te",
    b"trailer",
    b"transfer-encoding",
    b"upgrade",
}


async def _public_address(host: str, port: int) -> str:
    loop = asyncio.get_running_loop()
    results = await loop.getaddrinfo(host, port, type=0)
    addresses = {ipaddress.ip_address(result[4][0]) for result in results}
    if not addresses or any(
        not address.is_global
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
        or address.is_loopback
        or address.is_link_local
        or address.is_private
        for address in addresses
    ):
        raise ValueError("Nicht-öffentliche Zieladresse")
    return str(sorted(addresses, key=lambda value: (value.version, int(value)))[0])


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    transferred = 0
    try:
        while chunk := await reader.read(64 * 1024):
            transferred += len(chunk)
            if transferred > MAX_STREAM_BYTES:
                logger.info("Egress-Datenbudget von %s Bytes überschritten", MAX_STREAM_BYTES)
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


async def _headers(reader: asyncio.StreamReader) -> list[bytes]:
    headers: list[bytes] = []
    size = 0
    for _ in range(MAX_HEADER_COUNT):
        line = await asyncio.wait_for(reader.readline(), timeout=10)
        size += len(line)
        if size > MAX_HEADER_BYTES:
            raise ValueError("Header zu groß")
        if line in {b"\r\n", b"\n", b""}:
            return headers
        if b"\x00" in line or not line.endswith((b"\r\n", b"\n")):
            raise ValueError("Ungültiger Header")
        headers.append(line)
    raise ValueError("Zu viele Header")


async def _connect_tunnel(
    target: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    parsed = urlsplit(f"//{target}")
    if not parsed.hostname or parsed.username or parsed.password or (parsed.port or 443) != 443:
        raise ValueError("CONNECT ist nur zu Port 443 erlaubt")
    address = await _public_address(parsed.hostname, 443)
    remote_reader, remote_writer = await asyncio.wait_for(
        asyncio.open_connection(address, 443), timeout=15
    )
    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await writer.drain()
    await asyncio.gather(
        _pipe(reader, remote_writer),
        _pipe(remote_reader, writer),
        return_exceptions=True,
    )


async def _plain_http(
    method: str,
    target: str,
    version: str,
    headers: list[bytes],
    writer: asyncio.StreamWriter,
) -> None:
    if method not in {"GET", "HEAD"}:
        raise ValueError("Nur lesende HTTP-Anfragen sind erlaubt")
    parsed = urlsplit(target)
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or (parsed.port or 80) != 80
    ):
        raise ValueError("Ungültiges HTTP-Ziel")
    address = await _public_address(parsed.hostname, 80)
    remote_reader, remote_writer = await asyncio.wait_for(
        asyncio.open_connection(address, 80), timeout=15
    )
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"
    remote_writer.write(f"{method} {path} {version}\r\n".encode("ascii"))
    for header in headers:
        name = header.split(b":", 1)[0].strip().lower()
        if name not in HOP_BY_HOP_HEADERS | {b"host"}:
            remote_writer.write(header)
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    remote_writer.write(f"Host: {host}\r\n".encode("ascii"))
    remote_writer.write(b"Connection: close\r\n\r\n")
    await remote_writer.drain()
    await _pipe(remote_reader, writer)
    remote_writer.close()


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=10)
        if len(request_line) > 8192:
            raise ValueError("Anfragezeile zu lang")
        method, target, version = request_line.decode("ascii").strip().split(" ", 2)
        headers = await _headers(reader)
        if method == "GET" and target == "/health/live":
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b'Content-Length: 15\r\nConnection: close\r\n\r\n{"status":"ok"}'
            )
            await writer.drain()
        elif method == "CONNECT":
            await _connect_tunnel(target, reader, writer)
        else:
            await _plain_http(method, target, version, headers, writer)
    except Exception as exc:
        logger.info("Egress-Anfrage abgelehnt: %s", exc)
        if not writer.is_closing():
            writer.write(
                b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            with contextlib.suppress(ConnectionError):
                await writer.drain()
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()


async def main() -> None:
    server = await asyncio.start_server(
        handle_client,
        "0.0.0.0",  # noqa: S104 - nur im isolierten Container-Netz veröffentlicht
        8888,
        limit=MAX_HEADER_BYTES,
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
