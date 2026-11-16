#!/usr/bin/env bash
# Stop the Hearth panel and the local agent. Game server files and settings are kept.
# Game servers that were running stay up; stop them in the panel first if you want them off.
set -euo pipefail
cd "$(dirname "$0")"
docker compose down
echo "Hearth stopped. Start it again with ./start.sh"
