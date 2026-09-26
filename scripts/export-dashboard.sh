#!/usr/bin/env bash
# Pull a dashboard you built/changed in the Grafana UI into the repo.
#
#   export GRAFANA_URL=http://arch-pc:3000
#   export GRAFANA_TOKEN=glsa_xxx   # service account token, Viewer role is enough
#   ./scripts/export-dashboard.sh <dashboard-uid> [dashboards/homelab/<file>.json]
#
# Then: git diff -> git commit -> git push. The git-sync timer deploys it.
set -euo pipefail

uid="${1:?usage: $0 <dashboard-uid> [output.json]}"
out="${2:-dashboards/homelab/${uid}.json}"
: "${GRAFANA_URL:?set GRAFANA_URL}"
: "${GRAFANA_TOKEN:?set GRAFANA_TOKEN}"

curl -fsS -H "Authorization: Bearer ${GRAFANA_TOKEN}" \
  "${GRAFANA_URL%/}/api/dashboards/uid/${uid}" |
  # keep only the dashboard model; drop DB-specific fields so diffs stay clean
  jq '.dashboard | del(.id, .version, .iteration)' > "${out}.tmp"

mv "${out}.tmp" "${out}"
echo "saved ${out}"
python3 "$(dirname "$0")/validate.py"
