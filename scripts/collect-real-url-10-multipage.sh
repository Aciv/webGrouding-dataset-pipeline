#!/usr/bin/env bash
# Reuse the standard build/log/audit/resume runner with homepage-only multipage settings.
set -euo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [[ "${1:-}" == "--help" ]]; then
  echo "Usage: bash scripts/collect-real-url-10-multipage.sh [output-directory] [--resume]"
  exit 0
fi
output=${1:-"runs/real-url-10-multipage-$(date +%Y%m%d-%H%M%S)-$$"}
COLLECTION_CONFIG=examples/real-url-10-multipage.json exec bash "$script_dir/recollect-real-url-10.sh" "$output" "${@:2}"
