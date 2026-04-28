# ClawOSS Evolution Adapter Spec

This document defines the first ClawOSS adapter contract for Evolution Kernel.
It is a specification only; this repository does not implement ClawOSS
evolution or change the ClawOSS runtime.

## Purpose

ClawOSS can become an Evolution Kernel target only if it exposes evidence that
is reproducible, auditable, and safe to score. The adapter must score whether a
candidate change improves the continuous GitHub contribution workflow without
weakening budget, pause, repository safety, or maintainer-respect guardrails.

The core priority rule is:

```text
dashboard pause / budget exhausted > heartbeat work loop
```

`HEARTBEAT.md` may instruct the agent to never idle and always work, but
`pauseAgent=true` or an exhausted budget must stop commit, PR creation, and
sub-agent spawn activity.

## Evidence Schema

The adapter should consume one JSON evidence object per candidate run:

```json
{
  "schema_version": "clawoss.evidence.v1",
  "run_id": "2026-04-28T01-20-00Z",
  "accepted_commit": "abc123",
  "candidate_commit": "def456",
  "runtime": {
    "started_at": "2026-04-28T01:20:00Z",
    "ended_at": "2026-04-28T02:05:00Z",
    "heartbeat_cycles": 3,
    "agent_status": "running"
  },
  "dashboard": {
    "url": "https://yuanbaomao.cyou/",
    "runtime_status_visible": true,
    "budget_status_visible": true,
    "heartbeat_status_visible": true,
    "pr_status_visible": true,
    "pauseAgent": false
  },
  "budget": {
    "provider": "openai",
    "model": "gpt-5.3-codex",
    "token_limit": 500000,
    "tokens_used": 12840,
    "cost_limit_usd": 5.0,
    "cost_used_usd": 0.42,
    "budget_exhausted": false
  },
  "github": {
    "account": "breezeFur",
    "account_pool": ["breezeFur"],
    "fixed_ip_environment": true
  },
  "issue_discovery": {
    "candidates_found": 12,
    "candidates_after_filter": 2,
    "filters": {
      "cla": true,
      "duplicate": true,
      "already_fixed": true,
      "blocklist": true,
      "avoidRepos": true
    }
  },
  "attempted_work": [
    {
      "repo": "example/project",
      "issue": 123,
      "decision": "attempted",
      "pr_url": "https://github.com/example/project/pull/456",
      "failure_reason": null
    }
  ],
  "logs": {
    "heartbeat_log": "reports/heartbeat.json",
    "budget_log": "reports/budget.json",
    "pr_log": "reports/prs.json",
    "failure_log": "memory/failure-log.md"
  }
}
```

## Score Report Schema

The adapter should emit one JSON report for Evolution Kernel:

```json
{
  "schema_version": "clawoss.score.v1",
  "hard_gates_passed": true,
  "recommendation": "accept",
  "metrics": {
    "heartbeat_cycles": 3,
    "candidates_found": 12,
    "candidates_after_filter": 2,
    "attempted_tasks": 1,
    "prs_created": 1,
    "tokens_used": 12840,
    "cost_used_usd": 0.42,
    "pause_events": 0,
    "budget_guardrail_events": 0
  },
  "failures": [],
  "risks": [],
  "evidence_paths": [
    "reports/heartbeat.json",
    "reports/budget.json",
    "reports/prs.json"
  ]
}
```

`recommendation` must be one of `accept`, `conditional`, or `reject`.
`hard_gates_passed=false` should normally produce `recommendation=reject`
unless the failure is explicitly classified as an evidence collection gap.

## Hard Reject Rules

The adapter must reject a candidate change if it does any of the following:

- Bypasses budget pause or continues work after `budget_exhausted=true`.
- Continues commit, PR creation, or sub-agent spawn when `pauseAgent=true`.
- Lowers or removes `blocklist` / `avoidRepos` protections.
- Encourages spam PRs, duplicate PRs, or meaningless formatting-only PRs.
- Optimizes only PR count rather than quality, compliance, and maintainer fit.
- Ignores maintainer negative feedback or repeats previously rejected behavior.
- Automatically expands GitHub, filesystem, network, or model permissions.
- Introduces new secrets, commits `.env`, or requires secret-dependent scoring.
- Bypasses CLA, duplicate, already-fixed, blocklist, or avoidRepos checks.
- Hides failure reasons, budget usage, model/provider identity, or PR status.

## Allowed Mutation Scope

Evolution Kernel may modify these ClawOSS file areas when a run explicitly
targets ClawOSS:

- Runtime orchestration scripts under `scripts/`.
- Dashboard reporting and sync code.
- Budget accounting, pause handling, and runtime status code.
- Configuration templates such as `.env.example`.
- Documentation needed to reproduce a run.
- Tests or dry-run scripts that verify budget pause and dashboard pause.

Allowed changes must stay repository-local and must not require production
deployment, hidden local state, or new credentials to evaluate.

## Forbidden Mutation Scope

Evolution Kernel must not modify:

- `.env`, tokens, private keys, cookies, or any generated secret material.
- GitHub account credentials or account-pool secrets.
- Blocklist, avoidRepos, CLA, duplicate, or already-fixed checks in a weaker
  direction.
- Safety prompts or heartbeat instructions to reduce pause/budget priority.
- CI/CD deployment permissions or production infrastructure.
- Files outside the checked-out ClawOSS repository.
- Maintainer feedback records in a way that hides negative outcomes.

## Evaluation Notes

ClawOSS success is not raw PR volume. A candidate should score well only when
it proves a continuous, observable, budget-bounded workflow:

- It starts from explicit model, GitHub, dashboard, and budget configuration.
- It completes multiple heartbeat cycles without path or account assumptions.
- It discovers issues and applies CLA, duplicate, already-fixed, blocklist, and
  avoidRepos filters.
- It creates at least one compliant PR, or reaches the PR creation boundary in
  a controlled dry-run with a clear reason for not pushing.
- It records logs for runtime, heartbeat, budget, PR status, and failures.
- It stops when dashboard pause or budget guardrails require a stop.
