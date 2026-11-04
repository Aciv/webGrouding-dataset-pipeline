#!/usr/bin/env bash
# Recollect the ten real entry pages at all three configured resolutions.
# Default: new output directory; --resume retries incomplete pages in a specified batch.
set -euo pipefail

if [[ "${1:-}" == "--help" || $# -gt 2 ]]; then
  echo "Usage: bash scripts/recollect-real-url-10.sh [output-directory] [--resume]"
  echo "Default output: runs/real-url-10-<timestamp>-<pid>"
  exit 0
fi
if [[ $# -eq 2 && "$2" != "--resume" ]]; then
  echo "Second argument must be --resume" >&2
  exit 2
fi
if [[ "${1:-}" == --* ]]; then
  echo "Specify an output directory before --resume" >&2
  exit 2
fi

project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "$project_root"
timestamp=$(date +%Y%m%d-%H%M%S)
output=${1:-"runs/real-url-10-${timestamp}-$$"}
config=${COLLECTION_CONFIG:-examples/real-url-10.json}
resume_args=()
if [[ "${2:-}" == "--resume" ]]; then
  if [[ ! -f "$output/batch-index.json" ]]; then
    echo "Cannot resume: $output/batch-index.json does not exist" >&2
    exit 2
  fi
  resume_args=(--resume)
elif [[ -e "$output/batch-index.json" ]]; then
  echo "Output already contains a batch. Choose a new directory or pass --resume." >&2
  exit 2
fi

mkdir -p -- "$output"
log="$output/recollect-${timestamp}-$$.log"
{
  echo "Output: $output"
  echo "Config: $config"
  npm run build --silent
  node dist/cli.js collect --input "$config" --output "$output" "${resume_args[@]}"
  # Collection already writes bboxes, overlays and hierarchy; audit saved artifacts once.
  node dist/cli.js audit --dir "$output" > "$output/audit.stdout.json"
  node --input-type=module - "$output" <<'NODE'
import fs from 'node:fs/promises';
import path from 'node:path';
const root = path.resolve(process.argv[2]);
const index = JSON.parse(await fs.readFile(path.join(root, 'batch-index.json'), 'utf8'));
const audit = JSON.parse(await fs.readFile(path.join(root, 'summary.json'), 'utf8'));
console.log(JSON.stringify({
  output: root,
  pages: index.counts,
  observations: audit.observations,
  integrityErrors: audit.integrityErrors,
  note: 'captured means artifacts saved; inspect network.json and quality.json for resource failures',
}, null, 2));
NODE
  printf '\nOpen review:\n npm run review -- --dir %q --port 4312\n' "$output"
} 2>&1 | tee "$log"
