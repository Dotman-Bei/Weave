#!/usr/bin/env bash
# Bring up HydraDB and point Weave at it.
#
# Weave runs on its embedded graph engine by default and needs nothing here.
# Run this only when you want the Bolt/OpenCypher backend.
set -euo pipefail

COMPOSE_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/docker-compose.yml"
URI="${WEAVE_HYDRA_URI:-neo4j://localhost:7687}"
TOKEN="${WEAVE_HYDRA_TOKEN:-local-development-token-32-bytes}"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker not found. Install Docker, or keep the default embedded backend:" >&2
  echo "  WEAVE_BACKEND=embedded weave serve" >&2
  exit 1
fi

echo "Starting HydraDB from ${COMPOSE_FILE}"
docker compose -f "${COMPOSE_FILE}" up -d hydradb

echo -n "Waiting for the Bolt endpoint"
for _ in $(seq 1 30); do
  if python3 - "$URI" "$TOKEN" <<'PY' 2>/dev/null
import sys
from neo4j import GraphDatabase
driver = GraphDatabase.driver(sys.argv[1], auth=("", sys.argv[2]))
with driver.session() as session:
    session.run("RETURN 1")
driver.close()
PY
  then
    echo " ready."
    echo
    echo "Point Weave at it:"
    echo "  export WEAVE_BACKEND=hydra"
    echo "  export WEAVE_HYDRA_URI=${URI}"
    echo "  export WEAVE_HYDRA_TOKEN=${TOKEN}"
    echo "  weave serve"
    exit 0
  fi
  echo -n "."
  sleep 2
done

echo " timed out." >&2
echo "Check container logs: docker compose -f ${COMPOSE_FILE} logs hydradb" >&2
exit 1
