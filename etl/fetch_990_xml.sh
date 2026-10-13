#!/usr/bin/env bash
# Download IRS 990 e-file XML batches and extract the filings we load.
# Usage: YEAR=2025 BATCHES="05A 11B" etl/fetch_990_xml.sh     # 990/990-EZ for EINs in orgs -> data/xml/<year>
#        YEAR=2024 TYPES=990PF etl/fetch_990_xml.sh           # every 990-PF in the US -> data/xml/<year>pf
# BATCHES defaults to every batch the year's index names (2023's index names none: 01A … 12A).
# 990-PFs are national, with no orgs filter: foundations anywhere fund groups in the loaded states.
# Files are picked by listing each zip against the index, so filings the index puts in the wrong
# batch (or in none) are still found.
# Note: batches use Deflate64, which Python's zipfile can't read; Info-ZIP unzip can. Some zips are cut
# short (2025 05A lists 81,770 files, fewer than its index): the missing filings are skipped.
set -euo pipefail
cd "$(dirname "$0")/.."
DB="${DATABASE_URL:-postgresql:///nombot}"
YEAR="${YEAR:-2025}"; TYPES="${TYPES:-990 990EZ}"
PF=$([ "$TYPES" = 990PF ] && echo pf || true)
DIR="data/xml"; OUT="$DIR/$YEAR$PF"; mkdir -p "$OUT"
BASE="https://apps.irs.gov/pub/epostcard/990/xml/$YEAR"
UA="generosity-data (+https://github.com/institute-on-generosity/generosity-data)"
[ -f "$DIR/index_$YEAR.csv" ] || curl -fsSL -A "$UA" -o "$DIR/index_$YEAR.csv" "$BASE/index_$YEAR.csv"
BATCHES="${BATCHES:-$(python3 -c '
import csv, sys
y = sys.argv[1]
b = sorted({r.get("XML_BATCH_ID", "").upper().removeprefix(f"{y}_TEOS_XML_") for r in csv.DictReader(open(f"data/xml/index_{y}.csv", encoding="latin-1"))} - {""})
print(" ".join(b) or " ".join(f"{m:02d}A" for m in range(1, 13)))
' "$YEAR")}"
[ -n "$PF" ] || psql "$DB" -Atc "SELECT ein FROM orgs" > "$DIR/eins.txt"
for b in $BATCHES; do
  zip="$DIR/${YEAR}_TEOS_XML_$b.zip"
  [ -f "$zip" ] || { curl -fsSL --retry 3 -A "$UA" -o "$zip.part" "$BASE/${YEAR}_TEOS_XML_$b.zip" && mv "$zip.part" "$zip"; } || { echo "$b: no such batch"; continue; }
  want="$DIR/want_${YEAR}_$b$PF.txt"
  { unzip -Z1 "$zip" 2>/dev/null || true; } | python3 -c '
import csv, sys
d, y, types, pf = sys.argv[1:]
eins = None if pf else set(open(f"{d}/eins.txt").read().split())
ids = {r["OBJECT_ID"] for r in csv.DictReader(open(f"{d}/index_{y}.csv", encoding="latin-1"))
       if r["RETURN_TYPE"] in types.split() and (eins is None or r["EIN"].zfill(9) in eins)}
for name in sys.stdin:
    name = name.strip()
    if name.rsplit("/", 1)[-1].split("_")[0] in ids: print(name)
' "$DIR" "$YEAR" "$TYPES" "$PF" > "$want"
  xargs -n 2000 unzip -qq -o -j -d "$OUT" "$zip" < "$want" 2>/dev/null || true
  echo "$b: $(wc -l < "$want") extracted"
done
echo "$OUT: $(ls "$OUT" | wc -l) filings"
