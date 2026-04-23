# Token-Ignition backend · v0.2

> The audit engine behind the gates.
> Submissions come in, a nanobot-powered audit agent reasons about them,
> verdicts land as git commits on a public ledger. A watchdog observes
> and alerts when something looks off.

This repo is the **backend only**. It does not serve HTML. The frontend
lives in `justDance-everybody/token-ignition` and the public ledger lives
in `billion-token-one-task/token-ignition-ledger`. The contract between
them is the shape of `submissions/<hash>.json` on the ledger.

---

## One-file configuration

Everything you can change lives in **`config.yaml`**. Never edit anything
else by hand for configuration purposes. Any change in `config.yaml` takes
effect after re-running `./setup.sh`.

Generated artifacts (gitignored, rebuilt every setup):
- `.env`                          — env vars for docker-compose
- `nanobot/nanobot.config.json`   — the runtime config mounted into nanobot

---

## One-shot deployment

On the server where the audit agent will run:

```bash
git clone https://github.com/justDance-everybody/token-ignition-backend.git
cd token-ignition-backend

cp config.example.yaml config.yaml
$EDITOR config.yaml            # fill in every {{...}} placeholder

./setup.sh                      # builds images, starts services, health checks
```

Stop:

```bash
./stop.sh
```

Check health:

```bash
./scripts/health_check.sh
```

Follow logs:

```bash
docker compose logs -f
docker compose logs -f watchdog    # just the observer
```

---

## Directory map

```
.
├── config.example.yaml        ← the one file you edit
├── setup.sh                   ← one-shot boot
├── stop.sh
├── docker-compose.yml         ← three services: nanobot + mcp-tools + watchdog
│
├── nanobot/                   ← nanobot audit agent runtime
│   ├── Dockerfile
│   └── README.md
│
├── mcp-tools/                 ← the two audit tools the agent calls
│   ├── server.py              ← MCP server (http on :9000 inside net)
│   ├── fetch_url.py           ← the ONLY external-world tool
│   ├── commit_verdict.py      ← the ONLY ledger-write tool
│   ├── Dockerfile
│   └── requirements.txt
│
├── watchdog/                  ← L4 observability service
│   ├── watchdog.py            ← polls ledger, computes indicators, alerts
│   ├── Dockerfile
│   └── requirements.txt
│
├── prompts/                   ← the three audit gates (versioned)
│   ├── gate.1.v1.md           ← admission auditor
│   ├── gate.2.v1.md           ← verified auditor
│   └── gate.3.v1.md           ← research auditor (2-of-3 consensus)
│
└── scripts/
    ├── render_nanobot_config.py   ← yaml → nanobot json + .env
    └── health_check.sh
```

The **submission API layer** (`api/submit.ts`) and the **live ledger
panel** (`ledger.js`) live in the frontend repo. They talk to this
backend over (a) the ledger repo itself, and (b) the nanobot trigger URL
you expose publicly (see `config.yaml → nanobot.public_url`).

---

## Architecture

```
                     ┌────────────────────────────────┐
                     │  token-ignition.vercel.app     │
                     │  (frontend, separate repo)     │
                     └──────────┬─────────────────────┘
                                │ POST /api/submit
                                ▼
                     ┌────────────────────────────────┐
                     │  Vercel serverless fn          │
                     │  (lives in the frontend repo)  │
                     └──────────┬────────────┬────────┘
                                │            │
           PUT submissions/<h>.json          │  POST /v1/audit/trigger
           (verdict=pending)                 │  (fire-and-forget)
                                │            ▼
                                │     ┌──────────────────────────────────┐
                                │     │  your server  (this repo)        │
                                │     │  ┌─────────┐ ┌─────────┐ ┌──────┐│
                                │     │  │ nanobot │─│ mcp-    │ │watch-││
                                │     │  │ gateway │ │ tools   │ │ dog  ││
                                │     │  └────┬────┘ └─────────┘ └──┬───┘│
                                │     └───────┼────────────────────┼────┘
                                │             │                    │
                                │  fetch_url, commit_verdict       │  (read-only)
                                ▼             ▼                    │
                          ┌────────────────────────────┐           │
                          │ public ledger repo (GitHub)│◄──────────┘
                          │ submissions/<hash>.json    │
                          └──────────┬─────────────────┘
                                     │ raw.githubusercontent
                                     ▼
                            (frontend polls and renders)
```

### Invariants

- **The agent is stateless.** Nanobot's memory / dream / channels are all
  disabled via the rendered config. Every audit is reproducible from
  (submission + prompt version + models used + self-check).
