#!/bin/bash
# Run after any load that can add or change orgs or 990 text (load_bmf.sh, load_990_text.py, load_990_flag.py,
# recover_missing_xml.py, load_grants.py --add, load_charity_grants.py --add). Replaces a bare `psql -f db/refresh.sql`.
#
#   etl/after_load.sh                  # embed new filings, refresh views, top up cause labels, refresh what reads labels
#   etl/after_load.sh --dry            # same refreshes; labels: only count what a top-up would send to Claude
#   LABELS="climate" etl/after_load.sh # only some labels (default: climate women_girls)
#   SKIP_REFRESH=1 etl/after_load.sh   # labels + dependents only (refresh.sql already ran)
#
# Labels run on the Claude subscription (claude -p via etl/labels/claude_cli.mjs), never the API key: the key
# variables are unset here and again for each claude child. A label only loads into org_labels when it is approved
# (LABELS[...]['approved'] in etl/label_causes.py; substance_use is a pilot and is skipped). If a usage limit stops
# the top-up, rerun later: decisions already made are kept and skipped.
set -euo pipefail
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DB="${DATABASE_URL:-postgresql:///nombot}"
LABELS="${LABELS:-climate women_girls}"
DRY=""; [ "${1:-}" = "--dry" ] && DRY="--dry"
cd "$REPO"
t0=$(date +%s)
say() { echo "[after_load $(($(date +%s) - t0))s] $*"; }

if [ -z "${SKIP_REFRESH:-}" ]; then
  say "embed new filing text (local model, same as the label queries)"
  (cd embed && EMBED_PROVIDER=local node embed.mjs)
  say "db/refresh.sql"
  psql "$DB" -X -q -v ON_ERROR_STOP=1 -f db/refresh.sql
fi

for label in $LABELS; do
  say "label top-up: $label"
  python3 etl/label_causes.py --label "$label" topup --api cli --concurrency 3 --max-util "${MAX_UTIL:-0.95}" $DRY
done

if [ -z "$DRY" ]; then
  say "refresh what reads org_labels: research layer (grant_cube), insights"
  psql "$DB" -X -q -v ON_ERROR_STOP=1 -c "SET work_mem = '512MB'; SELECT refresh_research_layer();"
  python3 etl/mine_insights.py
fi
say "done"
