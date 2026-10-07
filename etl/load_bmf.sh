#!/usr/bin/env bash
# Download the IRS EO BMF for the given states and upsert into orgs.
# Usage: STATES="wv ky tn va oh" etl/load_bmf.sh
set -euo pipefail
cd "$(dirname "$0")/.."
DB="${DATABASE_URL:-postgresql:///nombot}"
STATES="${STATES:-wv ky tn va oh}"
mkdir -p data/bmf

for st in $STATES; do
  echo "download eo_$st.csv"
  curl -fsSL -A "generosity-data (+https://github.com/institute-on-generosity/generosity-data)" \
    -o "data/bmf/eo_$st.csv" "https://www.irs.gov/pub/irs-soi/eo_$st.csv"
done

{
  cat <<'SQL'
CREATE TEMP TABLE bmf_raw (
  ein text, name text, ico text, street text, city text, state text, zip text,
  grp text, subsection text, affiliation text, classification text, ruling text,
  deductibility text, foundation text, activity text, organization text, status text,
  tax_period text, asset_cd text, income_cd text, filing_req_cd text, pf_filing_req_cd text,
  acct_pd text, asset_amt text, income_amt text, revenue_amt text, ntee_cd text, sort_name text
);
SQL
  for st in $STATES; do
    echo "\\copy bmf_raw FROM 'data/bmf/eo_$st.csv' WITH (FORMAT csv, HEADER true)"
  done
  cat <<'SQL'
INSERT INTO orgs (ein, name, care_of, street, city, state, zip, subsection, foundation,
                  ruling, status, ntee_cd, asset_amt, income_amt, revenue_amt, tax_period)
SELECT DISTINCT ON (ein)
       ein, name, nullif(ico, ''), nullif(street, ''), nullif(city, ''), nullif(state, ''),
       nullif(zip, ''), nullif(subsection, ''), nullif(foundation, ''), nullif(ruling, ''),
       status, nullif(ntee_cd, ''), nullif(asset_amt, '')::bigint,
       nullif(income_amt, '')::bigint, nullif(revenue_amt, '')::bigint, nullif(tax_period, '')
FROM bmf_raw
WHERE status <> '25'
ON CONFLICT (ein) DO UPDATE SET
  name = EXCLUDED.name, care_of = EXCLUDED.care_of, street = EXCLUDED.street,
  city = EXCLUDED.city, state = EXCLUDED.state, zip = EXCLUDED.zip,
  subsection = EXCLUDED.subsection, foundation = EXCLUDED.foundation,
  ruling = EXCLUDED.ruling, status = EXCLUDED.status, ntee_cd = EXCLUDED.ntee_cd,
  asset_amt = EXCLUDED.asset_amt, income_amt = EXCLUDED.income_amt,
  revenue_amt = EXCLUDED.revenue_amt, tax_period = EXCLUDED.tax_period,
  loaded_at = now();
SELECT count(*) AS raw_rows, count(*) FILTER (WHERE status = '25') AS skipped_terminating FROM bmf_raw;
SQL
} | psql "$DB" -v ON_ERROR_STOP=1 -q
