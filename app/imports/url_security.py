from __future__ import annotations

import ipaddress
import socket
from urllib.parse import ParseResult, urlparse


class UnsafeURL(ValueError):
    pass


def _parse_http_target(url: str) -> tuple[ParseResult, int]:
    if url != url.strip() or any(ord(character) < 32 for character in url):
        raise UnsafeURL("Die Webadresse enthält unzulässige Zeichen")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeURL("Nur HTTP- und HTTPS-Adressen sind erlaubt")
    if not parsed.hostname or parsed.username or parsed.password:
        raise UnsafeURL("Die Webadresse ist ungültig")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeURL("Die Webadresse enthält einen ungültigen Port") from exc
    if port not in {80, 443}:
        raise UnsafeURL("Nicht standardmäßige Netzwerkports sind nicht erlaubt")
    return parsed, port


def validate_http_url_shape(url: str) -> str:
    """Validate URL grammar only; the connecting egress must enforce public DNS/IPs."""
    _parse_http_target(url)
    return url


def validate_public_url(url: str) -> str:
    parsed, port = _parse_http_target(url)
    try:
        results = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        raise UnsafeURL("Die Webadresse konnte nicht aufgelöst werden") from exc
    if not results:
        raise UnsafeURL("Die Webadresse konnte nicht aufgelöst werden")
    for result in results:
        address = ipaddress.ip_address(result[4][0])
        if (
            not address.is_global
            or address.is_multicast
            or address.is_unspecified
            or address.is_reserved
            or address.is_loopback
            or address.is_link_local
            or address.is_private
        ):
            raise UnsafeURL("Interne oder lokale Netzwerkziele sind nicht erlaubt")
    return url
