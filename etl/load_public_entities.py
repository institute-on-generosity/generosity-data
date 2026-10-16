#!/usr/bin/env python3
"""Load school districts (NCES CCD LEA directory) and colleges (IPEDS HD) into public_entities.

  python3 etl/load_public_entities.py           # load the two directories
  python3 etl/load_public_entities.py --match   # link unmatched grants by exact name (or IPEDS alias) in the state,
                                                # one entity per name; colleges with an EIN in orgs link there (graded 100%)
Files (download into data/gov/ and unzip):
  https://nces.ed.gov/ccd/Data/zip/ccd_lea_029_2324_w_1a_073124.zip
  https://nces.ed.gov/ipeds/datacenter/data/HD2023.zip
"""
import csv, glob, os, subprocess, sys

DB = os.environ.get("DATABASE_URL", "postgresql:///nombot")
GOV = os.path.join(os.path.dirname(__file__), "..", "data", "gov")
OUT = os.path.join(GOV, "public_entities.tsv")

def clean(s):
    return " ".join((s or "").replace("\t", " ").split())

def main():
    rows = []
    lea = sorted(glob.glob(os.path.join(GOV, "ccd_lea_029_*.csv")))[-1]
    for r in csv.DictReader(open(lea, encoding="latin-1")):
        # regular, supervisory union and charter districts that are open
        if r["SY_STATUS_TEXT"] in ("Closed", "Inactive"):
            continue
        rows.append([f"nces:{r['LEAID']}", "school_district", clean(r["LEA_NAME"]), "", clean(r["LCITY"]).upper(), r["LSTATE"],
                     r["LZIP"][:5], "", clean(r["WEBSITE"])])
    hd = sorted(glob.glob(os.path.join(GOV, "HD20*.csv")))[-1]
    for r in csv.DictReader(open(hd, encoding="utf-8-sig", errors="replace")):
        if r["CYACTIVE"] not in ("1",):
            continue
        ein = r["EIN"].strip()
        rows.append([f"ipeds:{r['UNITID']}", "college_public" if r["CONTROL"] == "1" else "college_private", clean(r["INSTNM"]),
                     clean(r["IALIAS"]), clean(r["CITY"]).upper(), r["STABBR"], r["ZIP"][:5], ein if len(ein) == 9 and ein.isdigit() else "",
                     clean(r["WEBADDR"])])
    with open(OUT, "w") as f:
        f.writelines("\t".join(x) + "\n" for x in rows)
    sql = f"""
-- upsert, not truncate: public_entity_aliases points at these ids
CREATE TEMP TABLE pe_new (LIKE public_entities);
\\copy pe_new (id, kind, name, alias, city, state, zip, ein, website) FROM '{OUT}' WITH (FORMAT text, NULL '')
INSERT INTO public_entities SELECT * FROM pe_new
ON CONFLICT (id) DO UPDATE SET kind = EXCLUDED.kind, name = EXCLUDED.name, alias = EXCLUDED.alias, city = EXCLUDED.city,
  state = EXCLUDED.state, zip = EXCLUDED.zip, ein = EXCLUDED.ein, website = EXCLUDED.website;
SELECT kind, count(*) FROM public_entities GROUP BY 1 ORDER BY 1;"""
    r = subprocess.run(["psql", DB, "-v", "ON_ERROR_STOP=1", "-qAt"], input=sql, text=True, capture_output=True)
    print(r.stdout.strip() or r.stderr.strip())

MATCH = """
SET work_mem = '1GB';
CREATE OR REPLACE FUNCTION pg_temp.pe_norm(s text) RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT org_norm(regexp_replace(regexp_replace(regexp_replace(upper(s), '\\m(SD|S D)$', 'SCHOOL DISTRICT'),
         '\\mISD\\M', 'INDEPENDENT SCHOOL DISTRICT'), '\\mUSD\\M', 'UNIFIED SCHOOL DISTRICT'))
$$;
CREATE TEMP TABLE pe AS
WITH names AS (
  SELECT id, state, pg_temp.pe_norm(name) n FROM public_entities
  UNION SELECT id, state, pg_temp.pe_norm(trim(a)) FROM public_entities, unnest(regexp_split_to_array(alias, '[|,]')) a
  WHERE alias IS NOT NULL AND length(trim(a)) >= 6)
SELECT n, state, min(id) id FROM names GROUP BY 1, 2 HAVING count(DISTINCT id) = 1;
CREATE INDEX ON pe (state, n);
UPDATE grants g SET recipient_ein = o.ein, recipient_entity = pe.id,
       match_method = CASE WHEN o.ein IS NULL THEN 'public' ELSE 'ipeds-ein' END, match_score = 1
FROM pe JOIN public_entities e ON e.id = pe.id LEFT JOIN orgs o ON o.ein = e.ein
WHERE g.recipient_ein IS NULL AND g.recipient_entity IS NULL AND g.recipient_state = pe.state AND pg_temp.pe_norm(g.recipient_name) = pe.n;
-- hand-checked names (public_entity_aliases, migration 026), any state when state = ''
UPDATE grants g SET recipient_ein = o.ein, recipient_entity = a.id,
       match_method = CASE WHEN o.ein IS NULL THEN 'public' ELSE 'ipeds-ein' END, match_score = 1
FROM public_entity_aliases a JOIN public_entities e ON e.id = a.id LEFT JOIN orgs o ON o.ein = e.ein
WHERE g.recipient_ein IS NULL AND g.recipient_entity IS NULL AND g.recipient_kind IS NULL
  AND org_norm(g.recipient_name) = a.alias_norm AND a.state IN ('', g.recipient_state);
SELECT match_method, count(*) FROM grants WHERE match_method IN ('public', 'ipeds-ein') GROUP BY 1;
"""

if __name__ == "__main__":
    if "--match" in sys.argv:
        r = subprocess.run(["psql", DB, "-v", "ON_ERROR_STOP=1", "-qAt"], input=MATCH, text=True, capture_output=True)
        print(r.stdout.strip() or r.stderr.strip())
    else:
        main()
