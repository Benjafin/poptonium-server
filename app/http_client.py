"""The shared keep-alive httpx client.

A single pooled client avoids a fresh TCP+TLS handshake on every outbound call
(section resolution and the Plex reverse-proxy fire many). Created lazily and
closed on shutdown.
"""

from typing import Optional

import httpx

_http_client: Optional[httpx.AsyncClient] = None


def http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=30,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
        )
    return _http_client


async def aclose_http_client() -> None:
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
