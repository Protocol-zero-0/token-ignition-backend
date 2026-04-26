#!/usr/bin/env bash
# ============================================================================
# Token-Ignition backend  ·  one-shot setup
# ----------------------------------------------------------------------------
#   1. verify config.yaml exists and has no placeholders
#   2. render nanobot.config.json and .env from config.yaml
#   3. docker compose up -d
#   4. health check
# ============================================================================

set -Eeuo pipefail

cd "$(dirname "$0")"

BOLD="\033[1m"; GREEN="\033[32m"; YELLOW="\033[33m"; RED="\033[31m"; DIM="\033[2m"; RESET="\033[0m"
log()  { printf "${DIM}[%s]${RESET} %s\n" "$(date +%H:%M:%S)" "$*"; }
ok()   { printf "${GREEN}✓${RESET} %s\n" "$*"; }
warn() { printf "${YELLOW}!${RESET} %s\n" "$*"; }
err()  { printf "${RED}✗${RESET} %s\n" "$*" >&2; }

# -----------------------------------------------------------------------------
# 0. preflight
# -----------------------------------------------------------------------------
printf "${BOLD}token-ignition backend · setup${RESET}\n\n"

command -v docker >/dev/null 2>&1 \
  || { err "docker not found. Please install Docker Engine first."; exit 1; }

docker compose version >/dev/null 2>&1 \
  || { err "docker compose plugin not found. Please install the Docker Compose plugin."; exit 1; }

command -v python3 >/dev/null 2>&1 \
  || { err "python3 not found."; exit 1; }

# -----------------------------------------------------------------------------
# 1. config.yaml
# -----------------------------------------------------------------------------
if [[ ! -f config.yaml ]]; then
  warn "config.yaml not found"
  log  "copying config.example.yaml -> config.yaml"
  cp config.example.yaml config.yaml
  err "please edit config.yaml and fill in the {{...}} placeholders, then re-run ./setup.sh"
  exit 1
fi

# ignore full-line comments when scanning for placeholders
if grep -vE '^[[:space:]]*#' config.yaml | grep -qE "\{\{[^}]+\}\}"; then
  err "config.yaml still contains unreplaced placeholders:"
  grep -nE "\{\{[^}]+\}\}" config.yaml | grep -vE '^[[:space:]]*[0-9]+:[[:space:]]*#' | sed 's/^/    /'
  exit 1
fi
ok "config.yaml looks fully populated"

# -----------------------------------------------------------------------------
# 2. render runtime files
# -----------------------------------------------------------------------------
if [[ ! -d .venv-render ]]; then
  log "creating render venv (one-time, ~10s)"
  python3 -m venv .venv-render
  .venv-render/bin/pip install --quiet --upgrade pip
  .venv-render/bin/pip install --quiet pyyaml
fi
.venv-render/bin/python scripts/render_nanobot_config.py

# -----------------------------------------------------------------------------
# 3. docker compose
# -----------------------------------------------------------------------------
log "building and starting containers"
docker compose up -d --build

# -----------------------------------------------------------------------------
# 4. health check
# -----------------------------------------------------------------------------
log "waiting for services to come up (up to 60s)"

HEALTHY=0
for i in {1..30}; do
  sleep 2
  if docker compose ps --format json 2>/dev/null | grep -q '"Health":"healthy"'; then
    HEALTHY=1
    break
  fi
done

if [[ "$HEALTHY" -eq 1 ]]; then
  ok "at least one service reports healthy"
else
  warn "services are up but not yet reporting healthy — check with 'docker compose logs -f'"
fi

echo
ok "backend is up"
echo
printf "  ${BOLD}next steps${RESET}\n"
printf "  ------------------------------------------------------------\n"
printf "  audit receiver    http://localhost:%s\n" "$(grep -E '^AUDIT_RECEIVER_PORT=' .env 2>/dev/null | cut -d= -f2 || echo 8080)"
printf "  trigger endpoint  http://localhost:%s/v1/audit/trigger\n" "$(grep -E '^AUDIT_RECEIVER_PORT=' .env 2>/dev/null | cut -d= -f2 || echo 8080)"
printf "  logs              docker compose logs -f\n"
printf "  stop              ./stop.sh\n"
printf "  health check      ./scripts/health_check.sh\n"
echo
