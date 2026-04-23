"""
Tool: fetch_url
---------------
The *only* way the auditor reaches the outside world.

Supports GET/POST against any public URL the candidate supplied. Bounded
response size, explicit timeout, no redirect loops, no auth passthrough.
Returns a structured result the LLM can reason over.
"""

from __future__ import annotations

import time
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field


MAX_BYTES = 512 * 1024     # 512KB is enough for a benchmark JSON / log slice
TIMEOUT_SEC = 25.0
MAX_REDIRECTS = 3


class FetchResult(BaseModel):
    ok: bool
    url: str
    final_url: str
    status_code: int | None = None
    elapsed_ms: int
    content_type: str | None = None
    truncated: bool = False
    body: str = ""
    error: str | None = None


async def fetch_url(
    url: str,
    method: Literal["GET", "POST"] = "GET",
    body: str | None = None,
    content_type: str = "application/json",
) -> FetchResult:
    """
    Fetch a public URL. Body is optional (only meaningful for POST).

    Safety rails:
      * hard 25s timeout
      * follows at most 3 redirects
      * refuses private / loopback addresses
      * caps response body at 512KB (truncated flag is set if trimmed)
    """
    start = time.monotonic()

    if not url.lower().startswith(("http://", "https://")):
        return FetchResult(
            ok=False,
            url=url,
            final_url=url,
            elapsed_ms=int((time.monotonic() - start) * 1000),
            error="url must start with http:// or https://",
        )

    headers = {
        "User-Agent": "token-ignition-auditor/0.2 (+https://token-ignition.vercel.app)",
        "Accept": "application/json, text/plain, text/html;q=0.8, */*;q=0.5",
    }

    post_kwargs: dict[str, Any] = {}
    if method == "POST":
        headers["Content-Type"] = content_type
        post_kwargs["content"] = (body or "").encode("utf-8")

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
            timeout=TIMEOUT_SEC,
        ) as client:
            resp = await client.request(method, url, headers=headers, **post_kwargs)

        raw = resp.content or b""
        truncated = len(raw) > MAX_BYTES
        if truncated:
            raw = raw[:MAX_BYTES]

        try:
            text = raw.decode(resp.encoding or "utf-8", errors="replace")
        except (LookupError, UnicodeDecodeError):
            text = raw.decode("utf-8", errors="replace")

        return FetchResult(
            ok=resp.status_code < 400,
            url=url,
            final_url=str(resp.url),
            status_code=resp.status_code,
            elapsed_ms=int((time.monotonic() - start) * 1000),
            content_type=resp.headers.get("content-type"),
            truncated=truncated,
            body=text,
        )

    except httpx.TimeoutException:
        return FetchResult(
            ok=False,
            url=url,
            final_url=url,
            elapsed_ms=int((time.monotonic() - start) * 1000),
            error=f"timeout after {TIMEOUT_SEC}s",
        )
    except httpx.HTTPError as e:
        return FetchResult(
            ok=False,
            url=url,
            final_url=url,
            elapsed_ms=int((time.monotonic() - start) * 1000),
            error=f"http_error: {type(e).__name__}: {e}",
        )


TOOL_DESCRIPTION = (
    "Fetch a public HTTP/HTTPS URL. This is the ONLY way you can reach the "
    "candidate's live endpoint or any external artifact. Use it to verify the "
    "endpoint is reachable, inspect the response body, and re-run POST requests "
    "against an agent endpoint. Response body is capped at 512KB; bodies larger "
    "than that are truncated (the 'truncated' flag will be true)."
)


FETCH_URL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "description": "Absolute http(s) URL to fetch.",
        },
        "method": {
            "type": "string",
            "enum": ["GET", "POST"],
            "default": "GET",
        },
        "body": {
            "type": "string",
            "description": "Optional request body. Only used for POST.",
            "default": "",
        },
        "content_type": {
            "type": "string",
            "default": "application/json",
        },
    },
    "required": ["url"],
}
