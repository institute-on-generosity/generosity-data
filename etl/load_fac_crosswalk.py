#!/usr/bin/env python3
"""UEI -> EIN crosswalk from the Federal Audit Clearinghouse (single audits), for linking federal_awards
(USAspending carries a UEI, not an EIN) to orgs. Public data, no key.

  curl -L -o data/gov/fac/general.csv https://app.fac.gov/dissemination/public-data/gsa/full/general.csv
  python3 -I etl/load_fac_crosswalk.py data/gov/fac/general.csv

Reads only the auditee identity columns (no contact names, emails or phones) and writes table fac_uei_ein:
one row per (uei, ein) pair seen on an audit since 2022 (UEIs replaced DUNS in April 2022), with the audit
count, newest audit year and the auditee name, city and state as filed. is_multiple_eins audits (one UEI
reporting several EINs) are kept but flagged. Additive: a new table, replaced on each run.
"""
import csv, io, os, subprocess, sys

DB = os.environ.get("DATABASE_URL", "postgresql:///nombot")
csv.field_size_limit(10_000_000)

def main(path):
    pairs = {}
    with open(path, newline="", encoding="utf-8", errors="replace") as fh:
        for r in csv.DictReader(fh):
            uei = (r.get("auditee_uei") or "").strip().upper()
            ein = "".join(ch for ch in (r.get("auditee_ein") or "") if ch.isdigit())
            if len(uei) != 12 or not uei.isalnum() or len(ein) != 9 or ein == "000000000":
                continue
            k = (uei, ein)
            year = int(r.get("audit_year") or 0)
            p = pairs.get(k)
            if p is None:
                pairs[k] = p = {"n": 0, "year": 0, "multi": False, "name": "", "city": "", "state": "", "type": ""}
            p["n"] += 1
            p["multi"] |= (r.get("is_multiple_eins") or "").strip().lower() in ("yes", "true", "y")
            if year >= p["year"]:
                p.update(year=year, name=" ".join((r.get("auditee_name") or "").split()), city=(r.get("auditee_city") or "").strip().upper(),
                         state=(r.get("auditee_state") or "").strip().upper()[:2], type=(r.get("entity_type") or "").strip())
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    for (uei, ein), p in pairs.items():
        w.writerow([uei, ein, p["n"], p["year"], p["multi"], p["name"], p["city"], p["state"], p["type"]])
    sql = """
BEGIN;
DROP TABLE IF EXISTS fac_uei_ein;
CREATE TABLE fac_uei_ein (uei text NOT NULL, ein char(9) NOT NULL, audits int, latest_year int, multiple_eins boolean,
                          name text, city text, state text, entity_type text, PRIMARY KEY (uei, ein));
COPY fac_uei_ein FROM STDIN WITH (FORMAT csv);
""" + buf.getvalue() + "\\.\nCREATE INDEX ON fac_uei_ein (ein);\nANALYZE fac_uei_ein;\nCOMMIT;\n"
    r = subprocess.run(["psql", DB, "-v", "ON_ERROR_STOP=1", "-q"], input=sql, text=True, capture_output=True)
    if r.returncode:
        sys.exit(r.stderr[:2000])
    print(f"fac_uei_ein: {len(pairs)} (uei, ein) pairs, {len({u for u, _ in pairs})} UEIs")

if __name__ == "__main__":
    main(sys.argv[1])