- **The agent has exactly two tools.** `fetch_url` to reach the outside
  world, `commit_verdict` to publish a judgement. Nothing else is allowed.
- **Prompts are versioned.** Every verdict records which prompt version
  judged it. Prompts change → bump the version → audit trail stays sane.
- **The ledger is the source of truth.** No internal database. Anyone can
  clone the ledger repo and replay every decision.
- **The watchdog is read-only.** It cannot influence audits. A watchdog
  crash does not affect the pipeline.

---

## L1 — auditor self-check

Every `commit_verdict` must include a `self_check` object:

```json
{
  "evidence_sufficiency": "sufficient" | "marginal" | "insufficient",
  "suspected_injection":  true | false,
  "tool_failures":        0,
  "free_note":            "endpoint returned HTML not JSON but parseable"
}
```

This lands on the ledger record next to the verdict. It's what lets you
detect drift without running another audit: scan `submissions/*.json`,
count how many self-rate `insufficient` or flag injection, and you have a
live gauge of gate health.

The prompts require it. The `commit_verdict` tool coerces malformed
values (e.g. unknown sufficiency) to `marginal` and records the coercion,
rather than crashing the audit.

---

## L4 — watchdog observability

Runs as a third container alongside nanobot + mcp-tools. Every N minutes
(default 5), it:

1. Reads `submissions/*.json` from the ledger repo (prefers `index.json`
   if present, falls back to Contents API).
2. Computes indicators over the last 24h:
   - verdict distribution (pending / advanced / rejected / ignited)
   - rejection ratio
   - pending items older than `pending_stuck_minutes`
   - count of `self_check.suspected_injection = true`
   - count of `self_check.evidence_sufficiency = insufficient`
3. Fires alerts when thresholds trip (thresholds in `config.yaml`):
   - rejection ratio > max → warn
   - rejection ratio < min → warn (too permissive)
   - any pending stuck → **error**
   - any suspected injection → warn
   - ≥3 insufficient-evidence in 24h → warn

Alerts are de-duplicated — the same alert key won't fire more than once
per 6 hours.

### Notifier channels

All notifiers are optional — configure any subset. If all are empty,
alerts go to `docker compose logs -f watchdog` only.

| channel       | configure via                                    |
|---------------|--------------------------------------------------|
| generic webhook (Discord / Slack / Feishu) | `config.yaml → watchdog.notifiers.webhook_url` |
| Telegram bot  | `config.yaml → watchdog.notifiers.telegram.{bot_token, chat_id}` |

Telegram quick setup: talk to `@BotFather`, create a bot, copy the token,
send the bot any message, then visit
`https://api.telegram.org/bot<TOKEN>/getUpdates` to find your `chat_id`.

---

## Three gates

| gate     | verdict    | budget unlock | model strategy                              |
|----------|------------|---------------|---------------------------------------------|
| gate.1   | advanced   | 1M tokens     | single frontier model                       |
| gate.2   | advanced   | 10M tokens    | single frontier model                       |
| gate.3   | ignited    | 100M tokens + invite to research | 3 models from 3 families · 2-of-3 consensus |

Which exact models are used is set in `config.yaml → gates`. Changing them
does not change the audit trail for past verdicts, because the ledger
records `models_used` on every record.

---

## Operational notes

- **Audit latency.** gate.1 typically ~15s; gate.3 typically 1–3 min
  (three independent agent loops). The frontend doesn't block — it polls
  the ledger.
- **Abuse / rate limits.** Not wired in v0.2. Simple next-step:
  Cloudflare in front of the Vercel function, or a per-contact-handle
  throttle inside the submit function.
- **Human audit sampling.** Not wired in v0.2. Next-step: a tiny cron
  that samples 1% of verdicts and opens an issue on the ledger repo.
- **Placeholders.** Values you don't know yet (ledger repo, public URL,
  trigger secret) are kept as `{{...}}` in `config.example.yaml`. Setup
  refuses to run until they are all replaced. Optional fields (watchdog
  notifiers) use empty strings and are skipped if left blank.

---

## Development tips

- Edit prompts in `prompts/gate.N.v<k>.md` — bump `v<k>` every time, and
  reference the new filename in `scripts/render_nanobot_config.py` (one
  line change).
- Edit tools in `mcp-tools/*.py`. Rebuild the container with
  `docker compose up -d --build mcp-tools`.
- Edit the watchdog in `watchdog/watchdog.py`. Rebuild with
  `docker compose up -d --build watchdog`.
- Edit tool-level safety (timeouts, size caps, redirect limits) at the top
  of `fetch_url.py` and `commit_verdict.py`. These are deliberately not
  configurable from `config.yaml` — they are security invariants.
