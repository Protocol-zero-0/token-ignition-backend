"""
MCP server exposing the two audit tools (fetch_url, commit_verdict).

We use the streamable-HTTP transport on port 9000 so nanobot (or any other
MCP client in the same docker network) can connect via
  http://mcp-tools:9000/mcp

A /health endpoint is also exposed for docker healthchecks.
"""

from __future__ import annotations

import json
import logging
import os
import sys

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from fetch_url import (
    FETCH_URL_SCHEMA,
    TOOL_DESCRIPTION as FETCH_URL_DESC,
    fetch_url as _fetch_url,
)
from commit_verdict import (
    COMMIT_VERDICT_SCHEMA,
    TOOL_DESCRIPTION as COMMIT_VERDICT_DESC,
    commit_verdict as _commit_verdict,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("ti.mcp")


mcp = FastMCP(
    name="ti-audit-tools",
    instructions=(
        "Audit tools for TOKEN-IGNITION. Use fetch_url to reach any public "
        "endpoint the candidate provided, and commit_verdict to publish the "
        "final judgement to the ledger. Do not call other tools for these "
        "two tasks — these are the sanctioned paths."
    ),
)


@mcp.tool(description=FETCH_URL_DESC)
async def fetch_url(
    url: str,
    method: str = "GET",
    body: str = "",
    content_type: str = "application/json",
) -> dict:
    result = await _fetch_url(
        url=url,
        method=method.upper() if method else "GET",   # type: ignore[arg-type]
        body=body or None,
        content_type=content_type,
    )
    log.info(
        "fetch_url  %s %s -> %s  (%dms, %d bytes)",
        method, url, result.status_code, result.elapsed_ms, len(result.body or ""),
    )
    return result.model_dump()


@mcp.tool(description=COMMIT_VERDICT_DESC)
async def commit_verdict(
    submission_id: str,
    verdict: str,
    gate: str,
    prompt_version: str,
    models_used: list[str],
    reasoning: str,
    self_check: dict,
    evidence: dict | None = None,
) -> dict:
    result = await _commit_verdict(
        submission_id=submission_id,
        verdict=verdict,    # type: ignore[arg-type]
        gate=gate,          # type: ignore[arg-type]
        prompt_version=prompt_version,
        models_used=models_used,
        reasoning=reasoning,
        self_check=self_check,
        evidence=evidence,
    )
    sc = self_check or {}
    log.info(
        "commit_verdict  %s / %s -> %s  (suf=%s, inj=%s, tf=%s, sha=%s)",
        submission_id, gate, verdict,
        sc.get("evidence_sufficiency"),
        sc.get("suspected_injection"),
        sc.get("tool_failures"),
        result.commit_sha,
    )
    return result.model_dump()


async def health(_request):
    return JSONResponse({"ok": True, "service": "ti-mcp", "version": "0.2.0"})


def build_app() -> Starlette:
    return Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Mount("/mcp", app=mcp.streamable_http_app()),
        ]
    )


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("MCP_PORT", "9000"))
    log.info("starting ti-audit-tools MCP server on :%d", port)
    uvicorn.run(build_app(), host="0.0.0.0", port=port, log_level="info")
