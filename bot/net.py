import ipaddress
import socket
import ssl
from urllib.parse import urlparse

import aiohttp


class PublicResolver(aiohttp.abc.AbstractResolver):
    def __init__(self) -> None:
        self._resolver = aiohttp.resolver.DefaultResolver()

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        records = await self._resolver.resolve(host, port, family)
        if not records or any(
            not ipaddress.ip_address(record["host"]).is_global for record in records
        ):
            raise OSError(f"Refusing to connect to a non-public host: {host}")
        return records

    async def close(self) -> None:
        await self._resolver.close()


def is_public_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return False
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return True
    return address.is_global


def public_connector(ssl_context: ssl.SSLContext | None = None) -> aiohttp.TCPConnector:
    return aiohttp.TCPConnector(
        resolver=PublicResolver(), ssl=ssl_context if ssl_context is not None else True
    )
