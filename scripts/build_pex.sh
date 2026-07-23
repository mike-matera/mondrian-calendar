#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${ROOT_DIR}/dist"
OUTPUT_PEX="${DIST_DIR}/reterminal-daemon.pex"

mkdir -p "${DIST_DIR}"

# Build from project metadata and use the console script entrypoint.
pex "${ROOT_DIR}" -c reterminal-daemon -o "${OUTPUT_PEX}"

echo "Built ${OUTPUT_PEX}"
