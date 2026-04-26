from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from typing import Any

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field


log = logging.getLogger("ti.audit_receiver")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

app = FastAPI(title="Token-Ignition audit receiver", version="0.2.1")

IN_FLIGHT: set[str] = set()
SEEN: dict[str, int] = {}


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if not value:
        raise RuntimeError(f"missing env var: {name}")
    return value


def optional_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def require_bearer(authorization: str | None) -> None:
    expected = env("NANOBOT_TRIGGER_SECRET")
    prefix = "Bearer "
    if not authorization or not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="missing bearer token")
    if authorization[len(prefix):] != expected:
        raise HTTPException(status_code=403, detail="invalid bearer token")


class TriggerBody(BaseModel):
    submission_id: str = Field(pattern=r"^[0-9a-f]{12}$")
    submission: dict[str, Any] | None = None
    gate: str = "gate.1"


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "audit-receiver",
        "ledger_repo": optional_env("LEDGER_REPO"),
        "nanobot_url": optional_env("NANOBOT_CHAT_URL", "http://nanobot:8080/v1/chat/completions"),
        "in_flight": sorted(IN_FLIGHT),
    }


@app.post("/v1/audit/trigger")
async def trigger(
    body: TriggerBody,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_bearer(authorization)
    if body.submission_id in IN_FLIGHT:
        return {"ok": True, "accepted": True, "deduped": True, "submission_id": body.submission_id}
    background_tasks.add_task(audit_one, body.submission_id, body.submission, body.gate)
    return {"ok": True, "accepted": True, "submission_id": body.submission_id}


@app.post("/v1/audit/sweep-pending")
async def sweep_pending(
    request: Request,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_bearer(authorization)
    payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    limit = int(payload.get("limit", 10)) if isinstance(payload, dict) else 10
    rows = await list_pending(limit=max(1, min(limit, 50)))
    for row in rows:
        sid = str(row.get("submission_id", ""))
        if sid and sid not in IN_FLIGHT:
            background_tasks.add_task(audit_one, sid, None, "gate.1")
    return {"ok": True, "queued": [r.get("submission_id") for r in rows]}


@app.on_event("startup")
async def startup() -> None:
    if optional_env("AUDIT_SWEEP_ON_STARTUP", "true").lower() == "true":
        asyncio.create_task(startup_sweep())


async def startup_sweep() -> None:
    await asyncio.sleep(5)
    interval = int(optional_env("AUDIT_SWEEP_INTERVAL_SEC", "300"))
    while True:
        try:
            rows = await list_pending(limit=int(optional_env("AUDIT_SWEEP_LIMIT", "10")))
            for row in rows:
                sid = str(row.get("submission_id", ""))
                if sid and sid not in IN_FLIGHT and not recently_seen(sid):
                    asyncio.create_task(audit_one(sid, None, "gate.1"))
        except Exception:
            log.exception("pending sweep failed")
        await asyncio.sleep(interval)


def recently_seen(submission_id: str) -> bool:
    now = int(time.time())
    seen_at = SEEN.get(submission_id, 0)
    if now - seen_at < 300:
        return True
    SEEN[submission_id] = now
    for sid, ts in list(SEEN.items()):
        if now - ts > 3600:
            SEEN.pop(sid, None)
    return False


async def audit_one(submission_id: str, submission: dict[str, Any] | None, gate: str) -> None:
    if submission_id in IN_FLIGHT:
        return
    IN_FLIGHT.add(submission_id)
    try:
        record = await fetch_record(submission_id)
        if not record:
            log.warning("submission %s not found in ledger", submission_id)
            return
        if str(record.get("verdict", "pending")) != "pending":
            log.info("submission %s already has verdict=%s", submission_id, record.get("verdict"))
            return
        submission_payload = submission or record.get("submission") or {}
        await call_nanobot(submission_id, submission_payload, gate)
    except Exception:
        log.exception("audit failed for %s", submission_id)
    finally:
        IN_FLIGHT.discard(submission_id)


async def call_nanobot(submission_id: str, submission: dict[str, Any], gate: str) -> None:
    url = optional_env("NANOBOT_CHAT_URL", "http://nanobot:8080/v1/chat/completions")
    model = optional_env("NANOBOT_GATE1_MODEL", "ti-gate-1")
    prompt = {
        "submission_id": submission_id,
        "gate": gate,
        "submission": submission,
        "instruction": (
            "Audit this Token-Ignition submission using the configured gate prompt. "
            "Fetch the submitted endpoint and baseline endpoint when useful. "
            "When finished, call commit_verdict exactly once."
        ),
    }
    body = {
        "model": model,
        "messages": [{"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}],
        "temperature": 0.1,
    }
    headers = {
        "Authorization": f"Bearer {env('NANOBOT_TRIGGER_SECRET')}",
        "Content-Type": "application/json",
    }
    timeout = float(optional_env("NANOBOT_TIMEOUT_SEC", "180"))
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=headers, json=body)
        if resp.status_code >= 400:
            raise RuntimeError(f"nanobot returned {resp.status_code}: {resp.text[:500]}")
    log.info("audit request completed for %s via %s", submission_id, model)


async def list_pending(limit: int) -> list[dict[str, Any]]:
    repo = env("LEDGER_REPO")
    branch = optional_env("LEDGER_BRANCH", "main")
    token = optional_env("LEDGER_GITHUB_TOKEN")
    headers = {"User-Agent": "token-ignition-audit-receiver/0.2.1"}
    async with httpx.AsyncClient(timeout=20.0) as client:
        if token:
            api_url = f"https://api.github.com/repos/{repo}/contents/submissions/index.json"
            resp = await client.get(
                api_url,
                headers={
                    **headers,
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
                params={"ref": branch},
            )
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            content = resp.json().get("content", "")
            rows = json.loads(base64.b64decode(content).decode("utf-8"))
        else:
            url = f"https://raw.githubusercontent.com/{repo}/{branch}/submissions/index.json"
            resp = await client.get(url, headers=headers)
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            rows = resp.json()
    if not isinstance(rows, list):
        return []
    pending = [r for r in rows if isinstance(r, dict) and r.get("verdict") == "pending"]
    return pending[:limit]


async def fetch_record(submission_id: str) -> dict[str, Any] | None:
    repo = env("LEDGER_REPO")
    branch = optional_env("LEDGER_BRANCH", "main")
    token = optional_env("LEDGER_GITHUB_TOKEN")
    raw_url = f"https://raw.githubusercontent.com/{repo}/{branch}/submissions/{submission_id}.json"
    headers = {"User-Agent": "token-ignition-audit-receiver/0.2.1"}
    if token:
        api_url = f"https://api.github.com/repos/{repo}/contents/submissions/{submission_id}.json"
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                api_url,
                headers={
                    **headers,
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
                params={"ref": branch},
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            content = resp.json().get("content", "")
            return json.loads(base64.b64decode(content).decode("utf-8"))
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(raw_url, headers=headers)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
