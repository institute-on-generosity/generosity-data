#!/usr/bin/env python3
"""Load "doing business as" names from 990 e-file headers (Filer/DoingBusinessAsName) into org_dba, so grants
written to a public name ("Doctors Without Borders") can find the legal entity. Reads the extracted XML dirs.

  python3 etl/load_dba.py
"""
import glob, os, re, subprocess, sys
from multiprocessing import Pool

DB = os.environ.get("DATABASE_URL", "postgresql:///nombot")
XML = os.path.join(os.path.dirname(__file__), "..", "data", "xml")
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "rematch", "dba.tsv")
EIN = re.compile(rb"<Filer>\s*<EIN>(\d{9})</EIN>")
DBA = re.compile(rb"<DoingBusinessAsName>\s*<BusinessNameLine1Txt>([^<]+)</BusinessNameLine1Txt>")

def read(path):
    with open(path, "rb") as f:
        head = f.read(6000)  # the Filer block sits in the return header
    e, d = EIN.search(head), DBA.search(head)
    if not (e and d):
        return None
    name = d.group(1).decode("utf8", "replace").replace("&amp;", "&").strip()
    return e.group(1).decode(), name

def main():
    files = [p for d in sorted(glob.glob(os.path.join(XML, "*/"))) for p in glob.glob(os.path.join(d, "*.xml"))]
    with Pool(10) as pool:
        rows = {r for r in pool.imap_unordered(read, files, chunksize=2000) if r and len(r[1]) >= 4}
    with open(OUT, "w") as f:
        f.writelines(f"{e}\t{n.replace(chr(9), ' ')}\n" for e, n in sorted(rows))
    sql = f"""
CREATE TABLE IF NOT EXISTS org_dba (ein char(9) NOT NULL, name text NOT NULL, PRIMARY KEY (ein, name));
CREATE TEMP TABLE d (ein char(9), name text);
\\copy d FROM '{OUT}' WITH (FORMAT text)
INSERT INTO org_dba SELECT DISTINCT ein, name FROM d WHERE ein IN (SELECT ein FROM orgs) ON CONFLICT DO NOTHING;
SELECT count(*), count(DISTINCT ein) FROM org_dba;"""
    r = subprocess.run(["psql", DB, "-v", "ON_ERROR_STOP=1", "-qAt"], input=sql, text=True, capture_output=True)
    print(f"{len(files)} filings read; org_dba rows, orgs: {r.stdout.strip() or r.stderr.strip()}")

if __name__ == "__main__":
    main()
