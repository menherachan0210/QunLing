"""Bound remote image response buffers before the SDK parses their JSON."""
from __future__ import annotations

import httpx

# A 10 MiB image expands to 13.34 MiB of base64; leave room for JSON metadata.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class ImageResponseLimitError(Exception):
    pass


class ImageHTTPClient(httpx.AsyncClient):
    def __init__(self, **kwargs):
        super().__init__(headers={"Accept-Encoding": "identity"}, follow_redirects=True,
                         limits=httpx.Limits(max_connections=2, max_keepalive_connections=1), **kwargs)

    async def send(self, request, *, stream=False, **kwargs):
        # This client is only used for one non-streaming image JSON operation.
        # Read incrementally even when the caller requests a buffered response.
        response = await super().send(request, stream=True, **kwargs)
        try:
            if response.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
                raise ImageResponseLimitError("Unexpected compressed image response")
            length = response.headers.get("content-length", "")
            if length.isdecimal() and int(length) > MAX_RESPONSE_BYTES:
                raise ImageResponseLimitError("Image response exceeds byte budget")
            body = bytearray()
            async for block in response.aiter_bytes(chunk_size=64 * 1024):
                if len(body) + len(block) > MAX_RESPONSE_BYTES:
                    raise ImageResponseLimitError("Image response exceeds byte budget")
                body.extend(block)
            return httpx.Response(response.status_code, headers=response.headers,
                                  content=bytes(body), request=request, extensions=response.extensions)
        finally:
            await response.aclose()
