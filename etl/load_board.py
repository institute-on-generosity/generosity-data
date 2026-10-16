#!/usr/bin/env python3
"""Load board members into `board_members` (migration 018), then refresh `board_interlocks`
(matching rules: person_key(), person_middle(), person_suffix(), same-county rule for low confidence: migration 027).

  python3 etl/load_board.py

  nonprofits   officers and directors from each org's newest 990 / 990-EZ (filing_flag.people, from
               load_990_flag.py; staff-only roles "key" and "highest" and "former" are left out)
  foundations  officers, directors and trustees from each foundation's newest 990-PF (funders.object_id),
               read from the XML in data/xml/*pf: OfficerDirTrstKeyEmplGrp (name, title, hours, pay, state)
Run after load_990_flag.py and load_grants.py: interlocks use grants.recipient_ein to mark funder-grantee pairs.
Note: filing_flag.people keeps the 25 best-paid people per filing, so very large boards are cut short.
"""
import csv, os, subprocess, sys, tempfile, xml.etree.ElementTree as ET
from multiprocessing import Pool

DB = os.environ.get("DATABASE_URL", "postgresql:///nombot")
XML = os.path.join(os.path.dirname(__file__), "..", "data", "xml")

def psql(sql):
    out = subprocess.run(["psql", DB, "-v", "ON_ERROR_STOP=1", "-qAt"], input=sql, text=True, capture_output=True)
    if out.returncode: sys.exit(f"psql failed: {out.stderr.strip()[:2000]}")
    return out.stdout

def local(tag):
    return tag.rsplit("}", 1)[-1]

def txt(g, name):
    e = next((x for x in g.iter() if local(x.tag) == name), None)
    return " ".join((e.text or "").split()) if e is not None else ""

def officers(args):
    """(ein, object_id, path) -> rows for one 990-PF."""
    ein, oid, path = args
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return []
    year = txt(next((e for e in root.iter() if local(e.tag) == "ReturnHeader"), root), "TaxYr")
    rows = []
    for g in (e for e in root.iter() if local(e.tag) == "OfficerDirTrstKeyEmplGrp"):
        name = txt(g, "PersonNm") or txt(g, "BusinessNameLine1Txt")
        if not name: continue
        rows.append([ein, oid, year, "990PF", name, txt(g, "TitleTxt"), "",
                     txt(g, "AverageHrsPerWkDevotedToPosRt"), txt(g, "CompensationAmt"), txt(g, "StateAbbreviationCd")])
    return rows

def main():
    files = {}
    for d in os.listdir(XML):
        if d.endswith("pf"):
            for f in os.listdir(os.path.join(XML, d)):
                files[f.split("_")[0]] = os.path.join(XML, d, f)
    funders = [l.split("|") for l in psql("SELECT ein, object_id FROM funders").split()]
    jobs = [(ein, oid, files[oid]) for ein, oid in funders if oid in files]
    print(f"{len(jobs)} of {len(funders)} foundations have their newest 990-PF on disk", flush=True)

    tmp = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="")
    w = csv.writer(tmp)
    n = 0
    with Pool() as pool:
        for rows in pool.imap_unordered(officers, jobs, chunksize=200):
            w.writerows(rows); n += len(rows)
    tmp.close()
    print(f"foundation board rows: {n}", flush=True)

    print(psql(f"""
TRUNCATE board_members;
\\copy board_members (ein, object_id, tax_year, form, name, title, role, hours, pay, state) FROM '{tmp.name}' WITH (FORMAT csv, NULL '')
-- nonprofits: each org's newest 990 / 990-EZ
INSERT INTO board_members (ein, object_id, tax_year, form, name, title, role, hours, pay)
SELECT f.ein, f.object_id, f.tax_year, f.form, p->>'name', p->>'title', nullif(p->>'role', ''),
       (p->>'hours')::real, (p->>'pay')::numeric
FROM (SELECT DISTINCT ON (ein) ein, object_id, tax_year, form, people FROM filing_flag
      WHERE people IS NOT NULL ORDER BY ein, tax_year DESC NULLS LAST, object_id DESC) f,
     jsonb_array_elements(f.people::jsonb) p
WHERE coalesce(p->>'role', '') NOT IN ('key', 'highest', 'former');
UPDATE board_members SET person_key = person_key(name);
ANALYZE board_members;
SELECT 'board rows: ' || count(*) || ', people matched by name: ' || count(person_key) || ', orgs: ' || count(DISTINCT ein) FROM board_members;
"""), flush=True)
    os.unlink(tmp.name)
    if "--no-refresh" not in sys.argv:
        print(psql("""
SET work_mem = '512MB';
REFRESH MATERIALIZED VIEW board_interlocks;
SELECT kind || ' ' || confidence || ': ' || count(*) FROM board_interlocks GROUP BY kind, confidence ORDER BY kind, confidence;
"""), flush=True)

if __name__ == "__main__":
    main()
