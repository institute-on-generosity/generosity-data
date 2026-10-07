#!/usr/bin/env bash
# Download IRS 990 e-file XML batches and extract only 990/990-EZ filings for EINs in orgs.
# Usage: YEAR=2025 BATCHES="05A 11B" etl/fetch_990_xml.sh
#        TYPES=990PF BATCHES="01A … 12A" etl/fetch_990_xml.sh   # foundations (990-PF) -> data/xml/<year>pf, for load_grants.py
# Note: batches use Deflate64, which Python's zipfile can't read; Info-ZIP unzip can.
# The IRS index sometimes lists filings under the wrong batch; those are skipped.
set -euo pipefail
cd "$(dirname "$0")/.."
DB="${DATABASE_URL:-postgresql:///nombot}"
YEAR="${YEAR:-2025}"; BATCHES="${BATCHES:-05A 11B}"; TYPES="${TYPES:-990 990EZ}"
DIR="data/xml"; OUT="$DIR/$YEAR$([ "$TYPES" = 990PF ] && echo pf)"; mkdir -p "$OUT"
BASE="https://apps.irs.gov/pub/epostcard/990/xml/$YEAR"
UA="generosity-data (+https://github.com/institute-on-generosity/generosity-data)"
[ -f "$DIR/index_$YEAR.csv" ] || curl -fsSL -A "$UA" -o "$DIR/index_$YEAR.csv" "$BASE/index_$YEAR.csv"
psql "$DB" -Atc "SELECT ein FROM orgs" > "$DIR/eins.txt"
for b in $BATCHES; do
  zip="$DIR/${YEAR}_TEOS_XML_$b.zip"
  [ -f "$zip" ] || curl -fsSL -A "$UA" -o "$zip" "$BASE/${YEAR}_TEOS_XML_$b.zip"
  python3 - "$DIR" "$YEAR" "$b" "$TYPES" <<'PY'
import csv, sys
d, y, b, types = sys.argv[1:]
eins = set(open(f"{d}/eins.txt").read().split())
with open(f"{d}/want_{b}{'_pf' if types == '990PF' else ''}.txt", "w") as f:
    for r in csv.DictReader(open(f"{d}/index_{y}.csv", encoding="latin-1")):
        if r["XML_BATCH_ID"] == f"{y}_TEOS_XML_{b}" and r["EIN"].zfill(9) in eins and r["RETURN_TYPE"] in types.split():
            f.write(r["OBJECT_ID"] + "_public.xml\n")
PY
  want="$DIR/want_$b$([ "$TYPES" = 990PF ] && echo _pf).txt"
  xargs -n 2000 unzip -qq -o -d "$OUT" "$zip" < "$want" 2>/dev/null || true
  echo "$b: $(wc -l < "$want") wanted"
done
echo "extracted: $(ls "$OUT" | wc -l) filings in $OUT"
