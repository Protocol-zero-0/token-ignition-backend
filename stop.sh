#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"

echo "stopping token-ignition backend..."
docker compose down
echo "✓ stopped."
