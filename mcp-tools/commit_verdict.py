"""
Tool: commit_verdict
--------------------
The *only* way the auditor writes to the public ledger.

A verdict is a structured append-only record. The tool enforces:
  * the schema (verdict must be one of the allowed values)
  * the target path (submissions/<submission_id>.json — no wildcards)
  * that the submission_id is a 12-char hex hash
  * that we include the prompt version, model, and timestamp in the record,
    so every historical judgement is fully reproducible.

Anyone can audit the ledger repo's git history and replay the judgement.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field


ALLOWED_VERDICTS = {"pending", "advanced", "ignited", "rejected"}
ALLOWED_SUFFICIENCY = {"sufficient", "marginal", "insufficient"}
SUBMISSION_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def _env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if not v:
        raise RuntimeError(f"missing env var: {name}")
    return v


class CommitResult(BaseModel):
    ok: bool
    submission_id: str
    verdict: str
    commit_sha: str | None = None
    path: str
    html_url: str | None = None
    error: str | None = None


def _status_for(verdict: str, gate: str) -> str:
    if verdict == "rejected":
        return "rejected"
    if verdict == "ignited" or gate == "gate.3":
        return "ignited"
    if gate == "gate.2":
        return "verified"
    return "admitted"


def _summary_row(record: dict[str, Any]) -> dict[str, Any]:
    submission = record.get("submission") or {}
    return {
        "submission_id": record.get("submission_id"),
        "hash": record.get("hash") or f"0x{record.get('submission_id')}",
        "status": record.get("status") or record.get("verdict"),
        "verdict": record.get("verdict"),
        "gate": record.get("gate"),
        "ts": record.get("ts"),
        "updated_at": record.get("updated_at") or record.get("ts"),
        "axes": submission.get("axes", []),
        "repo": submission.get("repo"),
        "endpoint": submission.get("endpoint"),
        "baselineRepo": submission.get("baselineRepo"),
        "baselineEndpoint": submission.get("baselineEndpoint"),
        "task_excerpt": str(submission.get("task", ""))[:180],
    }


async def _update_index(
    client: httpx.AsyncClient,
    repo: str,
    branch: str,
    headers: dict[str, str],
    record: dict[str, Any],
) -> None:
    path = "submissions/index.json"
    api_base = f"https://api.github.com/repos/{repo}/contents/{path}"
    get_resp = await client.get(api_base, headers=headers, params={"ref": branch})

    sha: str | None = None
    rows: list[dict[str, Any]] = []
    if get_resp.status_code == 200:
        meta = get_resp.json()
        sha = meta.get("sha")
        try:
            rows = json.loads(base64.b64decode(meta.get("content", "")).decode("utf-8"))
            if not isinstance(rows, list):
                rows = []
        except (ValueError, UnicodeDecodeError):
            rows = []
    elif get_resp.status_code not in (404, 422):
        raise RuntimeError(f"github GET {path} failed: {get_resp.status_code} {get_resp.text[:200]}")

    row = _summary_row(record)
    rows = [
        row,
        *[r for r in rows if isinstance(r, dict) and r.get("submission_id") != record.get("submission_id")],
    ]
    rows.sort(key=lambda r: int(r.get("updated_at") or r.get("ts") or 0), reverse=True)

    payload: dict[str, Any] = {
        "message": f"index · {record.get('submission_id')}",
        "content": base64.b64encode(
            json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8")
        ).decode("ascii"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    put_resp = await client.put(api_base, headers=headers, json=payload)
    if put_resp.status_code not in (200, 201):
        raise RuntimeError(f"github PUT {path} failed: {put_resp.status_code} {put_resp.text[:200]}")


async def commit_verdict(
    submission_id: str,
    verdict: Literal["advanced", "ignited", "rejected"],
    reasoning: str,
    gate: Literal["gate.1", "gate.2", "gate.3"],
    prompt_version: str,
    models_used: list[str],
    self_check: dict[str, Any],
    evidence: dict[str, Any] | None = None,
) -> CommitResult:
    """
    Commit (or update) the JSON record for <submission_id> in the ledger repo.

    Ledger path convention:  submissions/<submission_id>.json

    The JSON schema we write:
      {
        "submission_id": "0a1b2c...",
        "verdict":       "advanced" | "ignited" | "rejected",
        "gate":          "gate.1" | "gate.2" | "gate.3",
        "prompt_version":"gate.1.v1",
        "models_used":   ["anthropic/claude-opus-4-7"],
        "reasoning":     "...",
        "self_check":    {
          "evidence_sufficiency": "sufficient"|"marginal"|"insufficient",
          "suspected_injection":  bool,
          "tool_failures":        int,
          "free_note":            "..."
        },
        "evidence":      {...},
        "ts":            1712345678,
        "history": [     # appended every time this file is updated
          { "verdict": "pending",   "gate": null,     "ts": ... },
          { "verdict": "advanced",  "gate": "gate.1", "ts": ... }
        ]
      }
    """
    if verdict not in ALLOWED_VERDICTS:
        return CommitResult(
            ok=False,
            submission_id=submission_id,
            verdict=verdict,
            path="",
            error=f"invalid verdict: {verdict}",
        )

    if not SUBMISSION_ID_RE.match(submission_id):
        return CommitResult(
            ok=False,
            submission_id=submission_id,
            verdict=verdict,
            path="",
            error="submission_id must be a 12-char lowercase hex string",
        )

    # best-effort validate self_check shape (don't crash on weird agent output,
    # but normalize obvious issues)
    sc = dict(self_check or {})
    suf = sc.get("evidence_sufficiency")
    if suf not in ALLOWED_SUFFICIENCY:
        sc["evidence_sufficiency"] = "marginal"
        sc["_coerced"] = f"unknown sufficiency: {suf!r}"
    sc["suspected_injection"] = bool(sc.get("suspected_injection", False))
    try:
        sc["tool_failures"] = max(0, int(sc.get("tool_failures", 0)))
    except (TypeError, ValueError):
        sc["tool_failures"] = 0
    sc["free_note"] = str(sc.get("free_note", ""))[:400]

    repo = _env("LEDGER_REPO")
    branch = os.environ.get("LEDGER_BRANCH", "main")
    token = _env("LEDGER_GITHUB_TOKEN")

    path = f"submissions/{submission_id}.json"
    api_base = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "token-ignition-auditor/0.2",
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        get_resp = await client.get(api_base, headers=headers, params={"ref": branch})
        sha: str | None = None
        existing: dict[str, Any] = {}
        if get_resp.status_code == 200:
            meta = get_resp.json()
            sha = meta.get("sha")
            try:
                existing = json.loads(
                    base64.b64decode(meta.get("content", "")).decode("utf-8")
                )
            except (ValueError, UnicodeDecodeError):
                existing = {}
        elif get_resp.status_code not in (404, 422):
            return CommitResult(
                ok=False,
                submission_id=submission_id,
                verdict=verdict,
                path=path,
                error=f"github GET failed: {get_resp.status_code} {get_resp.text[:200]}",
            )

        now = int(time.time())
        history = list(existing.get("history", []))
        history.append({
            "status": _status_for(verdict, gate),
            "verdict": verdict,
            "gate": gate,
            "prompt_version": prompt_version,
            "models_used": models_used,
            "ts": now,
        })

        status = _status_for(verdict, gate)
        record = {
            **existing,
            "submission_id": submission_id,
            "hash": existing.get("hash") or f"0x{submission_id}",
            "status": status,
            "verdict": verdict,
            "gate": gate,
            "prompt_version": prompt_version,
            "models_used": models_used,
            "reasoning": reasoning,
            "self_check": sc,
            "evidence": evidence or {},
            "updated_at": now,
            "history": history,
        }
        record.setdefault("ts", now)

        encoded = base64.b64encode(
            json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8")
        ).decode("ascii")

        commit_msg = f"{verdict} · {gate} · {submission_id}"
        payload: dict[str, Any] = {
            "message": commit_msg,
            "content": encoded,
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha

        put_resp = await client.put(api_base, headers=headers, json=payload)
        if put_resp.status_code not in (200, 201):
            return CommitResult(
                ok=False,
                submission_id=submission_id,
                verdict=verdict,
                path=path,
                error=f"github PUT failed: {put_resp.status_code} {put_resp.text[:200]}",
            )

        data = put_resp.json()
        index_error: str | None = None
        try:
            await _update_index(client, repo, branch, headers, record)
        except Exception as e:
            index_error = f"verdict committed but index update failed: {type(e).__name__}: {e}"

        return CommitResult(
            ok=True,
            submission_id=submission_id,
            verdict=verdict,
            commit_sha=(data.get("commit") or {}).get("sha"),
            path=path,
            html_url=(data.get("content") or {}).get("html_url"),
            error=index_error,
        )


TOOL_DESCRIPTION = (
    "Append a verdict for this submission to the public ledger. This is the ONLY "
    "way a judgement becomes official. The ledger repo tracks every verdict as a "
    "git commit, so every historical decision is auditable. You must include "
    "'gate', 'prompt_version', and 'models_used' — these are what makes the "
    "judgement reproducible. Do not call this tool more than once per gate per "
    "submission."
)


COMMIT_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "submission_id": {
            "type": "string",
            "description": "12-char lowercase hex hash of the submission.",
        },
        "verdict": {
            "type": "string",
            "enum": ["advanced", "ignited", "rejected"],
            "description": (
                "'advanced' = passed gate.1 or gate.2; "
                "'ignited' = passed gate.3 (research invite); "
                "'rejected' = did not pass."
            ),
        },
        "gate": {
            "type": "string",
            "enum": ["gate.1", "gate.2", "gate.3"],
        },
        "prompt_version": {
            "type": "string",
            "description": "e.g. 'gate.1.v1'",
        },
        "models_used": {
            "type": "array",
            "items": {"type": "string"},
        },
        "reasoning": {
            "type": "string",
            "description": "Short plain-text reasoning visible on the public ledger.",
        },
        "self_check": {
            "type": "object",
            "description": (
                "Mandatory self-rating by the auditor. This is the L1 self-check "
                "field — visible on the ledger so drift can be detected."
            ),
            "properties": {
                "evidence_sufficiency": {
                    "type": "string",
                    "enum": ["sufficient", "marginal", "insufficient"],
                },
                "suspected_injection": {"type": "boolean"},
                "tool_failures": {"type": "integer", "minimum": 0},
                "free_note": {"type": "string"},
            },
            "required": [
                "evidence_sufficiency",
                "suspected_injection",
                "tool_failures",
                "free_note",
            ],
        },
        "evidence": {
            "type": "object",
            "description": (
                "Optional structured evidence: URLs fetched, status codes, "
                "relevant log slices, etc."
            ),
        },
    },
    "required": [
        "submission_id", "verdict", "gate", "prompt_version",
        "models_used", "reasoning", "self_check",
    ],
}
