"""
Token-Ignition  ·  watchdog  (L4 — observability & alerting)

Read-only observer. Polls the ledger repo every N minutes, computes a few
health indicators, and fires structured notifications when thresholds trip.

Notifiers supported (any combination — leave empty to disable):
  * generic webhook  — Discord / Slack / Feishu / custom
    Sends POST application/json with {"text": "..."} OR, for Discord,
    {"content": "..."}. Both platforms accept our payload.
  * telegram bot     — Bot API sendMessage

Design invariants:
  * Never touches submissions themselves. Pure read.
  * Never interacts with nanobot. A watchdog crash must not affect audits.
  * Keeps minimal state (`/app/state.json`) to avoid re-firing the same
    alert every cycle.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx


log = logging.getLogger("ti.watchdog")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    stream=sys.stdout,
)


STATE_PATH = Path(os.environ.get("WATCHDOG_STATE_PATH", "/var/lib/ti-watchdog/state.json"))


# ---------------------------------------------------------------------------
# config (from env — injected by docker-compose, rendered from config.yaml)
# ---------------------------------------------------------------------------
def env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default if default is not None else "")
    return v


CFG = {
    "ledger_repo":    env("LEDGER_REPO"),
    "ledger_branch":  env("LEDGER_BRANCH", "main"),
    "interval_sec":   int(env("WATCHDOG_INTERVAL_SEC", "300")),
    "rejected_ratio_max":     float(env("WD_REJECTED_RATIO_MAX", "0.85")),
    "rejected_ratio_min":     float(env("WD_REJECTED_RATIO_MIN", "0.05")),
    "rejected_min_sample":    int(env("WD_REJECTED_MIN_SAMPLE", "10")),
    "pending_stuck_minutes":  int(env("WD_PENDING_STUCK_MINUTES", "15")),
    # notifiers
    "webhook_url":       env("WATCHDOG_WEBHOOK_URL"),
    "telegram_token":    env("WATCHDOG_TELEGRAM_BOT_TOKEN"),
    "telegram_chat_id":  env("WATCHDOG_TELEGRAM_CHAT_ID"),
}


# ---------------------------------------------------------------------------
# tiny durable state (avoid re-firing same alert every loop)
# ---------------------------------------------------------------------------
def load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict[str, Any]) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state))
    except Exception as e:
        log.warning("failed to persist state: %s", e)


# ---------------------------------------------------------------------------
# ledger reading
# ---------------------------------------------------------------------------
RAW_BASE = "https://raw.githubusercontent.com/{repo}/{branch}/{path}"
API_BASE = "https://api.github.com/repos/{repo}/contents/{path}"


async def list_submissions(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """
    Prefer submissions/index.json (one cheap request). Fall back to listing
    the submissions/ directory via the GitHub Contents API.
    """
    repo = CFG["ledger_repo"]
    branch = CFG["ledger_branch"]

    # try index.json
    url = RAW_BASE.format(repo=repo, branch=branch, path="submissions/index.json")
    try:
        r = await client.get(url, timeout=15.0)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                return data
    except Exception as e:
        log.debug("index.json miss: %s", e)

    # fall back
    url = API_BASE.format(repo=repo, path="submissions") + f"?ref={branch}"
    try:
        r = await client.get(url, timeout=15.0)
        if r.status_code != 200:
            return []
        files = r.json()
        if not isinstance(files, list):
            return []
    except Exception as e:
        log.warning("contents-api failed: %s", e)
        return []

    # hydrate each file (rate-limited to 30 most recent to stay cheap)
    files = [f for f in files if f.get("name", "").endswith(".json")][:30]
    out: list[dict[str, Any]] = []
    for f in files:
        try:
            rr = await client.get(f["download_url"], timeout=15.0)
            if rr.status_code == 200:
                out.append(rr.json())
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------
# indicators
# ---------------------------------------------------------------------------
def compute_indicators(rows: list[dict[str, Any]]) -> dict[str, Any]:
    now = int(time.time())
    one_day  = now - 86_400
    pending_stuck_cutoff = now - (CFG["pending_stuck_minutes"] * 60)

    last_24h = [r for r in rows if (r.get("ts") or 0) >= one_day]

    verdicts_24h: dict[str, int] = {"pending": 0, "advanced": 0, "rejected": 0, "ignited": 0}
    for r in last_24h:
        v = (r.get("verdict") or "unknown").lower()
        verdicts_24h[v] = verdicts_24h.get(v, 0) + 1

    pending_stuck = [
        r for r in rows
        if (r.get("verdict") or "").lower() == "pending"
        and (r.get("ts") or 0) <= pending_stuck_cutoff
    ]

    suspected_injection_24h = sum(
        1 for r in last_24h
        if ((r.get("self_check") or {}).get("suspected_injection") is True)
    )
    insufficient_24h = sum(
        1 for r in last_24h
        if ((r.get("self_check") or {}).get("evidence_sufficiency") == "insufficient")
    )

    settled_24h = verdicts_24h["advanced"] + verdicts_24h["rejected"] + verdicts_24h["ignited"]
    rejected_ratio = (verdicts_24h["rejected"] / settled_24h) if settled_24h else None

    return {
        "total_24h":     len(last_24h),
        "settled_24h":   settled_24h,
        "verdicts_24h":  verdicts_24h,
        "rejected_ratio_24h": rejected_ratio,
        "pending_stuck_count": len(pending_stuck),
        "pending_stuck_ids":   [r.get("submission_id") for r in pending_stuck[:10]],
        "suspected_injection_24h": suspected_injection_24h,
        "insufficient_24h": insufficient_24h,
    }


def detect_alerts(ind: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    """Return list of alerts to fire this cycle. Respects 'already fired' state."""
    alerts: list[dict[str, Any]] = []
    now = int(time.time())

    rr = ind["rejected_ratio_24h"]
    if rr is not None and ind["settled_24h"] >= CFG["rejected_min_sample"]:
        if rr >= CFG["rejected_ratio_max"]:
            alerts.append({
                "key": "rejected_ratio_high",
                "severity": "warn",
                "title": "rejection rate unusually high",
                "detail": (
                    f"24h rejected ratio = {rr:.0%} "
                    f"(≥ {CFG['rejected_ratio_max']:.0%}), "
                    f"settled sample = {ind['settled_24h']}"
                ),
            })
        elif rr <= CFG["rejected_ratio_min"]:
            alerts.append({
                "key": "rejected_ratio_low",
                "severity": "warn",
                "title": "rejection rate unusually low",
                "detail": (
                    f"24h rejected ratio = {rr:.0%} "
                    f"(≤ {CFG['rejected_ratio_min']:.0%}), "
                    f"settled sample = {ind['settled_24h']}. "
                    f"Gate may be too permissive — inspect prompts."
                ),
            })

    if ind["pending_stuck_count"] > 0:
        alerts.append({
            "key": "pending_stuck",
            "severity": "error",
            "title": "submissions stuck in pending",
            "detail": (
                f"{ind['pending_stuck_count']} submission(s) have been 'pending' "
                f"for > {CFG['pending_stuck_minutes']}m. "
                f"ids: {', '.join(ind['pending_stuck_ids']) or '—'}"
            ),
        })

    if ind["suspected_injection_24h"] > 0:
        alerts.append({
            "key": "suspected_injection",
            "severity": "warn",
            "title": "auditor flagged possible prompt injection",
            "detail": (
                f"{ind['suspected_injection_24h']} verdict(s) in last 24h have "
                f"self_check.suspected_injection = true"
            ),
        })

    if ind["insufficient_24h"] >= 3:
        alerts.append({
            "key": "evidence_insufficient",
            "severity": "warn",
            "title": "auditor self-rated evidence insufficient",
            "detail": (
                f"{ind['insufficient_24h']} verdict(s) in last 24h are self-rated "
                f"'insufficient'. Tool reliability or gate design may need review."
            ),
        })

    # dedupe: don't re-fire same key within 6h
    fresh: list[dict[str, Any]] = []
    fired = state.setdefault("last_fired", {})
    for a in alerts:
        last = fired.get(a["key"], 0)
        if now - last < 6 * 3600:
            continue
        fired[a["key"]] = now
        fresh.append(a)
    return fresh


# ---------------------------------------------------------------------------
# notifiers
# ---------------------------------------------------------------------------
def format_alert(alert: dict[str, Any], ind: dict[str, Any]) -> str:
    v = ind["verdicts_24h"]
    return (
        f"[{alert['severity'].upper()}] TOKEN-IGNITION watchdog  ·  {alert['title']}\n"
        f"{alert['detail']}\n\n"
        f"24h summary: total={ind['total_24h']}  "
        f"advanced={v.get('advanced', 0)}  "
        f"ignited={v.get('ignited', 0)}  "
        f"rejected={v.get('rejected', 0)}  "
        f"pending={v.get('pending', 0)}"
    )


async def notify_webhook(client: httpx.AsyncClient, text: str) -> None:
    url = CFG["webhook_url"]
    if not url:
        return
    # Discord accepts {"content": ...}, Slack/Feishu accept {"text": ...}.
    # Send both keys so the same payload works for all three.
    payload = {"content": text, "text": text}
    try:
        r = await client.post(url, json=payload, timeout=15.0)
        if r.status_code >= 300:
            log.warning("webhook non-2xx: %s %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("webhook error: %s", e)


async def notify_telegram(client: httpx.AsyncClient, text: str) -> None:
    token = CFG["telegram_token"]
    chat_id = CFG["telegram_chat_id"]
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    try:
        r = await client.post(url, json=payload, timeout=15.0)
        if r.status_code >= 300:
            log.warning("telegram non-2xx: %s %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("telegram error: %s", e)


async def fire(client: httpx.AsyncClient, alerts: list[dict[str, Any]], ind: dict[str, Any]) -> None:
    for a in alerts:
        text = format_alert(a, ind)
        log.warning("[ALERT %s] %s", a["key"], a["title"])
        await asyncio.gather(
            notify_webhook(client, text),
            notify_telegram(client, text),
        )


# ---------------------------------------------------------------------------
# loop
# ---------------------------------------------------------------------------
async def one_cycle(client: httpx.AsyncClient, state: dict[str, Any]) -> None:
    rows = await list_submissions(client)
    ind  = compute_indicators(rows)
    log.info(
        "cycle  total_24h=%d settled=%d reject_ratio=%s pending_stuck=%d inj=%d insuf=%d",
        ind["total_24h"], ind["settled_24h"],
        f"{ind['rejected_ratio_24h']:.0%}" if ind["rejected_ratio_24h"] is not None else "—",
        ind["pending_stuck_count"],
        ind["suspected_injection_24h"],
        ind["insufficient_24h"],
    )
    alerts = detect_alerts(ind, state)
    if alerts:
        await fire(client, alerts, ind)
    save_state(state)


async def main() -> None:
    if not CFG["ledger_repo"]:
        log.error("LEDGER_REPO not set — exiting")
        sys.exit(1)
    log.info(
        "watchdog boot  ledger=%s@%s  interval=%ds  webhook=%s  telegram=%s",
        CFG["ledger_repo"], CFG["ledger_branch"], CFG["interval_sec"],
        "on" if CFG["webhook_url"] else "off",
        "on" if (CFG["telegram_token"] and CFG["telegram_chat_id"]) else "off",
    )
    state = load_state()
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await one_cycle(client, state)
            except Exception as e:
                log.exception("cycle error: %s", e)
            await asyncio.sleep(CFG["interval_sec"])


if __name__ == "__main__":
    asyncio.run(main())
