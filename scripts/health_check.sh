#!/usr/bin/env bash
# Lightweight end-to-end smoke test for the backend.
# Verifies docker services are up and receiver, MCP, and nanobot answer.

set -Eeuo pipefail
cd "$(dirname "$0")/.."

GREEN="\033[32m"; RED="\033[31m"; DIM="\033[2m"; RESET="\033[0m"
ok() { printf "${GREEN}✓${RESET} %s\n" "$*"; }
bad(){ printf "${RED}✗${RESET} %s\n" "$*"; exit 1; }

# 1. docker services up?
if ! docker compose ps --services --filter "status=running" | grep -q .; then
  bad "no running services"
fi
ok "containers running"

# 2. public receiver port responds?
PORT="$(grep -E '^AUDIT_RECEIVER_PORT=' .env 2>/dev/null | cut -d= -f2 || echo 8080)"
if curl -fsS --max-time 5 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
  ok "audit-receiver :${PORT}/health OK"
else
  bad "audit-receiver :${PORT}/health not responding"
fi

# 3. nanobot health inside the network
if docker compose exec -T audit-receiver curl -fsS --max-time 5 http://nanobot:8080/health >/dev/null 2>&1; then
  ok "nanobot :8080/health OK"
else
  bad "nanobot :8080/health not responding"
fi

# 4. mcp-tools health inside the network
if docker compose exec -T mcp-tools curl -fsS --max-time 5 http://127.0.0.1:9000/health >/dev/null 2>&1; then
  ok "mcp-tools :9000/health OK"
else
  bad "mcp-tools :9000/health not responding"
fi

printf "\n${DIM}backend looks healthy.${RESET}\n"
